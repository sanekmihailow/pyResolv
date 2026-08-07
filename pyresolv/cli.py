"""pyresolv entry point: argparse + dispatch into pyresolv.pipeline.

Stages are independent Unix filters connected by shell pipes, e.g.:

    pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \\
      | pyresolv --type trim \\
      | pyresolv --type aggregate \\
      | pyresolv --type resolve --resolver gunter -o out.csv
"""
from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from pyresolv import __version__
from pyresolv import i18n
from pyresolv.config import ConfigError
from pyresolv.i18n import _
from pyresolv.logfile import tee_stderr
from pyresolv.pipeline import dispatch
from pyresolv.runner import run_pipeline
from pyresolv.schema import DEFAULT_AGGREGATE_CHUNKSIZE, DEFAULT_KEY_COLUMN

LANG_CHOICES = ["ru", "en"]

# `run` step-param override flags -> forwarded to run_pipeline, applied on top of
# the YAML per step that has the field (dest names match the runner param models).
_RUN_OVERRIDE_KEYS = (
    "source", "start", "end", "time_unit", "min_count",
    "out_dir", "resolver", "key_column", "workers", "cache",
)


def _add_lang_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lang",
        choices=LANG_CHOICES,
        default=None,
        help=_(
            "Force output language (overrides LANG/LC_MESSAGES); "
            "by default taken from the environment, otherwise English"
        ),
    )


def _add_log_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=_(
            "Also append all status/progress output (stderr) to PATH, with a "
            "timestamp per line — handy for cron runs. The terminal still shows "
            "live progress; stdout (the CSV data) is never written to the log."
        ),
    )


