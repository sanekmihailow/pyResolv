"""The `merge` stage: concatenates several CSV inputs into a single stream on
stdout/-o, taking the header from the first non-empty input.

Unlike get_dst_ip_ranges.py, where merge_files_by_creation_time scanned the
`connections_*.csv` directory itself and sorted files by creation time, here the
concatenation order is the order of the given `-i` args (the stage works as a
narrow Unix filter over explicitly listed inputs/stdin, with no knowledge of
directories or file names; selecting and sorting files is the caller's job,
e.g. the shell).
"""
from __future__ import annotations

import csv
from typing import List, Optional

import pandas as pd

from pyresolv.i18n import _
from pyresolv.io import open_input, open_output
from pyresolv.schema import PANDAS_READ_KWARGS


def merge_frames(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """In-memory merge for the single-process pipeline engine (Variant B):
    concatenate several DataFrames, keeping the columns of the first non-empty
    one. Empty frames (no rows) are skipped, mirroring the path-based merge that
    takes the header from the first non-empty input."""
    non_empty = [df for df in frames if df is not None and not df.empty]
    if not non_empty:
        raise ValueError(_("Could not find any non-empty CSV input for merge"))
    columns = list(non_empty[0].columns)
    aligned = [df.reindex(columns=columns, fill_value="") for df in non_empty]
    return pd.concat(aligned, ignore_index=True)


def read_frame(path: Optional[str]) -> pd.DataFrame:
    """Read a CSV path/stdin into a DataFrame using the shared PANDAS_READ_KWARGS
    (dtype=str, keep_default_na=False), so pipeline steps see the same values as
    the path-based stages."""
    with open_input(path) as in_f:
        return pd.read_csv(in_f, **PANDAS_READ_KWARGS)


def merge(input_paths: List[Optional[str]], output_path: Optional[str]) -> int:
    if not input_paths:
        input_paths = [None]

    header_written = False
    rows_written = 0
    writer = None

    with open_output(output_path) as out_f:
        for path in input_paths:
            with open_input(path) as in_f:
                reader = csv.reader(in_f)
                try:
                    header = next(reader)
                except StopIteration:
                    continue

                if not header_written:
                    writer = csv.writer(out_f)
                    writer.writerow(header)
                    header_written = True

                for row in reader:
                    if row:
                        writer.writerow(row)
                        rows_written += 1

    if not header_written:
        raise ValueError(_("Could not find any non-empty CSV input for merge"))

    return rows_written
