# PyResolv

[Русский](README.ru.md) | **English**

Processing firewall logs with a set of composable filter stages
(collect/trim/merge/aggregate/resolve), connected via shell pipes — in the spirit
of classic Unix utilities: one stage = one process, reads stdin/`-i`,
writes stdout/`-o`.

## Stages

```
collect  ->  trim  ->  merge  ->  aggregate  ->  resolve
```

- **collect** — pulls connections from a source (`--source`, default
  `graylog`/OpenSearch) by time windows, writes CSV with header
  `timestamp,SrcIP,DstIP,DstPort,ac_action,ac_rule_name,url_domain,url_path`.
  Filters IPv4 with non-private `DstIP`. The only stage that requires
  network access to the source.
- **trim** — reads CSV in chunks, drops service columns
  (`timestamp`, `SrcPort`, `source`, `message`).
- **merge** — concatenates multiple CSV inputs (multiple `-i`) into one stream.
- **aggregate** — groups by
  `SrcIP -> DstIP -> DstPort -> ac_action -> url_domain -> ac_rule_name`,
  counts `count`, sorts (`count` descending, rest ascending). By default —
  streaming mode `--streaming` (+`--chunk-size`, default 500 000) with bounded
  memory, safe for very large files; `--no-streaming` enables full pandas
  full-load (faster on small files). Both modes produce byte-identical results.
  `--min-count` (or env `MIN_UNIQ_COUNT`, default `1` = keep all) filters out
  groups with `count` below the threshold. `--out-dir DIR` (instead of `-o`)
  splits the result into one CSV per subnet from `GRAYLOG__SRC_IP_CIDR`
  (unmatched rows → an `other` file); filenames carry the time slice, e.g.
  `aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv`.
- **resolve** — enriches rows by key column (`--key-column`, default `DstIP`)
  via a resolver (`--resolver`), adding `country`, `asn`, `asn_descr`,
  `contacts`. Resolvers: **`default`** (a self-contained chain GEO → RDAP →
  WHOIS), `rdap` / `whois` (single provider, via `ipwhois`), `geo_maxmind`
  (country from a local MaxMind `.mmdb` — set `RESOLVE__MMDB_PATH` and
  `pip install -e '.[geo]'`), and `gunter` (the external HTTP service).
  Idempotent: already-filled rows are not resolved again and make no network
  calls. Results are also cached across runs (persistent cache, `--cache` on by
  default): each unique key is looked up at most once until its cache entry
  expires — the resolved expiry date + 1 day, or the 1st of next month when no
  date is available (empty/failed lookups are not cached). Backend via
  `RESOLVE__CACHE` (`default` SQLite / `redis` / `none`); `--no-cache` disables it.

Sources and resolvers are plugins, registered by name
(`pyresolv/sources/`, `pyresolv/resolvers/`); to add a new source
or resolver, you don't need to modify the core.

## Project layout

```
pyResolv/
├── pyresolv/                    package (installed via `pip install -e .`)
│   ├── __init__.py              package version and short description
│   ├── cli.py                   entry point: argparse, exports main()
│   ├── pipeline.py              --type -> stage dispatcher (Variant A)
│   ├── runner.py                single-process pipeline engine + YAML config (Variant B)
│   ├── config.py                typed configuration (pydantic-settings) from .env
│   ├── schema.py                single source of the CSV schema: columns, sort order, read kwargs
│   ├── io.py                    open_input/open_output: path -> file, None/'-' -> stdin/stdout
│   ├── subnets.py               ipaddress helpers: CIDR parsing, octet prefix, labels, split filenames
│   ├── i18n.py                  localization via gettext (_/ngettext, setup)
│   ├── stages/                  filter stages
│   │   ├── collect.py           pulls records from a source by time windows
│   │   ├── trim.py              drops service columns (chunked reading)
│   │   ├── merge.py             concatenates several CSV inputs into one stream
│   │   └── aggregate.py         group-by + count, sorting, --streaming/--min-count
│   ├── sources/                 source plugins for `collect`
│   │   ├── base.py              Source ABC + SOURCES registry + time windows
│   │   └── graylog.py           OpenSearch _search + search_after pagination
│   ├── resolvers/               resolver plugins for `resolve`
│   │   ├── base.py              Resolver ABC + RESOLVERS registry + ThreadPool/cache/idempotency
│   │   ├── default_chain.py     `default` resolver: chain GEO -> RDAP -> WHOIS
│   │   ├── rdap.py              ASN/contacts/country via ipwhois RDAP
│   │   ├── whois.py             same via legacy port-43 WHOIS
│   │   ├── geo_maxmind.py       country from a local MaxMind .mmdb (geoip2)
│   │   ├── _rdap.py             shared RDAP helpers (contacts extraction)
│   │   └── gunter.py            external Gunter HTTP service (geo-lookup + whois)
│   └── locale/                  compiled translation catalogs (.mo)
│       └── ru/LC_MESSAGES/pyresolv.mo
├── po/                          translation sources (.po) and template (.pot)
│   └── ru.po
├── tools/
│   └── msgfmt.py                pure-Python .po -> .mo compiler (no GNU gettext/Babel)
├── tests/                       pytest tests
├── .env.example                 configuration sample (copy to .env)
├── pyproject.toml               package metadata, `pyresolv` console script
├── requirements.txt             dependencies (pandas, tqdm, requests, pydantic-settings)
├── Makefile                     i18n-extract / i18n-update / i18n-compile targets
├── init.sh                      environment setup + .mo compilation
```

