"""Single-process pipeline engine (Variant B).

Instead of composing stages as separate OS processes glued by shell pipes
(Variant A: `pyresolv --type ... | pyresolv --type ...`), this runs a whole
pipeline inside ONE process, described by a small YAML config:

    # pipeline.yaml
    - collect: {source: graylog, start: 5, end: 0}
    - trim
    - aggregate: {min_count: 20}
    - resolve: {resolver: gunter}

    pyresolv run --config pipeline.yaml -o out.csv

A live pandas DataFrame flows between steps — no CSV is re-serialized between
them. Trade-off: the whole dataset is held in memory, so this is meant for
data that fits in RAM. For very large inputs prefer Variant A with
`aggregate --streaming` (see README).

Each step is one entry of the YAML list, either a bare stage name (`trim`) or a
single-key mapping of stage name -> params (`{aggregate: {min_count: 20}}`).
Params are validated with pydantic (`extra="forbid"`), so a typo like
`min_counts` fails fast with a clear message before anything runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError

from pyresolv.config import Settings, get_settings
from pyresolv.i18n import _
from pyresolv.io import open_output
from pyresolv.resolvers.base import get_resolver
from pyresolv.resolvers.cache import NullCache, get_cache
from pyresolv.schema import DEFAULT_AGGREGATE_CHUNKSIZE, DEFAULT_KEY_COLUMN
from pyresolv.sources.base import get_source
from pyresolv.stages.aggregate import aggregate, aggregate_frame, write_split_by_subnet
from pyresolv.stages.collect import collect, collect_frame
from pyresolv.stages.merge import merge, merge_frames, read_frame
from pyresolv.stages.trim import trim, trim_frame
from pyresolv.subnets import parse_cidrs

# Stages that consume the running frame as their input (as opposed to producing
# it themselves): if one of these is the first step, the engine reads the
# initial -i/stdin into a frame before the loop.
_NEEDS_INPUT = {"trim", "aggregate", "resolve"}


class _StepParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectParams(_StepParams):
    source: Optional[str] = None
    start: int = 1
    end: int = 0
    time_unit: Literal["d", "h"] = "h"


class TrimParams(_StepParams):
    pass


class MergeParams(_StepParams):
    inputs: List[str] = []


class AggregateParams(_StepParams):
    min_count: Optional[int] = None
    out_dir: Optional[str] = None
    start: int = 1
    end: int = 0
    time_unit: Literal["d", "h"] = "h"


class ResolveParams(_StepParams):
    resolver: Optional[str] = None
    key_column: str = DEFAULT_KEY_COLUMN
    workers: Optional[int] = None
    cache: bool = True


def _run_collect(frame: Optional[pd.DataFrame], p: CollectParams, s: Settings) -> pd.DataFrame:
    source = get_source(p.source or s.default_source)
    return collect_frame(source, p.start, p.end, p.time_unit)


def _run_trim(frame: Optional[pd.DataFrame], p: TrimParams, s: Settings) -> pd.DataFrame:
    return trim_frame(_require_frame(frame, "trim"))


def _run_merge(frame: Optional[pd.DataFrame], p: MergeParams, s: Settings) -> pd.DataFrame:
    frames: List[pd.DataFrame] = [frame] if frame is not None else []
    frames.extend(read_frame(path) for path in p.inputs)
    if not frames:
        raise ValueError(_("Could not find any non-empty CSV input for merge"))
    return merge_frames(frames)


def _run_aggregate(frame: Optional[pd.DataFrame], p: AggregateParams, s: Settings) -> pd.DataFrame:
    min_count = p.min_count if p.min_count is not None else s.min_uniq_count
    result = aggregate_frame(_require_frame(frame, "aggregate"), min_count=min_count)
    if p.out_dir:
        cidrs = s.graylog.src_ip_cidr if s.graylog else []
        networks = parse_cidrs(cidrs)
        if not networks:
            raise ValueError(_(
                "--out-dir needs subnets: set GRAYLOG__SRC_IP_CIDR in .env "
                "(see .env.example)."
            ))
        write_split_by_subnet(
            result, p.out_dir, networks, "aggregation", datetime.now(),
            p.start, p.end, p.time_unit,
        )
    return result


def _run_resolve(frame: Optional[pd.DataFrame], p: ResolveParams, s: Settings) -> pd.DataFrame:
    resolver_name = p.resolver or s.default_resolver
    resolver = get_resolver(resolver_name)
    max_workers = p.workers if p.workers is not None else s.resolve.workers
    cache = get_cache(s.resolve.cache, s.resolve) if p.cache else NullCache()
    return resolver.enrich(_require_frame(frame, "resolve"), p.key_column, max_workers, cache=cache)


# name -> (params model, runner). Also the source of truth for valid step names.
_StepRunner = Callable[[Optional[pd.DataFrame], _StepParams, Settings], pd.DataFrame]
STEP_TABLE: Dict[str, Tuple[type, _StepRunner]] = {
    "collect": (CollectParams, _run_collect),
    "trim": (TrimParams, _run_trim),
    "merge": (MergeParams, _run_merge),
    "aggregate": (AggregateParams, _run_aggregate),
    "resolve": (ResolveParams, _run_resolve),
}


def _require_frame(frame: Optional[pd.DataFrame], step: str) -> pd.DataFrame:
    if frame is None:
        raise ValueError(
            _("Step '%(step)s' has no input: it is not the first step, and no -i "
              "input was given.") % {"step": step}
        )
    return frame


def load_pipeline_config(path: str) -> List[Tuple[str, dict]]:
    """Parse and validate the YAML pipeline config into a list of
    (step_name, raw_params) tuples. Raises ValueError with a clear message on
    any structural problem (not a list, unknown step, bad step shape)."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - depends on the environment
        raise ValueError(
            _("PyYAML is required for 'pyresolv run'. Install it: pip install pyyaml")
        ) from None

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    if not isinstance(data, list):
        raise ValueError(_("Pipeline config must be a YAML list of steps."))

    steps: List[Tuple[str, dict]] = []
    for item in data:
        if isinstance(item, str):
            name, raw = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, raw = next(iter(item.items()))
            raw = raw or {}
            if not isinstance(raw, dict):
                raise ValueError(
                    _("Params for step '%(step)s' must be a mapping.") % {"step": name}
                )
        else:
            raise ValueError(
                _("Each step must be a stage name or a single-key mapping "
                  "'name: {params}', got: %(item)r") % {"item": item}
            )

        if name not in STEP_TABLE:
            available = ", ".join(STEP_TABLE)
            raise ValueError(
                _("Unknown pipeline step '%(step)s'. Available steps: %(available)s")
                % {"step": name, "available": available}
            )
        steps.append((name, raw))

    if not steps:
        raise ValueError(_("Pipeline config is empty."))
    return steps


