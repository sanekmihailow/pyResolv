# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