## Requirements

- Python 3.10+

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# or as a package providing the pyresolv command:
pip install -e .
```

## Configuration

Configuration is typed (`pydantic-settings`) and read from `.env`.
Copy `.env.example` to `.env` and fill in the needed sections:

```bash
cp .env.example .env
```

`GRAYLOG__*`/`GUNTER__*` sections are only needed by stages that use
the corresponding integration (`collect` -> graylog, `resolve` ->
gunter). `trim`/`merge`/`aggregate` work without `.env` at all.

Nested settings use the `__` delimiter (`GRAYLOG__URL`, `RESOLVE__RDAP_TIMEOUT`, …).
List values (`SRC_IP_LIST`, `SRC_IP_CIDR`) are JSON arrays, e.g. `["10.2.83.0/24"]`.

**Top-level**

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_SOURCE` | `graylog` | Source used by `collect` when `--source` is omitted |
| `DEFAULT_RESOLVER` | `default` | Resolver used by `resolve` when `--resolver` is omitted |
| `MIN_UNIQ_COUNT` | `1` | Default for `aggregate --min-count` (drop groups below it; `1` = keep all) |

**`GRAYLOG__*`** (used by `collect`)

| Variable | Default | Description |
|---|---|---|
| `GRAYLOG__URL` | — *(required)* | Base OpenSearch/Graylog URL, e.g. `http://localhost:9200` |
| `GRAYLOG__STREAM_ID` | — *(required)* | Graylog stream ID used to filter documents |
| `GRAYLOG__INDEX` | `device_net` | Index name without the date suffix |
| `GRAYLOG__SEARCH_SIZE` | `5000` | `search_after` page size |
| `GRAYLOG__REQUEST_TIMEOUT` | `300` | HTTP request timeout to OpenSearch, seconds |
| `GRAYLOG__SRC_IP_LIST` | `[]` | JSON array of exact SrcIPs (`terms` filter) |
| `GRAYLOG__SRC_IP_CIDR` | `[]` | JSON array of source subnets in CIDR; also defines `aggregate --out-dir` buckets |
| `GRAYLOG__SRC_IP_MATCH_MODE` | `or` | Combine LIST and CIDR when both set: `or` (in either) / `and` (in both) |

**`GUNTER__*`** (used by `resolve --resolver gunter`)

| Variable | Default | Description |
|---|---|---|
| `GUNTER__BASE_URL` | — *(required)* | Base URL of the Gunter API |
| `GUNTER__REQUEST_TIMEOUT` | `30` | HTTP request timeout to Gunter, seconds |

**`RESOLVE__*`** (native resolvers `default`/`rdap`/`whois`/`geo_maxmind`; all optional)

