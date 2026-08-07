# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

CLI tooling for processing firewall/network-connection logs. The project name is PyResolv. It is a single
package, `pyresolv/`, built as a set of composable filter-stages (`collect`, `trim`, `merge`, `aggregate`,
`resolve`) that compose via shell pipes — Unix-filter style: one stage = one process, reads stdin/`-i`, writes
stdout/`-o`. Sources (e.g. `graylog`/OpenSearch) and resolvers (e.g. `gunter`) are name-registered plugins.

There is no git repository (as of this writing), no lint config. Comments and docstrings are in English. All
user-facing strings (CLI help, errors, stderr status) are English `msgid`s in the code, translated to Russian
via gettext (`po/ru.po`); see the Localization section — match that when editing user-facing strings.

This replaced an earlier version of the project (two duplicated monoliths: `get_dst_ip_ranges.py` +
`resolver.py` + `api/gunter/resolve.py`, driven by boolean flags). See `.md/PLAN.md` for the refactor spec that
produced the current structure.

## Environment & running

Python 3.10+. Dependencies in `requirements.txt` (`pandas==3.0.3`, `tqdm==4.67.3`, `requests`,
`pydantic-settings`). Install editable to get the `pyresolv` console script:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .

cp .env.example .env   # fill in GRAYLOG__*/GUNTER__* as needed

./.venv/bin/pyresolv --type trim -i input.csv -o trimmed.csv
./.venv/bin/pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \
  | ./.venv/bin/pyresolv --type trim \
  | ./.venv/bin/pyresolv --type aggregate \
  | ./.venv/bin/pyresolv --type resolve --resolver gunter -o out.csv
```

`pyresolv` is a real package (`__init__.py` everywhere, no namespace-package tricks), installable via
`pyproject.toml` (`pip install -e .`), console-script entry point `pyresolv -> pyresolv.cli:main`. `cli.py`
follows the conventional `if __name__ == "__main__": main()` idiom (no `__main__.py`).

Tests: `./.venv/bin/python -m pytest tests/`.

## Architecture

```
pyresolv/
  config.py          pydantic-settings: nested per-integration settings loaded from .env
  schema.py           CANONICAL_COLUMNS / DROP_COLS / GROUP_COLS / sort order / pandas read kwargs — single source
  io.py                open_input/open_output: path -> file, None/'-' -> stdin/stdout
  subnets.py          ipaddress helpers: CIDR parsing, octet-prefix, subnet labels, split filenames
  pipeline.py         --type -> stage dispatcher (Variant A: one stage per process)
  runner.py           single-process pipeline engine + YAML config (Variant B)
  cli.py               argparse; exports main(); routes the `run` subcommand to runner.py
  sources/
    base.py            Source ABC + SOURCES registry + register_source; time-window helpers
    graylog.py          OpenSearch _search + search_after pagination (ported 1:1)
  stages/
    collect.py, trim.py, merge.py, aggregate.py
  resolvers/
    base.py             Resolver ABC + RESOLVERS registry; ThreadPool, cache, idempotent skip
    default_chain.py    `default` resolver: composite chain GEO -> RDAP -> WHOIS
    rdap.py / whois.py   native ASN/contacts/country via ipwhois (RDAP / port-43)
    geo_maxmind.py       country from a local MaxMind .mmdb (optional geoip2)
    _rdap.py             shared RDAP helpers (safe_get + contacts extraction)
    gunter.py            external Gunter HTTP service (geo-lookup + whois)
