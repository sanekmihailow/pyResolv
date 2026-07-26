"""The `aggregate` stage: group by GROUP_COLS + count, sorted count desc,
then ac_action/SrcIP/DstIP/DstPort asc.

Two modes:
  - default — --streaming: chunked reading with bounded memory
    (safe for very large files, tens/hundreds of millions of rows);
  - --no-streaming — full pandas load (fast, vectorized; loads the whole file
    into memory, can exhaust RAM on large inputs);
  - --streaming — chunked reading (pandas read_csv(chunksize=...)):
    compute group-by/count on each chunk separately, then sum the partial
    counts by identical keys. Shows a tqdm progress bar over the input file's
    bytes (f.tell()), like trim; for stdin (no real file) the bar advances by
    the number of rows read.

min_count filter (env MIN_UNIQ_COUNT / --min-count flag): groups with
count < min_count are dropped from the result. Applied AFTER the full
aggregation (in streaming a group's final count is only known once all chunks
are summed) — identically in both modes, so the byte-identical guarantee
holds. min_count=1 -> no filtering.

KEY REQUIREMENT: both modes must produce byte-identical output on the same
input — the same set of groups, the same count, the same sort order, the same
column dtypes, the same handling of empty values. This is guaranteed by:
  - both modes reading the CSV with the same PANDAS_READ_KWARGS (dtype=str,
    keep_default_na=False) — the grouping columns are strings everywhere, so
    sorting and aggregation don't depend on how pandas would "guess" the type
    in a particular chunk;
  - in streaming mode partial groups are summed (`.sum()`) by the same keys,
    which is mathematically equivalent to `.size()` on the full dataframe;
  - the final sort (`_finalize`) is shared code for both modes.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

import pandas as pd
from tqdm import tqdm

from pyresolv.i18n import _, ngettext
from pyresolv.io import open_input, open_output
from pyresolv.schema import (
    DEFAULT_AGGREGATE_CHUNKSIZE,
    GROUP_COLS,
    PANDAS_READ_KWARGS,
    SORT_CANDIDATES,
)


def _sort_cols_and_order(present_groups: List[str]) -> Tuple[List[str], List[bool]]:
    sort_cols = [c for c in SORT_CANDIDATES if c in present_groups + ["count"]]
    ascending = [c != "count" for c in sort_cols]
    return sort_cols, ascending


def _finalize(result: pd.DataFrame, present_groups: List[str]) -> pd.DataFrame:
    sort_cols, ascending = _sort_cols_and_order(present_groups)
    return (
        result.sort_values(by=sort_cols, ascending=ascending)
        .reset_index(drop=True)
    )


def _apply_min_count(result: pd.DataFrame, min_count: int) -> pd.DataFrame:
    """Drop groups with count < min_count (see the module docstring). Shared by
    the path-based `aggregate` and the in-memory `aggregate_frame`."""
    if min_count <= 1:
        return result
    before = len(result)
    result = result[result["count"] >= min_count].reset_index(drop=True)
    dropped = before - len(result)
    print(
        ngettext(
            "Dropped %(n)s group with count < %(min)s",
            "Dropped %(n)s groups with count < %(min)s",
            dropped,
        )
        % {"n": f"{dropped:,}", "min": f"{min_count:,}"},
        file=sys.stderr,
    )
    return result


def aggregate_frame(df: pd.DataFrame, min_count: int = 1) -> pd.DataFrame:
    """In-memory aggregation for the single-process pipeline engine (Variant B):
    the full-load path (group-by + count + finalize + min_count) applied to an
    already-loaded DataFrame — no CSV read/write. Byte-identical to the
    path-based full-load mode for the same input."""
    present_groups = [c for c in GROUP_COLS if c in df.columns]
    if not present_groups:
        raise ValueError(_("CSV has no columns to GROUP BY"))
    result = (
        df.groupby(present_groups, dropna=False)
        .size()
        .reset_index(name="count")
    )
    result = _finalize(result, present_groups)
    return _apply_min_count(result, min_count)


def _aggregate_full(in_f) -> Tuple[pd.DataFrame, List[str]]:
    df = pd.read_csv(in_f, **PANDAS_READ_KWARGS)

    present_groups = [c for c in GROUP_COLS if c in df.columns]
    if not present_groups:
        raise ValueError(_("CSV has no columns to GROUP BY"))

    result = (
        df.groupby(present_groups, dropna=False)
        .size()
        .reset_index(name="count")
    )
    return result, present_groups


def _aggregate_streaming(
    in_f, chunk_size: int, file_size: Optional[int], desc: str
) -> Tuple[pd.DataFrame, List[str]]:
    present_groups: Optional[List[str]] = None
    partials: List[pd.DataFrame] = []
    last_pos = 0

    with tqdm(total=file_size, unit="B", unit_scale=True, desc=desc) as pbar:
        reader = pd.read_csv(in_f, chunksize=chunk_size, **PANDAS_READ_KWARGS)
        for chunk in reader:
            if present_groups is None:
                present_groups = [c for c in GROUP_COLS if c in chunk.columns]
                if not present_groups:
                    raise ValueError(_("CSV has no columns to GROUP BY"))

            partial = (
                chunk.groupby(present_groups, dropna=False)
                .size()
                .reset_index(name="count")
            )
            partials.append(partial)

            if file_size is not None:
                try:
                    current_pos = in_f.tell()
                except (OSError, ValueError):
                    current_pos = last_pos
                pbar.update(max(0, current_pos - last_pos))
                last_pos = current_pos
            else:
                pbar.update(len(chunk))

    if present_groups is None:
        raise ValueError(_("Input CSV is empty"))

    combined = pd.concat(partials, ignore_index=True)
    result = (
        combined.groupby(present_groups, dropna=False)["count"]
        .sum()
        .reset_index()
    )
    return result, present_groups


def aggregate(
    input_path: Optional[str],
    output_path: Optional[str],
    streaming: bool = True,
    chunk_size: int = DEFAULT_AGGREGATE_CHUNKSIZE,
    min_count: int = 1,
) -> int:
    is_real_file = input_path is not None and input_path != "-"
    file_size = os.path.getsize(input_path) if is_real_file else None
    desc = (
        (_("Aggregating %(path)s") % {"path": input_path})
        if is_real_file
        else _("Aggregating")
    )

    with open_input(input_path) as in_f:
        if streaming:
            result, present_groups = _aggregate_streaming(
                in_f, chunk_size, file_size, desc
            )
        else:
            result, present_groups = _aggregate_full(in_f)

    result = _finalize(result, present_groups)

    # The count threshold filter is applied AFTER the full aggregation: in
    # streaming a group's final count is only known once all chunks are summed,
    # so per-chunk filtering isn't possible. Identical for both modes ->
    # the result stays byte-identical.
    result = _apply_min_count(result, min_count)

    with open_output(output_path) as out_f:
        result.to_csv(out_f, index=False)

    print(
        ngettext("Aggregated %(n)s row", "Aggregated %(n)s rows", len(result))
        % {"n": f"{len(result):,}"},
        file=sys.stderr,
    )
    return len(result)
