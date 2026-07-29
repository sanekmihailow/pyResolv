# Plan: refactor pyResolv into an extensible set of filter stages

## Context

Today the project is two nearly-duplicated monoliths:
- `get_dst_ip_ranges.py` — collect from OpenSearch (Graylog backend) by time windows + merge + trim + aggregate + enrich, all inside `main()` and toggled by boolean flags (`--merge`, `--aggregate`, `--enrich`).
- `resolver.py` — the same trim + aggregate, separately, for a ready CSV.
- `api/gunter/resolve.py` — IP enrichment via Gunter.

Problems we are removing:
1. Operational parameters (`OPENSEARCH_URL`, `INDEX`, `STREAM_ID`, `SRC_IP_LIST`, `SRC_IP_REGEX`, `GUNTER_BASE_URL`, timeouts, worker counts) are hardcoded.
2. The single source (OpenSearch/Graylog) and single resolver (Gunter) are hardwired — you can't add Elasticsearch/Mongo or a custom resolver without editing the core.
3. The data schema (`DROP_COLS`, `GROUP_COLS`) is duplicated across two files.
4. A set of boolean flags instead of a clear "one stage = one action" model.

Target outcome: a set of composable stages (Unix filters) connected by shell pipes; sources and resolvers as name-registered plugins; typed configuration moved out of the code.

## Locked decisions

- **Composition:** shell pipes. One stage = one run, reads stdin/`-i`, writes stdout/`-o`. An in-process config-driven pipeline is NOT built here (it can be added later on top of the same stages).
- **Config:** `pydantic-settings` over `.env`, nested models with a per-integration namespace (`GRAYLOG__URL`, `GUNTER__BASE_URL`, etc.).
- **Aggregate:** by default the current pandas full-load (fast, vectorized). The `--streaming` flag (+`--chunk-size`, default 500_000) switches to chunked pandas for very large files (hundreds of millions of rows) — read in chunks, `groupby` per chunk, sum the partial counts.
- **merge:** stays a narrow stage "concatenate several inputs into one stream".
- **Source selection:** `--source` is optional. Priority: CLI `--source` → `settings.default_source` → `"graylog"`.
- **Resolver selection:** `--resolver` is optional. Priority: CLI `--resolver` → `settings.default_resolver` → `"gunter"`.
- **Wire format between stages:** CSV with a fixed header (the header carries the schema).

## Target layout

```
pyresolv/
  config.py          # pydantic-settings: nested per-integration settings, loaded from .env
  schema.py          # CANONICAL_COLUMNS, DROP_COLS, GROUP_COLS, sort order — single source
  io.py              # read_records(path|stdin)->Iterator[dict]; write_records(iter, path|stdout); CSV
  pipeline.py        # --type -> stage dispatcher
  cli.py             # argparse; exports main(), entry point: if __name__ == "__main__": main()
  sources/
    base.py          # Source ABC + SOURCES registry + @register_source; shared time-window iteration
    graylog.py       # ported get_dst_ip_ranges logic (OpenSearch _search + search_after)
  stages/
    trim.py          # ported trim_csv (chunked reading already present)
    aggregate.py     # aggregate_csv + --streaming mode (chunked)
    merge.py         # ported merge_files_by_creation_time -> concatenate several -i/stdin
  resolvers/
    base.py          # Resolver ABC + RESOLVERS registry; SHARED: ThreadPool, per-key cache, idempotent skip
    gunter.py        # ported api/gunter/resolve.py: only geo-lookup + whois (resolve_one)
```

We make it a real package (`__init__.py`), since `api/` is currently a namespace package with no `__init__.py` and must be run from the repo root. The entry point stays in the classic idiom: `cli.py` defines `main()`, invoked via `if __name__ == "__main__": main()` (no `__main__.py`).

## CLI (spec)

Common flags:
- `--type {collect,trim,merge,aggregate,resolve}` — required, which stage.
- `-i, --input PATH` — input; stdin by default. Ignored for `collect` (the source generates data). May be given multiple times for `merge`.
- `-o, --output PATH` — output; stdout by default.

Per-stage:
- `collect`: `--source NAME` (default from config → graylog), `--start`, `--end`, `--time-unit {d,h}`. Source filters (SrcIP list/regex) come from config.
- `aggregate`: `--streaming` (flag, default off), `--chunk-size N` (default 500_000).
- `resolve`: `--resolver NAME` (default from config → gunter), `--key-column` (default `DstIP`), `--workers N`.

Composition example:
```bash
pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \
  | pyresolv --type trim \
  | pyresolv --type aggregate \
  | pyresolv --type resolve --resolver gunter -o out.csv
```

## Key abstractions (reusing existing logic)

**Source** (`sources/base.py`): an ABC with `fetch(windows) -> Iterator[dict]`; the base provides time-window iteration (`build_time_windows`, `shift_now`, `build_time_expr` from the current `get_dst_ip_ranges.py`). `graylog.py` implements only `fetch_window(gte, lt)` — porting `build_payload` + `process_window` (search_after pagination, IPv4 / non-private DstIP filter). Registry `SOURCES[name]` + decorator `@register_source("graylog")`.

**Resolver** (`resolvers/base.py`): all the shared mechanics from `enrich_csv_with_gunter` — ThreadPool (`max_workers`, default from config), per-key cache, idempotent skip via `_is_already_enriched`, writing the `country/asn/asn_descr/contacts` columns back. A subclass implements only `resolve_one(key) -> dict`. `gunter.py` = `_fetch_country` + `_fetch_whois` + `_extract_contacts`. Registry `RESOLVERS[name]`.

**Schema** (`schema.py`): move `DROP_COLS`, `GROUP_COLS`, sort candidates — defined once. Sources map raw fields → canonical.

**Config** (`config.py`): nested `BaseSettings` — `GraylogSettings`, `GunterSettings`, plus root-level `default_source`, `default_resolver`. Values from `.env` (create `.env.example`). Validated at startup.

## Migrating the current code

- `get_dst_ip_ranges.py`: windows → `sources/base.py`; payload+pagination → `sources/graylog.py`; `trim_csv` → `stages/trim.py`; `aggregate_csv` → `stages/aggregate.py`; `merge_files_by_creation_time` → `stages/merge.py`; constants → `config.py`.
- `api/gunter/resolve.py`: generic → `resolvers/base.py`, HTTP specifics → `resolvers/gunter.py`.
- `main()`: kept as the single entry point in `cli.py` (the `if __name__ == "__main__": main()` idiom); from the old `main()` in `get_dst_ip_ranges.py` only the argument dispatch into stages moves over, the function itself is not deleted.
- `resolver.py`: removed (its functionality = the `trim` + `aggregate` stages).
- `requirements.txt`: add `pydantic-settings` (pulls in `python-dotenv`).
- Fix along the way: `SRC_IP_REGEX` with a stray `\` (`r"10\.8\.\139\.\d+"`); tqdm total in bytes vs update by in-memory size.

## Verification

- Run a small reference CSV (5–10 rows, as in the discussion) through each stage separately, compare the output.
- End-to-end pipe composition: `collect|trim|aggregate|resolve`.
- **Key equivalence test:** `aggregate` without the flag and `aggregate --streaming` on the same input must produce an identical sorted result.
- `resolve` twice in a row on the same file — the second run makes no network calls (idempotency of `_is_already_enriched`).
- `--source` not set → falls back to `graylog`; an unknown `--source`/`--resolver` → a clear error from the registry.
- A required `.env` setting missing → the pydantic validator fails with a clear message at startup, not mid-run.
- `collect` requires a live OpenSearch — test against a real instance or mock HTTP; the other stages are verified offline.
