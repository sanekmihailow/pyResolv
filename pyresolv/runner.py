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

import sys
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
from pyresolv.schema import DEFAULT_KEY_COLUMN
from pyresolv.sources.base import get_source
from pyresolv.stages.aggregate import aggregate_frame, write_split_by_subnet
from pyresolv.stages.collect import collect_frame
from pyresolv.stages.merge import merge_frames, read_frame
from pyresolv.stages.trim import trim_frame
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


def run_pipeline(
    config_path: str,
    input_path: Optional[str] = None,
    output_path: Optional[str] = None,
    overrides: Optional[Dict[str, object]] = None,
) -> int:
    """Run the whole pipeline in one process. Returns the number of rows in the
    final frame. `overrides` are CLI step-param values that win over the YAML for
    every step that has the field (precedence CLI > YAML > ENV/config default)."""
    steps = load_pipeline_config(config_path)
    settings = get_settings()
    overrides = overrides or {}
    total = len(steps)

    frame: Optional[pd.DataFrame] = None
    if steps[0][0] in _NEEDS_INPUT:
        frame = read_frame(input_path)

    wrote_to_dir = False
    for idx, (name, raw) in enumerate(steps, start=1):
        model_cls, runner = STEP_TABLE[name]
        merged = dict(raw)
        # CLI overrides win over the YAML, but only for params this step has.
        merged.update({k: v for k, v in overrides.items() if k in model_cls.model_fields})
        try:
            params = model_cls(**merged)
        except ValidationError as e:
            raise ValueError(
                _("Invalid params for step '%(step)s': %(err)s")
                % {"step": name, "err": _short_validation(e)}
            ) from None
        print(_("[%(i)d/%(n)d] %(step)s") % {"i": idx, "n": total, "step": name}, file=sys.stderr)
        frame = runner(frame, params, settings)
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


def _short_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "?"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)
