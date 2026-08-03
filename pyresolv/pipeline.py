"""Dispatcher `--type` -> stage. Each run_* function takes the parsed
argparse.Namespace and runs the corresponding stage from pyresolv.stages.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers.base import get_resolver
from pyresolv.resolvers.cache import NullCache, get_cache
from pyresolv.sources.base import get_source
from pyresolv.stages.aggregate import aggregate
from pyresolv.subnets import parse_cidrs
from pyresolv.stages.collect import collect
from pyresolv.stages.merge import merge
from pyresolv.stages.trim import trim


def run_collect(args: argparse.Namespace) -> int:
    source_name = args.source or get_settings().default_source
    source = get_source(source_name)
    return collect(
        source=source,
        output_path=args.output,
        start=args.start,
        end=args.end,
        time_unit=args.time_unit,
    )


def run_trim(args: argparse.Namespace) -> int:
    return trim(
        input_path=args.input[0] if args.input else None,
        output_path=args.output,
    )


def run_merge(args: argparse.Namespace) -> int:
    return merge(
        input_paths=list(args.input) if args.input else [],
        output_path=args.output,
    )


def run_aggregate(args: argparse.Namespace) -> int:
    settings = get_settings()
    min_count = args.min_count if args.min_count is not None else settings.min_uniq_count

    out_dir = getattr(args, "out_dir", None)
    networks = None
    if out_dir:
        if args.output:
            raise ValueError(_("--out-dir and -o/--output are mutually exclusive"))
        cidrs = settings.graylog.src_ip_cidr if settings.graylog else []
        networks = parse_cidrs(cidrs)
        if not networks:
            raise ValueError(_(
                "--out-dir needs subnets: set GRAYLOG__SRC_IP_CIDR in .env "
                "(see .env.example)."
            ))

    return aggregate(
        input_path=args.input[0] if args.input else None,
        output_path=args.output,
        streaming=args.streaming,
        chunk_size=args.chunk_size,
        min_count=min_count,
        out_dir=out_dir,
        networks=networks,
        start=args.start,
        end=args.end,
        time_unit=args.time_unit,
    )


def run_resolve(args: argparse.Namespace) -> int:
    settings = get_settings()
    resolver_name = args.resolver or settings.default_resolver
    resolver = get_resolver(resolver_name)
    max_workers = args.workers if args.workers is not None else settings.resolve.workers
    cache = get_cache(settings.resolve.cache, settings.resolve) if args.cache else NullCache()
    return resolver.resolve(
        input_path=args.input[0] if args.input else None,
        output_path=args.output,
        key_column=args.key_column,
        max_workers=max_workers,
        cache=cache,
    )


DISPATCH = {
    "collect": run_collect,
    "trim": run_trim,
    "merge": run_merge,
    "aggregate": run_aggregate,
    "resolve": run_resolve,
}


def _delete_inputs(args: argparse.Namespace) -> None:
    """Delete the -i input files after the stage completes successfully (--delete).

    Safety: only real files passed via -i are deleted. We never touch stdin
    ('-'/no path) or the -o output file. collect ignores its input, so there is
    nothing to delete.
    """
    if not getattr(args, "delete", False):
        return

    if args.type == "collect":
        print(_("Warning: --delete is not applied for collect (input is unused)."), file=sys.stderr)
        return

    inputs = args.input or []
    out = args.output
    out_resolved = Path(out).resolve() if out and out != "-" else None

    for path in inputs:
        if path is None or path == "-":
            continue
        p = Path(path)
        if not p.is_file():
            continue
        if out_resolved is not None and p.resolve() == out_resolved:
            print(_("Skipping deletion of %(path)s: it is the same as the output file.") % {"path": p}, file=sys.stderr)
            continue
        try:
            p.unlink()
            print(_("Deleted input file: %(path)s") % {"path": p}, file=sys.stderr)
        except OSError as e:
            print(_("Failed to delete %(path)s: %(err)s") % {"path": p, "err": e}, file=sys.stderr)


def dispatch(args: argparse.Namespace) -> int:
    handler = DISPATCH[args.type]
    result = handler(args)
    # Delete inputs only after the stage returns successfully — if the handler
    # raises, the exception propagates and the input data is left untouched.
    _delete_inputs(args)
    return result