| Variable | Default | Description |
|---|---|---|
| `RESOLVE__MMDB_PATH` | *(unset)* | Path to a local MaxMind GeoLite2 `.mmdb` for `geo_maxmind`; unset → geo yields nothing |
| `RESOLVE__RDAP_TIMEOUT` | `10` | Socket timeout for the `rdap` resolver, seconds |
| `RESOLVE__RDAPSS` | `true` | `rdap` fallback: query the rdap.ss aggregator (before bootstrap) when the direct lookup returned nothing |
| `RESOLVE__RDAPSS_URL` | `https://rdap.ss/api/query?q=` | rdap.ss query URL (empty also disables the fallback) |
| `RESOLVE__RDAPSS_TIMEOUT` | `15` | HTTP timeout for the rdap.ss fallback, seconds |
| `RESOLVE__RDAP_BOOTSTRAP` | `true` | On an empty RDAP lookup, retry via the RDAP bootstrap server (fallback only) |
| `RESOLVE__WHOIS_TIMEOUT` | `15` | Socket timeout for the `whois` resolver, seconds |
| `RESOLVE__TCINET` | `false` | `whois` fallback: TCI domain whois (`whois.tcinet.ru:43`) for .ru/.su/.рф — domain-only, enable for `--key-column url_domain` runs |
| `RESOLVE__TCINET_HOST` | `whois.tcinet.ru` | TCI domain whois host (port 43) |
| `RESOLVE__TCINET_TIMEOUT` | `15` | Socket timeout for the tcinet fallback, seconds |
| `RESOLVE__WORKERS` | `3` | Default number of resolving threads for **any** resolver (overridden by `--workers`) |
| `RESOLVE__CACHE` | `default` | Persistent resolve-cache backend: `default` (SQLite file), `redis`, or `none` |
| `RESOLVE__CACHE_PATH` | `~/.cache/pyresolv/resolve-cache.sqlite` | SQLite file for the `default` backend |
| `RESOLVE__REDIS_URL` | `redis://localhost:6379/0` | Redis URL for the `redis` backend |
| `RESOLVE__REDIS_PREFIX` | `pyresolv:resolve:` | Key prefix for the `redis` backend |

## Running

```bash
# A single stage
pyresolv --type trim -i input.csv -o trimmed.csv

# Composition via a shell pipe
pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \
  | pyresolv --type trim \
  | pyresolv --type aggregate \
  | pyresolv --type resolve --resolver gunter -o out.csv

# Large file, streaming aggregation
pyresolv --type aggregate --streaming --chunk-size 500000 -i trimmed.csv -o aggregated.csv
```

Without installing the package, you can run as a module:
`python -m pyresolv.cli --type trim ...` or via
`./.venv/bin/pyresolv ...`.

## Command-line parameters

**Common** (every stage)

| Flag | Default | Description |
|---|---|---|
| `--type {collect,trim,merge,aggregate,resolve}` | — *(required)* | Which stage to run |
| `-i, --input PATH` | stdin | Input; repeatable for `merge`; ignored by `collect` |
| `-o, --output PATH` | stdout | Output |
| `--delete, --del` | off | After success, delete the `-i` input file(s) (never stdin or `-o`) |
| `--lang {ru,en}` | environment | Force output language |
| `--version` | | Print version and exit |

**`collect`**

| Flag | Default | Description |
|---|---|---|
| `--source NAME` | `DEFAULT_SOURCE` / `graylog` | Data source |
| `--start N` | `1` | How many units back the range starts |
| `--end N` | `0` | How many units back the range ends |
| `--time-unit {d,h}` | `h` | Time unit (days / hours) |

**`aggregate`**

| Flag | Default | Description |
|---|---|---|
| `--streaming / --no-streaming` | `--streaming` | Chunked (bounded memory) vs. full in-memory load |
| `--chunk-size N` | `500000` | Chunk size for `--streaming` |
| `--min-count N` | `MIN_UNIQ_COUNT` / `1` | Drop aggregated groups with count below `N` |
| `--out-dir DIR` | — | Split output into one CSV per subnet (mutually exclusive with `-o`) |

**`resolve`**

| Flag | Default | Description |
|---|---|---|
| `--resolver NAME` | `DEFAULT_RESOLVER` / `default` | `default` / `rdap` / `whois` / `geo_maxmind` / `gunter` |
| `--key-column COL` | `DstIP` | Column holding the IP to resolve |
| `--workers N` | `RESOLVE__WORKERS` / `3` | Number of resolving threads |
| `--cache` / `--no-cache` | `--cache` | Use the persistent resolve cache (backend from `RESOLVE__CACHE`); `--no-cache` disables it for this run |

**`run`** (Variant B, see below)

| Flag | Default | Description |
|---|---|---|
| `--config, -c PATH` | — *(required)* | YAML pipeline config |
| `-i, --input PATH` | stdin | Initial input for the first step |
| `-o, --output PATH` | stdout | Final output |

> **Note:** for `run` these are the *only* command-line flags. Every **stage**
> parameter (`source`, `start`, `min_count`, `out_dir`, `resolver`, `workers`, …)
> is set **inside the YAML config** as `name: {param: value}`, **not** as a CLI
> flag — passing e.g. `--out-dir` to `pyresolv run` is an error.

## Single-process pipeline (pyresolv run)

Besides shell-pipe composition (above, **Variant A**), there is **Variant B** —
the entire pipeline runs in a **single process** from a YAML config. Between
steps flows a live `pandas.DataFrame`, without CSV reserialization at each
junction.

