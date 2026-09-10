"""Tests for the persistent resolve cache (pyresolv/resolvers/cache.py) and its
integration into base.enrich. All offline (SQLite in tmp_path, a fake redis)."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pyresolv.config import parse_ttl
from pyresolv.resolvers.base import Resolver
from pyresolv.resolvers.cache import (
    NullCache,
    RedisCache,
    SqliteCache,
    _first_of_next_month,
    _parse_date,
    compute_cache_expiry,
)
from pyresolv.schema import RESOLVE_COLUMNS

_NOW = datetime.now(timezone.utc)


def _future(days=2):
    return _NOW + timedelta(days=days)


# --- helpers ----------------------------------------------------------------

def test_parse_date_formats():
    assert _parse_date("2025-01-01T21:00:00Z") == datetime(2025, 1, 1, 21, tzinfo=timezone.utc)
    assert _parse_date("2025-01-02") == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert _parse_date("2025.01.03") == datetime(2025, 1, 3, tzinfo=timezone.utc)
    assert _parse_date("20250104") == datetime(2025, 1, 4, tzinfo=timezone.utc)
    assert _parse_date("nonsense") is None
    assert _parse_date(None) is None and _parse_date("") is None


def test_compute_cache_expiry_plus_one_day():
    tomorrow = (_NOW + timedelta(days=1)).date().isoformat()
    exp = compute_cache_expiry(tomorrow)
    assert exp.date() == (_NOW + timedelta(days=2)).date()  # expired_date + 1 day


def test_compute_cache_expiry_fallback_first_of_next_month():
    nxt = _first_of_next_month(_NOW)
    assert compute_cache_expiry(None) == nxt          # no date
    assert compute_cache_expiry("2000-01-01") == nxt  # date in the past -> fallback


def test_compute_cache_expiry_caps_far_future():
    # A wildly far-future date exceeds the TTL cap -> fallback, not +1 day.
    assert compute_cache_expiry("2999-01-01") == _first_of_next_month(_NOW)


# --- explicit TTL (RESOLVE__CACHE_TTL / --cache-ttl) -------------------------

def test_parse_ttl_units_and_empty():
    assert parse_ttl("30d") == timedelta(days=30)
    assert parse_ttl("12h") == timedelta(hours=12)
    assert parse_ttl("45m") == timedelta(minutes=45)
    assert parse_ttl("2w") == timedelta(weeks=2)
    assert parse_ttl("3600") == timedelta(seconds=3600)  # bare number = seconds
    assert parse_ttl(" 1D ") == timedelta(days=1)        # trimmed, case-insensitive
    assert parse_ttl(None) is None and parse_ttl("") is None
    assert parse_ttl(timedelta(hours=1)) == timedelta(hours=1)


@pytest.mark.parametrize("bad", ["nonsense", "10x", "-1d", "0h"])
def test_parse_ttl_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_ttl(bad)


def test_compute_cache_expiry_ttl_replaces_fallback():
    ttl = timedelta(days=7)
    exp = compute_cache_expiry(None, ttl)          # no date -> now + ttl, not the 1st
    assert abs((exp - (_NOW + ttl)).total_seconds()) < 5
    assert exp != _first_of_next_month(_NOW)


def test_compute_cache_expiry_ttl_caps_expires_hint():
    # A hint 30 days out with a 1-day TTL -> the TTL wins (entry stays fresher).
    far = (_NOW + timedelta(days=30)).date().isoformat()
    exp = compute_cache_expiry(far, timedelta(days=1))
    assert abs((exp - (_NOW + timedelta(days=1))).total_seconds()) < 5


def test_compute_cache_expiry_expires_hint_wins_when_sooner():
    # A hint sooner than the TTL keeps its own date + 1 day.
    tomorrow = (_NOW + timedelta(days=1)).date().isoformat()
    exp = compute_cache_expiry(tomorrow, timedelta(days=30))
    assert exp.date() == (_NOW + timedelta(days=2)).date()


def test_compute_cache_expiry_ttl_capped_by_max_ttl():
    exp = compute_cache_expiry(None, timedelta(days=5000))
    assert exp <= _NOW + timedelta(days=367)


# --- SqliteCache ------------------------------------------------------------

def test_sqlite_roundtrip_and_expiry(tmp_path):
    c = SqliteCache(str(tmp_path / "sub" / "cache.sqlite"))  # nested dir auto-created
    val = {col: v for col, v in zip(RESOLVE_COLUMNS, ["RU", "1", "x", "y"])}
    c.set("k", val, _future())
    assert c.get("k") == val
    c.set("gone", {"country": "X"}, _NOW - timedelta(seconds=1))  # already expired
    assert c.get("gone") is None
    assert c.get("missing") is None


def test_null_cache_is_noop():
    c = NullCache()
    c.set("k", {"country": "RU"}, _future())
    assert c.get("k") is None


def test_get_cache_degrades_to_null_on_unwritable_path(capsys):
    from types import SimpleNamespace
    from pyresolv.resolvers.cache import get_cache
    # os.makedirs under an existing non-directory raises -> must NOT crash.
    s = SimpleNamespace(cache_path="/dev/null/nope/cache.sqlite")
    cache = get_cache("default", s)
    assert isinstance(cache, NullCache)
    assert "Cache disabled" in capsys.readouterr().err


# --- RedisCache (fake client) -----------------------------------------------

def test_redis_cache_set_get(monkeypatch):
    calls = {}

    class _FakeClient:
        def __init__(self):
            self.store = {}
        def get(self, k):
            return self.store.get(k)
        def set(self, k, v):
            self.store[k] = v
            calls["set"] = k
        def expireat(self, k, ts):
            calls["expireat"] = (k, ts)

    fake_redis = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda url: _FakeClient()))
    monkeypatch.setitem(sys.modules, "redis", fake_redis)

    c = RedisCache("redis://x", "pfx:")
    c.set("k", {"country": "RU"}, _future())
    assert calls["set"] == "pfx:k"          # prefixed key
    assert "expireat" in calls              # native TTL set
    assert c.get("k") == {"country": "RU"}


def test_redis_cache_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", None)  # -> import redis raises ImportError
    with pytest.raises(ValueError, match="redis"):
        RedisCache("redis://x", "p:")


# --- enrich integration -----------------------------------------------------

def _frame(ips):
    cols = {"DstIP": ips}
    for c in RESOLVE_COLUMNS:
        cols[c] = [""] * len(ips)
    return pd.DataFrame(cols)


class _Counting(Resolver):
    name = "counting"

    def __init__(self):
        self.calls = []

    def resolve_one(self, key):
        self.calls.append(key)
        r = self._empty_result()
        r["country"] = "RU"
        return r


def test_enrich_hit_skips_resolve_one(tmp_path):
    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    r1 = _Counting()
    r1.enrich(_frame(["1.1.1.1", "1.1.1.1", "2.2.2.2"]), "DstIP", 2, cache=cache)
    assert sorted(r1.calls) == ["1.1.1.1", "2.2.2.2"]  # unique keys, resolved once each

    r2 = _Counting()
    df2 = _frame(["1.1.1.1", "2.2.2.2"])
    r2.enrich(df2, "DstIP", 2, cache=cache)
    assert r2.calls == []                     # everything served from cache
    assert (df2["country"] == "RU").all()


class _Empty(Resolver):
    name = "empty"

    def resolve_one(self, key):
        return self._empty_result()


def test_enrich_does_not_cache_empty(tmp_path):
    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    r = _Empty()
    r.enrich(_frame(["9.9.9.9"]), "DstIP", 1, cache=cache)
    assert cache.get(r._cache_key("9.9.9.9")) is None   # empty result NOT stored


def test_enrich_caches_only_resolve_columns_with_expires(tmp_path):
    tomorrow = (_NOW + timedelta(days=1)).date().isoformat()

    class _Exp(Resolver):
        name = "exp"
        def resolve_one(self, key):
            r = self._empty_result()
            r["country"] = "RU"
            r["expires"] = tomorrow      # meta key -> drives TTL, not stored
            return r

    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    r = _Exp()
    r.enrich(_frame(["1.2.3.4"]), "DstIP", 1, cache=cache)
    stored = cache.get(r._cache_key("1.2.3.4"))
    assert stored == {c: ("RU" if c == "country" else "") for c in RESOLVE_COLUMNS}
    assert "exp:" in r._cache_key("1.2.3.4")   # namespaced by resolver name


def test_enrich_honours_cache_ttl(tmp_path):
    """A short --cache-ttl must actually shorten the stored entry's lifetime."""
    stored_at = {}

    class _Recording(SqliteCache):
        def set(self, key, value, expires_at):
            stored_at[key] = expires_at
            super().set(key, value, expires_at)

    cache = _Recording(str(tmp_path / "c.sqlite"))
    r = _Counting()
    r.enrich(_frame(["1.1.1.1"]), "DstIP", 1, cache=cache, cache_ttl=timedelta(hours=6))
    expires_at = stored_at[r._cache_key("1.1.1.1")]
    assert abs((expires_at - (_NOW + timedelta(hours=6))).total_seconds()) < 30


def test_enrich_reports_failed_share(tmp_path, capsys):
    """After the progress bar: how many of the resolved keys came back empty
    (exactly the ones not stored in the cache, retried on the next run)."""

    class _HalfEmpty(Resolver):
        name = "half_empty"

        def resolve_one(self, key):
            r = self._empty_result()
            if key.startswith("1."):
                r["country"] = "RU"
            return r

    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    r = _HalfEmpty()
    r.enrich(_frame(["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]), "DstIP", 2, cache=cache)
    err = capsys.readouterr().err
    assert "Resolved: 1 of 4" in err
    assert "failed: 3 (75.0%)" in err


def test_enrich_no_stats_line_when_all_cached(tmp_path, capsys):
    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    _Counting().enrich(_frame(["1.1.1.1"]), "DstIP", 1, cache=cache)
    capsys.readouterr()
    _Counting().enrich(_frame(["1.1.1.1"]), "DstIP", 1, cache=cache)  # full cache hit
    assert "Resolved:" not in capsys.readouterr().err
