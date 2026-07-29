"""The `trim` stage: drops the service columns (DROP_COLS), reading the input
in chunks so multi-gigabyte files aren't loaded into memory all at once.

Ported from trim_csv in get_dst_ip_ranges.py/resolver.py, fixing a progress-bar
bug: `total` used to be the file size in bytes while `update()` was called with
the in-memory chunk size (chunk.memory_usage(deep=True).sum()) — the units
didn't match, so the progress bar didn't reflect actual progress through the
file. Now total and update measure the same thing: the offset into the input
file in bytes (`f.tell()`), read after each chunk.
"""
from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from tqdm import tqdm

from pyresolv.i18n import _
from pyresolv.io import open_input, open_output
from pyresolv.schema import DEFAULT_TRIM_CHUNKSIZE, DROP_COLS, PANDAS_READ_KWARGS


def trim_frame(df: pd.DataFrame) -> pd.DataFrame:
    """In-memory trim for the single-process pipeline engine (Variant B): drop
    the DROP_COLS present in an already-loaded DataFrame — no CSV read/write."""
    drop_cols = [c for c in DROP_COLS if c in df.columns]
    return df.drop(columns=drop_cols) if drop_cols else df


def trim(
    input_path: Optional[str],
    output_path: Optional[str],
    chunksize: int = DEFAULT_TRIM_CHUNKSIZE,
) -> int:
    is_real_file = input_path is not None and input_path != "-"
    file_size = os.path.getsize(input_path) if is_real_file else None
    desc = (_("Trimming %(path)s") % {"path": input_path}) if is_real_file else _("Trimming")

    rows_written = 0
    first_chunk = True
    last_pos = 0

    with open_input(input_path) as in_f, open_output(output_path) as out_f:
        with tqdm(total=file_size, unit="B", unit_scale=True, desc=desc) as pbar:
            reader = pd.read_csv(in_f, chunksize=chunksize, **PANDAS_READ_KWARGS)

            for chunk in reader:
                drop_cols = [c for c in DROP_COLS if c in chunk.columns]
                if drop_cols:
                    chunk = chunk.drop(columns=drop_cols)

                chunk.to_csv(out_f, header=first_chunk, index=False)
                first_chunk = False
                rows_written += len(chunk)

                if file_size is not None:
                    try:
                        current_pos = in_f.tell()
                    except (OSError, ValueError):
                        current_pos = last_pos
                    pbar.update(max(0, current_pos - last_pos))
                    last_pos = current_pos
                else:
                    pbar.update(len(chunk))

    return rows_written
