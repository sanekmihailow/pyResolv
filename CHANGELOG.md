# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.6.1] - 2026-08-03

### Fixed
- The resolve cache no longer crashes the run when its backend cannot be initialized
  (e.g. a read-only `RESOLVE__CACHE_PATH`, or the `redis` package missing): `get_cache`
  now logs a warning and falls back to no caching. Caching is an optimization and must
  never take down a pipeline.

## [2.6.0] - 2026-08-03

### Added
- Persistent cross-run **resolve cache** (`resolvers/cache.py`), shared by every resolver via
  `base.enrich`. Keys found in cache skip the network. Entry TTL = the resolved expiry date
  (RDAP `expiration` event / tcinet `paid-till`) **+1 day**, or the 1st of next month when no
  date is available; empty/failed results are not cached. Backends via `RESOLVE__CACHE`:
  `default` (local SQLite file `RESOLVE__CACHE_PATH`), `redis` (shared, native TTL, optional
  `.[redis]` extra — `RESOLVE__REDIS_URL`/`RESOLVE__REDIS_PREFIX`), or `none`. The
  `--cache`/`--no-cache` flag (YAML `cache: true/false`) toggles caching per run; backend errors
  are non-fatal. Cache keys are namespaced by resolver name + `RESOLVE_SCHEMA_VERSION`.

## [2.5.0] - 2026-07-30

### Added
- `rdap` resolver fallback cascade (each tier fires only on an empty previous result): direct RIR
  RDAP → **rdap.ss** aggregator → RDAP bootstrap. `RESOLVE__RDAPSS` (default `true`),
  `RESOLVE__RDAPSS_URL`, `RESOLVE__RDAPSS_TIMEOUT`. rdap.ss is a reachable HTTP endpoint that
  proxies to the right registry (fills country + contacts from the raw RFC 7483 `entities`; an
  RDAP IP object carries no ASN) — useful when the direct RIR RDAP is unreachable from the host.
- `whois` resolver fallback: after the ipwhois whois, **tcinet** domain whois (`whois.tcinet.ru:43`)
  for .ru/.su/.рф. `RESOLVE__TCINET` (default `false`), `RESOLVE__TCINET_HOST`,
  `RESOLVE__TCINET_TIMEOUT`. Domain-oriented (keyed by `url_domain`, punycode for IDN); a no-op for
  an IP key. Via the `default` chain (rdap→whois) all tiers form one linear cascade.

## [2.4.0] - 2026-07-30

### Changed
- Resolver thread count is now unified under `RESOLVE__WORKERS` (default `3`) and applies
  to **every** resolver (`default`/`rdap`/`whois`/`geo_maxmind`/`gunter`) — the thread pool
  in `resolvers/base.py` was always shared, but the default worker count used to be
  gunter-specific. `--workers` / YAML `workers` still override it. **`GUNTER__MAX_WORKERS`
  is removed**; a leftover value in `.env` is silently ignored — switch to `RESOLVE__WORKERS`.

### Added
- `RESOLVE__RDAP_BOOTSTRAP` (default `true`): RDAP bootstrap fallback for the `rdap`
  resolver (and thus the `default` chain). When the primary ASN-based `lookup_rdap`
  returns nothing (RIR misdetected/unreachable), it retries via the RDAP bootstrap
  server, which follows referrals to the right RIR without an ASN lookup. The extra
  request fires only on an empty result. `country` now also falls back to the RDAP
  network object's own `country` when the ASN country code is absent (as in bootstrap
  mode).

## [2.3.0] - 2026-07-30

### Added
- `GRAYLOG__SRC_IP_MATCH_MODE` (`or` default / `and`): controls how `SRC_IP_LIST` and
  `SRC_IP_CIDR` combine for `collect` when **both** are set. `or` keeps a `SrcIP` in the
  list **or** in a subnet; `and` requires both. Previously the two were always ANDed,
  so a list and a non-overlapping subnet silently matched **nothing** — the default is
  now `or`, and `and` makes the strict behavior explicit.