def _resolve_step_params(name: str, raw: dict, overrides: Dict[str, object]) -> _StepParams:
    """Build a step's validated params from its YAML + the CLI overrides that win
    over the YAML for the fields this step actually has (CLI > YAML > default).
    Shared by both engines so param handling stays identical."""
    model_cls, _runner = STEP_TABLE[name]
    merged = dict(raw)
    merged.update({k: v for k, v in overrides.items() if k in model_cls.model_fields})
    try:
        return model_cls(**merged)
    except ValidationError as e:
        raise ValueError(
            _("Invalid params for step '%(step)s': %(err)s")
            % {"step": name, "err": _short_validation(e)}
        ) from None


def run_pipeline(
    config_path: str,
    input_path: Optional[str] = None,
    output_path: Optional[str] = None,
    overrides: Optional[Dict[str, object]] = None,
    streaming: bool = False,
) -> int:
    """Run the whole pipeline in one process. Returns the number of rows in the
    final result. `overrides` are CLI step-param values that win over the YAML for
    every step that has the field (precedence CLI > YAML > ENV/config default).

    Two engines: the default in-memory one holds a live DataFrame between steps
    (fast, but the whole dataset lives in RAM); `streaming=True` chains the
    bounded-memory path-based stage functions through temporary CSV files (safe
    for very large inputs). Both produce byte-identical output."""
    steps = load_pipeline_config(config_path)
    settings = get_settings()
    overrides = overrides or {}
    if streaming:
        return _run_streaming(steps, input_path, output_path, overrides, settings)
    return _run_in_memory(steps, input_path, output_path, overrides, settings)


def _run_in_memory(
    steps: List[Tuple[str, dict]],
    input_path: Optional[str],
    output_path: Optional[str],
    overrides: Dict[str, object],
    settings: Settings,
) -> int:
    total = len(steps)

    frame: Optional[pd.DataFrame] = None
    if steps[0][0] in _NEEDS_INPUT:
        frame = read_frame(input_path)

    wrote_to_dir = False
    for idx, (name, raw) in enumerate(steps, start=1):
        params = _resolve_step_params(name, raw, overrides)
        _runner = STEP_TABLE[name][1]
        print(_("[%(i)d/%(n)d] %(step)s") % {"i": idx, "n": total, "step": name}, file=sys.stderr)
        frame = _runner(frame, params, settings)
        # A step with out_dir writes per-subnet files itself -> no single -o file.
        if getattr(params, "out_dir", None):
            wrote_to_dir = True

    if frame is None:  # pragma: no cover - guarded by load_pipeline_config
        raise ValueError(_("Pipeline produced no data."))

    if wrote_to_dir:
        if output_path not in (None, "-"):
            print(_("Note: -o is ignored because a step wrote to --out-dir."), file=sys.stderr)
    else:
        with open_output(output_path) as out_f:
            frame.to_csv(out_f, index=False)

    print(_("Pipeline finished: %(n)s rows") % {"n": f"{len(frame):,}"}, file=sys.stderr)
    return len(frame)