```

**CSV wire format between stages**: fixed-header CSV. All pandas reads across the package share
`schema.PANDAS_READ_KWARGS` (`keep_default_na=False, dtype=str, low_memory=False`) so that (a) empty fields
stay empty strings rather than becoming NaN, (b) literal values like `"NA"`/`"NULL"` in real data are never
silently swallowed as missing, and (c) `aggregate --streaming` is guaranteed to produce byte-identical output
to the non-streaming full-load mode regardless of chunk boundaries (dtype can't drift chunk-to-chunk).

**Stage stdout is data-only**: every stage prints its status/progress messages to stderr (`print(..., file=sys.stderr)`,
tqdm already defaults to stderr) — stdout is reserved for the CSV wire format so stages can be piped together.

**Two ways to run a pipeline** (both use the same stages, produce the same result):
- **Variant A** (`pipeline.py`): `pyresolv --type X | pyresolv --type Y` — one stage per OS process, glued by
  shell pipes, CSV on the wire. Streaming/chunked, bounded memory; the default and the flexible option.
- **Variant B** (`runner.py`): `pyresolv run --config pipeline.yaml` — the whole pipeline in ONE process, a live
  `DataFrame` flowing between steps (no CSV re-serialization). Faster, single log/config, but holds the dataset
  in memory. Each stage exposes an in-memory `*_frame` core (`trim_frame`, `aggregate_frame`, `collect_frame`,
  `merge_frames`, `Resolver.enrich`) that the engine chains; the path-based Variant A functions are thin
  read→core→write wrappers around the same logic, so both stay byte-identical. YAML steps are validated with
  per-step pydantic models (`extra="forbid"`), so a typo'd param fails fast before anything runs. `cli.main()`
  routes `argv[0] == "run"` to `runner.run_pipeline`; everything else is the classic `--type` parser, unchanged.
  The `run` parser also accepts the stage flags (`--out-dir`, `--start`, `--min-count`, `--resolver`,
  `--cache`, …, all `default=None`); `main()` forwards the provided ones as an `overrides` dict, and
  `run_pipeline` merges them onto each step's YAML params for fields that step's model actually has
  (`--start` → collect & aggregate, `--out-dir` → aggregate) — precedence **CLI > YAML > ENV/config**.
  Needs `PyYAML`.

**`--delete`/`--del`**: handled centrally in `pipeline.dispatch` (via `_delete_inputs`), NOT inside individual
stages — after the stage handler returns successfully, it removes the stage's `-i` input file(s), so a chain
like `trim --del -i connections.csv -o trimmed.csv` then `aggregate --del -i trimmed.csv -o aggregated.csv`
leaves only the final result on disk. Safety guarantees: deletion runs only after the handler returns without
raising (a failed stage keeps its input); never deletes stdin (`-`/no path) or a path equal to `-o`
(`-i == -o` is skipped); for `collect` it is a no-op (input is ignored). Every deletion is logged to stderr.

**Stages** (`pyresolv/stages/`):
1. **collect** — `sources/graylog.py` pages OpenSearch via `search_after` (page size from
   `GRAYLOG__SEARCH_SIZE`), filters to IPv4 with non-private `DstIP`, applies optional `SrcIP` allowlist
   (`GRAYLOG__SRC_IP_LIST`, a `terms` query) and/or subnet filter (`GRAYLOG__SRC_IP_CIDR`, a **list** of
   CIDRs). `SrcIP` is a string field holding a plain dotted-quad IPv4 (no mask), so CIDR filtering is a
   server-side `prefix` query on the octet-aligned prefix (`bool.should` + `minimum_should_match=1`, narrowing
   a /25 to its enclosing /24) plus an **exact client-side `ipaddress` check** in `fetch_window` (pins down
   finer masks). When **both** `SRC_IP_LIST` and `SRC_IP_CIDR` are set, they combine per
   `GRAYLOG__SRC_IP_MATCH_MODE`: **`or`** (default) keeps a `SrcIP` in the list **OR** in a subnet (server-side
   the two sub-filters are wrapped in one `bool.should`; client-side `_src_ip_allowed` mirrors it); **`and`**
   requires **both** (sub-filters go straight into `filter`, which ANDs them). With only one of the two set, the
   mode is irrelevant. (An `and` of a list and a non-overlapping subnet matches nothing — that combination
   silently returning 0 rows was the original footgun this mode makes explicit.) `--source` picks the source by
   name (`sources.SOURCES` registry); default comes from `settings.default_source`, then falls back to
   `"graylog"`.
2. **trim** — drops `schema.DROP_COLS`, chunked read/write (`schema.DEFAULT_TRIM_CHUNKSIZE`, 10k rows). The
   tqdm progress bar tracks actual bytes consumed from the input file handle (`f.tell()`), not the pandas
   in-memory chunk size — the original script's progress bar was tracking two different units (`total` in file
   bytes, `update()` in `chunk.memory_usage(deep=True)`), which never lined up.
3. **merge** — concatenates whatever CSV inputs are passed via repeated `-i`, taking the header from the first
   non-empty one. (Simplified vs. the old `merge_files_by_creation_time`, which scanned a directory for
   `connections_*.csv` and sorted by file ctime — that directory-scanning responsibility now belongs to the
   caller/shell, not the stage.)
4. **aggregate** — `groupby(GROUP_COLS).size()`, sorted by `count` desc then `ac_action`/`SrcIP`/`DstIP`/`DstPort`
   asc (`schema.SORT_CANDIDATES`, intersected with columns actually present). **Default mode: `--streaming`**
   (`argparse.BooleanOptionalAction`, default `True`) — full load easily exhausts RAM on tens-of-millions-of-rows
   inputs (every cell is a `dtype=str` Python object), so streaming is the safe default. `--streaming`
   (+`--chunk-size`, default 500_000): reads in chunks, computes a partial `groupby().size()` per chunk, then
   sums partial counts by key — mathematically equivalent to the full-load `.size()`, and made byte-identical to
   it by sharing `PANDAS_READ_KWARGS` (see above); shows a tqdm byte-progress bar over the input file (`f.tell()`),
   like `trim`. `--no-streaming` forces the old full in-memory load (faster for small files). Both modes produce
   byte-identical output. **`--min-count`** (default from `MIN_UNIQ_COUNT` in `.env`, else `1` = keep all) drops
   aggregated groups whose `count` is below the threshold — applied *after* the full aggregation (in streaming the
   group's final count is only known once all chunks are summed), identically in both modes, so the byte-identical
   guarantee holds. This is the one config value `aggregate` reads (a plain top-level `Settings` field, needs no
   integration configured). **`--out-dir DIR`** (mutually exclusive with `-o`) splits the final aggregation into
   one CSV per subnet instead of a single file: rows are bucketed by their `SrcIP`'s CIDR (from
   `GRAYLOG__SRC_IP_CIDR`, resolved to `ipaddress` networks in `pipeline.run_aggregate`/`runner` and passed in,
   so the stage stays config-decoupled), unmatched rows go to an `other` file, and filenames carry the time slice
   from `--start`/`--end`/`--time-unit` (`pyresolv/subnets.py::slice_filename`, e.g.
   `aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv`). Requires a non-empty `SRC_IP_CIDR`.
5. **resolve** — `resolvers/base.py` has the generic mechanics (ThreadPoolExecutor, `--workers`, a **persistent
   cross-run cache**, `_is_already_enriched` idempotent skip — a row is skipped if `country`/`asn`/`asn_descr`/
   `contacts` are all already non-empty); each resolver implements only `resolve_one(ip) -> dict`, so both the
   thread pool and the cache are shared by **every** resolver (not just `gunter`). The persistent cache
   (`resolvers/cache.py`) is checked in `enrich` before the thread pool: keys found in cache skip the network,
   misses are resolved then stored. Key is namespaced `resolver_name:RESOLVE_SCHEMA_VERSION:key`; the entry's
   TTL comes from `compute_cache_expiry` — the resolver's optional `expires` hint (RDAP `expiration` event /
   tcinet `paid-till`, returned by `resolve_one` as a meta key, never written to the CSV) **+1 day**, else the
   1st of next month; **empty/failed results are not cached** (so a transient outage can't poison it). Backend
   from `RESOLVE__CACHE` (`default` = SQLite file `RESOLVE__CACHE_PATH`; `redis` = shared, native `EXPIREAT`,
   optional `.[redis]` extra; `none`); the `--cache/--no-cache` flag (YAML `cache: true/false`) toggles it per
   run, backend errors are non-fatal (log + behave as miss/no-store). The default thread count is the resolver-agnostic `RESOLVE__WORKERS`
   (`settings.resolve.workers`, default 3), applied in both `pipeline.run_resolve` and `runner._run_resolve`;
   `--workers`/YAML `workers` overrides it. `--resolver` picks by name
   (`resolvers.RESOLVERS` registry); default from `settings.default_resolver`, then `"default"`. Resolvers:
   **`default`** (`default_chain.py`) runs providers GEO → RDAP → WHOIS, filling each `RESOLVE_COLUMN` from the
   first provider that returns a non-empty value and stopping early once all are filled; **`rdap`**/**`whois`**
   do ASN/description/contacts/country via `ipwhois` (`lookup_rdap` / `lookup_whois`). Each has an internal
   fallback cascade, each tier firing only when the previous returned nothing: **`rdap`** = direct RIR RDAP →
   **rdap.ss** aggregator (`RESOLVE__RDAPSS`, HTTP `rdap.ss/api/query?q=`, fills country+contacts from the raw
   RFC7483 `entities`/vcardArray — no ASN) → RDAP **bootstrap** (`RESOLVE__RDAP_BOOTSTRAP`); **`whois`** = ipwhois
   port-43 whois → **tcinet** (`RESOLVE__TCINET`, `whois.tcinet.ru:43`, domain whois for .ru/.su/.рф — keyed by
   `url_domain`, no-op for an IP key, punycode for IDN). Via the `default` chain (rdap before whois) these tiers
   form one linear cascade. **`geo_maxmind`** reads
   country from a local MaxMind `.mmdb` via `geoip2` (optional extra — no path/lib → yields nothing);
   **`gunter`** still calls the external HTTP service. Providers return a full `_empty_result()`-padded dict
   (so partial fills are safe with `base.enrich`), never raise (log + return partial), and read timeouts/mmdb
   path from `settings.resolve` (`RESOLVE__*`). Contact extraction (RDAP `objects` is a dict keyed by handle)
   lives in `_rdap.py`, shared by `rdap` and `gunter`. Caveat: `country` can be a MaxMind name or a 2-letter
   RDAP/WHOIS code; direct RDAP/WHOIS per unique IP is rate-limit-sensitive on bulk runs.

## Configuration (`pyresolv/config.py`, `.env`)

All previously-hardcoded operational constants (`OPENSEARCH_URL`, `INDEX`, `STREAM_ID`, `SRC_IP_LIST`,
`SRC_IP_CIDR`, `GUNTER_BASE_URL`, timeouts, worker counts) now live in `.env` (see `.env.example`), loaded via
`pydantic-settings`. Nested settings use the `__` delimiter: `GRAYLOG__URL`, `GRAYLOG__STREAM_ID`,
`GUNTER__BASE_URL`, etc.

`Settings.graylog`/`Settings.gunter` are `Optional` — stages that don't need an integration (`trim`, `merge`,
`aggregate`) never require one. (`aggregate` does read the top-level `min_uniq_count` field for `--min-count`'s
default, but that is a plain scalar with a default — it constructs fine with no integration configured.) Stages
that do the external HTTP integrations (`collect` -> graylog, `resolve --resolver gunter` -> gunter) fetch their
section via `settings.require_graylog()`/`require_gunter()`, which raises a clear `ConfigError` (caught in
`cli.py`, printed to stderr, exit code 2) if the section is entirely absent. If a section is *partially* filled
in (e.g. `GRAYLOG__URL` set but `GRAYLOG__STREAM_ID` missing), `pydantic_settings.BaseSettings` itself raises a
`ValidationError` at `Settings()` construction time — i.e. at the very start of the run, not mid-stream. The
**native resolvers** (`default`/`rdap`/`whois`/`geo_maxmind`) read `settings.resolve` (`ResolveSettings`, a
top-level section with all-defaulted fields — `RESOLVE__MMDB_PATH`, `RESOLVE__RDAP_TIMEOUT`,
`RESOLVE__WHOIS_TIMEOUT`), so they construct with no `.env` at all.

Source-IP filtering for `collect` uses `GRAYLOG__SRC_IP_LIST` (exact IPs, `terms`) and/or
`GRAYLOG__SRC_IP_CIDR` (a JSON list of CIDR subnets, e.g. `["10.2.83.0/24"]`), combined per
`GRAYLOG__SRC_IP_MATCH_MODE` (`or` default / `and` — see the `collect` stage description). `SRC_IP_CIDR`
doubles as the bucket definition for `aggregate --out-dir`. It replaced the earlier `SRC_IP_REGEX` (regex
patterns) — regex can't express arbitrary masks like /25, CIDR can, and `ipaddress` handles the math
(`pyresolv/subnets.py`).

Historical bug fixed during the migration:
- `trim`'s tqdm progress bar mismatched units (see stage description above).

## Localization (`pyresolv/i18n.py`, `po/`, `pyresolv/locale/`)

All user-facing strings (help, errors, stderr status) go through gettext. **Source language is English**
— the string in code IS the `msgid`; Russian lives in `po/ru.po`, compiled to
`pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo`. (Pydantic `Field(description=...)` texts in `config.py` are plain
English developer schema docs — they are never printed to users and don't go through gettext.)

- **Usage in code:** `from pyresolv.i18n import _, ngettext`. `_` / `ngettext` are functions that read the
  current translation from a module global, so calling `i18n.setup(lang)` once at the top of `main()` is enough
  — everything built afterwards (including argparse `help=`) picks up the language. Named `%(x)s` placeholders
  (not positional `{}`), so translations can reorder. Counts use `ngettext` (Russian has 3 plural forms;
  `Plural-Forms` header in `ru.po`).
- **Language selection** (`i18n.setup`): `--lang {ru,en}` → environment (`LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG`,
  read by gettext when `languages=None`) → English via `fallback=True` (returns the msgid). `cli.py` pre-parses
  `--lang` with a throwaway `add_help=False` parser **before** building the real parser (chicken-and-egg: help
  strings must already be in the chosen language). Note: argparse's *own* built-in strings (`usage:`, etc.)
  stay English — only our strings are translated.
- **Tooling:** compiling `.po → .mo` uses the bundled pure-Python `tools/msgfmt.py` (no GNU gettext / Babel
  needed) — this is why `.mo` compiles anywhere; `init.sh` runs it on setup and the `.mo` is committed.
  Extraction/update (`make i18n-extract` / `i18n-update`) need Babel (`pip install -e '.[i18n]'`); compile is
  `make i18n-compile`. Adding a language = new `po/<lang>.po` + add to `LANGS` in the `Makefile`.
