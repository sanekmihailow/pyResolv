"""Persistent resolve cache: keyed by the (namespaced) resolve key, holding the
RESOLVE_COLUMNS result with a per-entry expiry.

Backends (selected by RESOLVE__CACHE):
  - ``none``    -> NullCache (no caching);
  - ``default`` -> SqliteCache (a local SQLite file, thread-safe for the
                   multi-threaded `base.enrich`);
  - ``redis``   -> RedisCache (shared, native TTL via EXPIREAT; needs the
                   optional ``redis`` extra).

Expiry rule (see compute_cache_expiry): the resolver may surface an ``expires``
date (domain paid-till, RDAP ``expiration`` event); the cache entry then lives
until that date + 1 day. With no usable date the entry lives until the 1st of
next month. Empty/failed results are never cached (handled in base.enrich), so a
transient outage does not poison the cache.

Backend errors are non-fatal: get/set swallow and log, behaving as a miss / a
no-store, so a broken cache never breaks a resolve run.
"""
from __future__ import annotations

import abc
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from pyresolv.i18n import _

# Never cache longer than this, so a malformed far-future date cannot freeze data.
_MAX_TTL = timedelta(days=366)


def _first_of_next_month(now: datetime) -> datetime:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _parse_date(value) -> Optional[datetime]:
    """Parse a datetime / ISO-8601 / YYYY-MM-DD / YYYY.MM.DD into an aware UTC
    datetime, or None if it cannot be parsed."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def compute_cache_expiry(expires) -> datetime:
    """Cache-entry expiry: the resolved `expires` date + 1 day when usable
    (future and within the TTL cap), otherwise the 1st of next month (UTC)."""
    now = datetime.now(timezone.utc)
    parsed = _parse_date(expires)
    if parsed is not None:
        candidate = parsed + timedelta(days=1)
        if now < candidate <= now + _MAX_TTL:
            return candidate
    return _first_of_next_month(now)


class Cache(abc.ABC):
    @abc.abstractmethod
    def get(self, key: str) -> Optional[dict]:
        """Return the cached value for `key` if present and not expired, else None."""

    @abc.abstractmethod
    def set(self, key: str, value: dict, expires_at: datetime) -> None:
        """Store `value` under `key` until `expires_at`."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class NullCache(Cache):
    def get(self, key: str) -> Optional[dict]:
        return None

    def set(self, key: str, value: dict, expires_at: datetime) -> None:
        pass


class SqliteCache(Cache):
    """Local SQLite-backed cache. Thread-safe via a single connection + lock,
    which is enough for the ThreadPoolExecutor in base.enrich."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        # WAL + synchronous=NORMAL: per-`set` commit stays durable across processes
        # but is ~200x cheaper than the default full fsync (bulk first-run writes).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        # Prune expired rows once on open (get() ignores stale rows anyway, so a
        # per-`set` full-table sweep — O(N) each write — is not worth it).
        self._conn.execute("DELETE FROM cache WHERE expires_at <= ?", (datetime.now(timezone.utc).timestamp(),))
        self._conn.commit()

    def get(self, key: str) -> Optional[dict]:
        now = datetime.now(timezone.utc).timestamp()
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM cache WHERE key = ? AND expires_at > ?", (key, now)
                ).fetchone()
        except Exception as e:  # pragma: no cover - defensive
            print(_("Cache read error: %(err)s") % {"err": e}, file=sys.stderr)
            return None
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    def set(self, key: str, value: dict, expires_at: datetime) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value), expires_at.timestamp()),
                )
                self._conn.commit()
        except Exception as e:  # pragma: no cover - defensive
            print(_("Cache write error: %(err)s") % {"err": e}, file=sys.stderr)

    def close(self) -> None:  # pragma: no cover - trivial
        with self._lock:
            self._conn.close()


class RedisCache(Cache):
    """Shared Redis-backed cache with native per-key TTL (EXPIREAT)."""

    def __init__(self, url: str, prefix: str) -> None:
        try:
            import redis  # noqa: F401
        except ImportError:
            raise ValueError(
                _("The 'redis' package is required for RESOLVE__CACHE=redis. "
                  "Install it: pip install -e '.[redis]'")
            ) from None
        self._prefix = prefix
        self._client = redis.Redis.from_url(url)

    def get(self, key: str) -> Optional[dict]:
        try:
            raw = self._client.get(self._prefix + key)
        except Exception as e:
            print(_("Cache read error: %(err)s") % {"err": e}, file=sys.stderr)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def set(self, key: str, value: dict, expires_at: datetime) -> None:
        try:
            full = self._prefix + key
            self._client.set(full, json.dumps(value))
            self._client.expireat(full, int(expires_at.timestamp()))
        except Exception as e:
            print(_("Cache write error: %(err)s") % {"err": e}, file=sys.stderr)


def get_cache(name: str, settings) -> Cache:
    """Build the cache backend named by RESOLVE__CACHE from resolve settings.

    Caching is an optimization, so a backend that cannot be initialized (e.g. a
    read-only cache path, or the redis package missing) must NOT crash the run:
    log a warning and fall back to NullCache (no caching)."""
    if name == "none":
        return NullCache()
    try:
        if name == "redis":
            return RedisCache(settings.redis_url, settings.redis_prefix)
        return SqliteCache(settings.cache_path)
    except Exception as e:
        print(
            _("Cache disabled (%(backend)s backend unavailable): %(err)s")
            % {"backend": name, "err": e},
            file=sys.stderr,
        )
        return NullCache()