def _run_streaming(
    steps: List[Tuple[str, dict]],
    input_path: Optional[str],
    output_path: Optional[str],
    overrides: Dict[str, object],
    settings: Settings,
) -> int:
    """Bounded-memory engine: chain the path-based stage functions through
    temporary CSV files, so the whole dataset is never held in memory. Reuses the
    same validated params and byte-identical stage cores as the in-memory engine.

    Each step reads the previous step's file and writes the next one; the first
    step's input is the real -i/stdin (or, for collect, nothing), the last step
    writes the real -o/out_dir. The temp dir (STREAMING__TEMP_LOG_PATH, else the
    system temp) is removed on exit, success or failure."""
    total = len(steps)
    temp_root = settings.streaming.temp_log_path or None
    if temp_root:
        os.makedirs(temp_root, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pyresolv-", dir=temp_root) as tmpdir:
        current_path = input_path  # None/'-' means stdin for the first step
        last_rows = 0
        wrote_to_dir = False

        for idx, (name, raw) in enumerate(steps, start=1):
            params = _resolve_step_params(name, raw, overrides)
            is_last = idx == total
            out_dir = getattr(params, "out_dir", None)
            # out_dir writes per-subnet files and yields no single CSV to chain,
            # so it is only valid as the terminal step.
            if out_dir and not is_last:
                raise ValueError(
                    _("Step '%(step)s' uses out_dir but is not the last step: "
                      "--out-dir writes per-subnet files and produces no single "
                      "output to feed the next step. Put it last.") % {"step": name}
                )

            print(_("[%(i)d/%(n)d] %(step)s") % {"i": idx, "n": total, "step": name}, file=sys.stderr)
            step_out = output_path if is_last else os.path.join(tmpdir, f"{idx}_{name}.csv")
            last_rows = _run_streaming_step(name, params, settings, current_path, step_out)

            if out_dir:
                wrote_to_dir = True  # terminal; nothing to chain further
            else:
                current_path = step_out

        if wrote_to_dir and output_path not in (None, "-"):
            print(_("Note: -o is ignored because a step wrote to --out-dir."), file=sys.stderr)

        print(_("Pipeline finished: %(n)s rows") % {"n": f"{last_rows:,}"}, file=sys.stderr)
        return last_rows


def _run_streaming_step(
    name: str,
    params: _StepParams,
    settings: Settings,
    in_path: Optional[str],
    out_path: Optional[str],
) -> int:
    """Dispatch one streaming step to its bounded-memory path-based stage function.
    Mirrors the param handling of the `_run_*` frame runners and `pipeline.run_*`."""
    if name == "collect":
        source = get_source(params.source or settings.default_source)
        return collect(source, out_path, params.start, params.end, params.time_unit)

    if name == "trim":
        return trim(in_path, out_path)

    if name == "merge":
        inputs: List[Optional[str]] = [in_path] if in_path is not None else []
        inputs.extend(params.inputs)
        if not inputs:
            raise ValueError(_("Could not find any non-empty CSV input for merge"))
        return merge(inputs, out_path)

    if name == "aggregate":
        min_count = params.min_count if params.min_count is not None else settings.min_uniq_count
        networks = None
        agg_out: Optional[str] = out_path
        if params.out_dir:
            cidrs = settings.graylog.src_ip_cidr if settings.graylog else []
            networks = parse_cidrs(cidrs)
            if not networks:
                raise ValueError(_(
                    "--out-dir needs subnets: set GRAYLOG__SRC_IP_CIDR in .env "
                    "(see .env.example)."
                ))
            agg_out = None  # out_dir and output are mutually exclusive
        return aggregate(
            input_path=in_path,
            output_path=agg_out,
            streaming=True,
            chunk_size=DEFAULT_AGGREGATE_CHUNKSIZE,
            min_count=min_count,
            out_dir=params.out_dir,
            networks=networks,
            start=params.start,
            end=params.end,
            time_unit=params.time_unit,
        )

    if name == "resolve":
        resolver = get_resolver(params.resolver or settings.default_resolver)
        max_workers = params.workers if params.workers is not None else settings.resolve.workers
        cache = get_cache(settings.resolve.cache, settings.resolve) if params.cache else NullCache()
        return resolver.resolve(in_path, out_path, params.key_column, max_workers, cache=cache)

    raise ValueError(_("Unknown pipeline step '%(step)s'.") % {"step": name})  # pragma: no cover


def _short_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "?"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)