```bash
pyresolv run --config pipeline.yaml -o out.csv
```

```yaml
# pipeline.yaml — a list of steps. A step is either a stage name (`trim`),
# or a mapping "name: {params}".
- collect: {source: graylog, start: 5, end: 0, time_unit: h}
- trim
- aggregate: {min_count: 20}
- resolve: {resolver: gunter}
```

**Stage parameters are set here, in the YAML — not as CLI flags.** Each step's
parameters mirror the Variant A stage flags (`--start` → `start`, etc.):

| Step | YAML params |
|---|---|
| `collect` | `source`, `start`, `end`, `time_unit` |
| `trim` | *(none)* |
| `merge` | `inputs` (list of CSV paths) |
| `aggregate` | `min_count`, `out_dir`, `start`, `end`, `time_unit` |
| `resolve` | `resolver`, `key_column`, `workers` |

For example, `aggregate --out-dir DIR` from Variant A becomes
`- aggregate: {out_dir: DIR, start: 5, end: 0, time_unit: h}` here (it needs
`GRAYLOG__SRC_IP_CIDR` set, and `start`/`end`/`time_unit` feed the filenames' time slice).

- `-i/--input` — initial input for the first step (default stdin);
  ignored if the first step is `collect` (it generates data itself).
- `-o/--output` — final output (default stdout); ignored when a step wrote to `out_dir`.
- Step parameters are strictly validated (`extra="forbid"`): a typo like
  `min_counts` fails with a clear error **before** execution, not in the middle.
- One shared log/progress: each step prints `[i/n] <step>` to stderr.

**When to use what:**

- **Variant A** (shell pipes `--type ... | --type ...`) — streaming, with
  bounded memory (`aggregate --streaming`), for very large files and one-off
  command-line experiments; steps can be rerun independently, intermediate
  results stay on disk.
- **Variant B** (`run --config`) — faster (no reserialization between
  steps) and with a single config, but holds the dataset **entirely in memory**.
  Suitable for data that fits in RAM; for gigantic inputs use Variant A
  with streaming.

Both variants use the same stages and produce identical results:
`run` with `trim -> aggregate` matches byte-for-byte with
`trim | aggregate --no-streaming`.

Requires `PyYAML` (included in `requirements.txt`).

## Deleting intermediate files (--delete/--del)

The `--delete` flag (alias `--del`) is available on any stage: after
**successful** output write, the stage deletes its input `-i` files. This way,
stepping through files stage by stage leaves only the final result on disk:

```bash
pyresolv --type collect                        -o connections.csv
pyresolv --type trim      --del -i connections.csv -o trimmed.csv    # deletes connections.csv
pyresolv --type aggregate --del -i trimmed.csv     -o aggregated.csv # deletes trimmed.csv
# only aggregated.csv is left on disk
```

Safety guarantees:

- deletion only happens after successful stage completion; if the stage fails,
  the input remains untouched;
- never deletes stdin or the output file (case `-i` == `-o` is skipped);
- does not apply to `collect` (input is not used);
- each deletion is logged to stderr.

## Output language (RU/EN)

All messages, errors, and help are localized via gettext. Language is chosen
in order of priority:

1. flag `--lang {ru,en}`;
2. environment — `LANGUAGE` / `LC_ALL` / `LC_MESSAGES` / `LANG`;
3. English (default).

```bash
pyresolv --lang ru --type aggregate -i trimmed.csv -o out.csv   # Russian output
LANG=ru_RU.UTF-8 pyresolv --type aggregate -i trimmed.csv        # also Russian
pyresolv --type aggregate -i trimmed.csv                         # English
```

### Adding or fixing a translation

Strings in the code are in English (that's the `msgid`), Russian translations
live in `po/ru.po` and compile to `pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo`.

```bash
# 1. (optional) extract new strings from the code into the template — needs Babel:
./.venv/bin/pip install -e '.[i18n]'
make i18n-extract       # updates po/pyresolv.pot
make i18n-update        # merges new strings into po/ru.po

# 2. fill in translations in po/ru.po, then compile (no external dependencies):
make i18n-compile       # -> pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo
```

Compilation of `.po → .mo` uses the built-in `tools/msgfmt.py`, so GNU gettext
and Babel are **not required** for execution and compilation — Babel is only
needed for string extraction (`i18n-extract`). To add a new language — create
`po/<lang>.po`, add it to `LANGS` in the `Makefile`, and run `make i18n-compile`.
