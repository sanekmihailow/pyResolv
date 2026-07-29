"""The `collect` stage: pulls records from the source (--source) by time
windows and writes them as CSV to stdout/-o. The source generates data itself,
so -i is ignored.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from typing import Optional

import pandas as pd

from pyresolv.i18n import _, ngettext
from pyresolv.io import open_output
from pyresolv.schema import COLLECT_COLUMNS
from pyresolv.sources.base import Source, build_time_windows


def collect_frame(
    source: Source,
    start: int,
    end: int,
    time_unit: str,
) -> pd.DataFrame:
    """In-memory collect for the single-process pipeline engine (Variant B):
    pulls records from the source into a DataFrame instead of writing CSV.
    Columns match COLLECT_COLUMNS; all values are strings (dtype=str) so the
    downstream stages behave exactly as when reading via PANDAS_READ_KWARGS."""
    base_now = datetime.now()
    windows = build_time_windows(start, end, time_unit)
    print(_("Base time for window calculation: %(ts)s") % {"ts": base_now.strftime("%Y-%m-%d %H:%M:%S")}, file=sys.stderr)
    print(ngettext("Total: %(n)d window", "Total: %(n)d windows", len(windows)) % {"n": len(windows)}, file=sys.stderr)

    rows = list(source.fetch(windows))
    df = pd.DataFrame(rows, columns=COLLECT_COLUMNS, dtype=str)
    # An empty result still yields the right columns; fill any gaps with "".
    df = df.fillna("").astype(str)
    print(ngettext("Wrote %(n)d row", "Wrote %(n)d rows", len(df)) % {"n": len(df)}, file=sys.stderr)
    return df


def collect(
    source: Source,
    output_path: Optional[str],
    start: int,
    end: int,
    time_unit: str,
) -> int:
    base_now = datetime.now()
    windows = build_time_windows(start, end, time_unit)

    # Status messages go to stderr: stdout is the CSV pipe between stages.
    print(_("Base time for window calculation: %(ts)s") % {"ts": base_now.strftime("%Y-%m-%d %H:%M:%S")}, file=sys.stderr)
    print(ngettext("Total: %(n)d window", "Total: %(n)d windows", len(windows)) % {"n": len(windows)}, file=sys.stderr)

    written_rows = 0

    with open_output(output_path) as out_f:
        writer = csv.DictWriter(out_f, fieldnames=COLLECT_COLUMNS)
        writer.writeheader()

        for row in source.fetch(windows):
            writer.writerow(row)
            written_rows += 1

    print(ngettext("Wrote %(n)d row", "Wrote %(n)d rows", written_rows) % {"n": written_rows}, file=sys.stderr)
    return written_rows