- `tools/diagnose_graylog.py`: read-only diagnostic that reruns the exact `collect`
  OpenSearch query and strips it clause by clause, pinpointing which filter (index /
  time / stream / field names / SrcIP) is returning 0 rows.

### Fixed
- `resolve` no longer crashes with `KeyError` on an empty (0-row) input frame — e.g.
  when `collect` returns 0 rows. Boolean-indexing a 0-row DataFrame dropped all columns;
  `enrich` now short-circuits an empty frame and passes it through with the resolve
  columns added.

## [2.2.0] - 2026-07-24

### Added
- Native, self-contained resolvers (no external service needed):
  - `rdap` — ASN / description / contacts / country via RDAP (`ipwhois.lookup_rdap`).
  - `whois` — the same, best-effort, via legacy port-43 WHOIS (`ipwhois.lookup_whois`).
  - `geo_maxmind` — country from a local MaxMind GeoLite2 `.mmdb` (optional `geoip2`).
  - `default` — the composite chain **GEO → RDAP → WHOIS**, filling each column from the
    first provider that returns a value (stops early once all are filled).
- `RESOLVE__MMDB_PATH`, `RESOLVE__RDAP_TIMEOUT`, `RESOLVE__WHOIS_TIMEOUT` settings.
- Dependency `ipwhois` (required); `geoip2` as an optional extra (`pip install -e '.[geo]'`).

### Changed
- **Default resolver is now `default`** (the native chain) instead of `gunter`. `gunter`
  remains available via `--resolver gunter`.
- `country` may now be a MaxMind country **name** or, via the RDAP/WHOIS fallbacks, a
  2-letter **code**.

## [2.1.1] - 2026-07-24

### Fixed
- `resolve --resolver gunter`: the `contacts` column was always empty against the real
  Gunter service. Gunter returns the raw ipwhois RDAP structure where `ip_whois.objects`
  is a dict keyed by entity handle, but `_extract_contacts` expected a list and bailed out.
  It now iterates the dict's values (still accepts a list for back-compat), so `contact.name`
  entries are extracted and deduplicated.

## [2.1.0] - 2026-07-24

### Added
- `aggregate --out-dir DIR` — split the final aggregation into one CSV per subnet
  (bucketed by each row's `SrcIP`), instead of a single `-o` file. Rows outside every
  listed subnet go to an `other` file. Filenames carry the time slice from
  `--start`/`--end`/`--time-unit`, e.g.
  `aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv`. Available in both
  Variant A (`--type aggregate`) and Variant B (`aggregate` step in `pyresolv run`).
- `pyresolv/subnets.py` — CIDR helpers built on the stdlib `ipaddress` module.

### Changed
- **BREAKING:** `GRAYLOG__SRC_IP_REGEX` replaced by **`GRAYLOG__SRC_IP_CIDR`** (a list of
  CIDR subnets, e.g. `["10.2.83.0/24"]`). It filters `collect` (server-side `prefix` on the
  string `SrcIP` narrowing to the enclosing /24, plus an exact client-side `ipaddress`
  check for finer masks like /25) and defines the `aggregate --out-dir` buckets. Update
  your `.env` accordingly.

## [2.0.0] - 2026-07-24

Baseline of the current PyResolv — a CLI for processing firewall / network-connection
logs, built as composable Unix-style filter stages (one stage = one process, reads
stdin/`-i`, writes stdout/`-o`) that compose via shell pipes.

### Added
- Filter stages: `collect`, `trim`, `merge`, `aggregate`, `resolve`.
- Name-registered plugins: sources (`graylog`/OpenSearch) and resolvers (`gunter`).
- `aggregate`: streaming by default (bounded memory, safe for very large files),
  `--min-count` / `MIN_UNIQ_COUNT` threshold, byte-progress bar.
- Two ways to run a pipeline: shell-pipe per stage (Variant A) and a single-process
  YAML pipeline `pyresolv run --config` with a live DataFrame between steps (Variant B).
- `--delete`/`--del` to remove intermediate inputs, keeping only the final result.
- `--lang {ru,en}` gettext localization (English source strings, Russian catalog).
- `--version` flag (now also available under the `run` subcommand).
- Typed configuration via `pydantic-settings` loaded from `.env`.