def _preparse_lang(argv) -> str | None:
    """Extract --lang BEFORE building the main parser, so help text and errors
    are built in the chosen language (a chicken-and-egg problem)."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", choices=LANG_CHOICES, default=None)
    ns, _rest = pre.parse_known_args(argv)
    return ns.lang


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyresolv",
        description=_(
            "pyResolv — process firewall logs with a set of filter stages "
            "(collect/trim/merge/aggregate/resolve) composed via shell pipes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"pyresolv {__version__}")
    _add_lang_argument(parser)
    _add_log_file_argument(parser)

    parser.add_argument(
        "--type",
        required=True,
        choices=["collect", "trim", "merge", "aggregate", "resolve"],
        help=_("Which stage to run"),
    )
    parser.add_argument(
        "-i", "--input",
        action="append",
        default=None,
        metavar="PATH",
        help=_(
            "Input; stdin by default. Ignored for collect (the source generates "
            "data itself). May be given multiple times for merge."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="PATH",
        help=_("Output; stdout by default."),
    )
    parser.add_argument(
        "--delete", "--del",
        action="store_true",
        dest="delete",
        help=_(
            "After output is written successfully, delete the -i input file(s), "
            "leaving only the result. Never touches stdin or the output file; "
            "not applied for collect (input is unused)."
        ),
    )

    collect_group = parser.add_argument_group("collect")
    collect_group.add_argument(
        "--source",
        default=None,
        help=_("Data source (default: from config, otherwise 'graylog')"),
    )
    collect_group.add_argument(
        "--start",
        type=int,
        default=1,
        help=_("How many units back the overall range starts, default 1"),
    )
    collect_group.add_argument(
        "--end",
        type=int,
        default=0,
        help=_("How many units back the overall range ends, default 0"),
    )
    collect_group.add_argument(
        "--time-unit",
        choices=["d", "h"],
        default="h",
        help=_("Time unit: d or h, default h"),
    )

    aggregate_group = parser.add_argument_group("aggregate")
    aggregate_group.add_argument(
        "--streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_(
            "Chunked aggregation (default): bounded memory, safe for very large "
            "files. Use --no-streaming to force the full in-memory load (faster "
            "for small files, but can exhaust RAM on huge ones)."
        ),
    )
    aggregate_group.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_AGGREGATE_CHUNKSIZE,
        help=_("Chunk size for --streaming, default %(size)s") % {"size": f"{DEFAULT_AGGREGATE_CHUNKSIZE:,}"},
    )
    aggregate_group.add_argument(
        "--min-count",
        type=int,
        default=None,
        help=_(
            "Drop aggregated groups whose count is below this threshold "
            "(default: from config MIN_UNIQ_COUNT, otherwise 1 = keep all)."
        ),
    )
    aggregate_group.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help=_(
            "Instead of a single -o file, split the aggregation into one CSV per "
            "subnet (from GRAYLOG__SRC_IP_CIDR) in DIR; unmatched rows go to an "
            "'other' file. Filenames carry the time slice from --start/--end/"
            "--time-unit. Mutually exclusive with -o/--output."
        ),
    )

    resolve_group = parser.add_argument_group("resolve")
    resolve_group.add_argument(
        "--resolver",
        default=None,
        help=_(
            "Resolver: default | rdap | whois | geo_maxmind | gunter "
            "(default: from config, otherwise 'default' = chain GEO->RDAP->WHOIS)"
        ),
    )
    resolve_group.add_argument(
        "--key-column",
        default=DEFAULT_KEY_COLUMN,
        help=_("Key column for resolving, default %(col)s") % {"col": DEFAULT_KEY_COLUMN},
    )
    resolve_group.add_argument(
        "--workers",
        type=int,
        default=None,
        help=_("Number of resolving threads (default: from the resolver config)"),
    )
    resolve_group.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_(
            "Use the persistent resolve cache (default). The backend is chosen by "
            "RESOLVE__CACHE in .env (default/redis/none); --no-cache disables caching "
            "for this run."
        ),
    )

    return parser


def build_run_parser() -> argparse.ArgumentParser:
    """Parser for the `run` subcommand (Variant B: single-process pipeline)."""
    parser = argparse.ArgumentParser(
        prog="pyresolv run",
        description=_(
            "Run a whole pipeline in one process from a YAML config, with a live "
            "DataFrame flowing between steps (no CSV re-serialization)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"pyresolv {__version__}")
    _add_lang_argument(parser)
    _add_log_file_argument(parser)
    parser.add_argument(
        "--config", "-c",
        required=True,
        metavar="PATH",
        help=_("YAML pipeline config: a list of steps (see README)."),
    )
    parser.add_argument(
        "-i", "--input",
        default=None,
        metavar="PATH",
        help=_(
            "Initial input for the first step (stdin by default). Ignored when "
            "the first step is collect (it produces data itself)."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="PATH",
        help=_("Final output; stdout by default."),
    )

    # Stage-parameter overrides: when given, they win over the YAML step value
    # (precedence CLI > YAML > ENV/config default). Each applies only to steps
    # that have that param (e.g. --out-dir -> aggregate, --resolver -> resolve,
    # --start -> collect & aggregate). All default None = "not overridden".
    override_group = parser.add_argument_group(
        "step overrides", _("Override YAML step params (CLI wins over the pipeline file)")
    )
    override_group.add_argument("--source", default=None, help=_("Override collect 'source'"))
    override_group.add_argument("--start", type=int, default=None, help=_("Override collect/aggregate 'start'"))
    override_group.add_argument("--end", type=int, default=None, help=_("Override collect/aggregate 'end'"))
    override_group.add_argument(
        "--time-unit", choices=["d", "h"], default=None, help=_("Override collect/aggregate 'time_unit'")
    )
    override_group.add_argument("--min-count", type=int, default=None, help=_("Override aggregate 'min_count'"))
    override_group.add_argument("--out-dir", default=None, metavar="DIR", help=_("Override aggregate 'out_dir'"))
    override_group.add_argument("--resolver", default=None, help=_("Override resolve 'resolver'"))
    override_group.add_argument("--key-column", default=None, help=_("Override resolve 'key_column'"))
    override_group.add_argument("--workers", type=int, default=None, help=_("Override resolve 'workers'"))
    override_group.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=_("Override resolve 'cache' (--cache/--no-cache)"),
    )

    parser.add_argument(
        "--streaming",
        action="store_true",
        default=False,
        help=_(
            "Bounded-memory engine: chain the stages through temporary CSV files "
            "instead of holding the whole DataFrame in memory — safe for very large "
            "inputs (the default in-memory engine can exhaust RAM). Temp dir from "
            "STREAMING__TEMP_LOG_PATH (system temp by default; put it on real disk, "
            "not a tmpfs). Put 'resolve' after 'aggregate' so it runs on the small "
            "grouped result."
        ),
    )
    return parser


def _format_validation_error(exc: ValidationError) -> str:
    lines = [_("Configuration error (%(title)s):") % {"title": exc.title}]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        lines.append("  - %(loc)s: %(msg)s" % {"loc": loc, "msg": error["msg"]})
    lines.append(_("Check .env (see .env.example)."))
    return "\n".join(lines)


def main() -> None:
    argv = sys.argv[1:]
    # Language selection before building the parser: --lang -> environment -> English.
    i18n.setup(_preparse_lang(argv))

    # `run` subcommand (Variant B) is routed separately, so the classic
    # `--type`-based interface (Variant A) stays exactly as before.
    if argv and argv[0] == "run":
        args = build_run_parser().parse_args(argv[1:])
        overrides = {k: getattr(args, k) for k in _RUN_OVERRIDE_KEYS if getattr(args, k, None) is not None}
        with tee_stderr(args.log_file):
            _run_guarded(lambda: run_pipeline(
                args.config, args.input, args.output, overrides, streaming=args.streaming,
            ))
        return

    parser = build_parser()
    args = parser.parse_args(argv)
    with tee_stderr(args.log_file):
        _run_guarded(lambda: dispatch(args))


def _run_guarded(action) -> None:
    try:
        action()
    except ValidationError as e:
        print(_format_validation_error(e), file=sys.stderr)
        sys.exit(2)
    except ConfigError as e:
        print(_("Configuration error: %(err)s") % {"err": e}, file=sys.stderr)
        sys.exit(2)
    except (ValueError, FileNotFoundError) as e:
        print(_("Error: %(err)s") % {"err": e}, file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        # Normal in a shell pipe when the next stage exits early
        # (e.g. `| head`) — not treated as an error.
        sys.exit(0)


if __name__ == "__main__":
    main()
