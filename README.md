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
  groups with `count` below the threshold.
- **resolve** — enriches rows by key column (`--key-column`, default `DstIP`)
  via a resolver (`--resolver`, default `gunter`): adds `country`, `asn`,
  `asn_descr`, `contacts`. Idempotent: already-filled rows are not resolved
  again and do not make network calls.

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
│   │   └── gunter.py            geo-lookup + whois HTTP calls
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
- collect: {source: graylog, start: 5, end: 0}
- trim
- aggregate: {min_count: 20}
- resolve: {resolver: gunter}
```

- `-i/--input` — initial input for the first step (default stdin);
  ignored if the first step is `collect` (it generates data itself).
- `-o/--output` — final output (default stdout).
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
