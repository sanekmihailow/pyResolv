"""Tests for `aggregate --out-dir`: the aggregation is split into one CSV per
subnet (bucketed by SrcIP's CIDR), unmatched rows go to an `other` file, and the
filenames carry the time slice."""
from __future__ import annotations

import os
from datetime import datetime

from pyresolv.stages.aggregate import aggregate
from pyresolv.subnets import parse_cidrs

# SrcIP spread across two /25 halves of 10.2.83.0/24, plus one outsider.
SAMPLE_CSV = """SrcIP,DstIP,DstPort,ac_action,url_domain,ac_rule_name
10.2.83.10,8.8.8.8,443,allow,a.com,r1
10.2.83.10,8.8.8.8,443,allow,a.com,r1
10.2.83.200,1.1.1.1,443,allow,b.com,r2
9.9.9.9,2.2.2.2,53,deny,,r3
"""


def _names(d):
    return sorted(os.listdir(d))


def test_split_by_25_and_other(tmp_path):
    src = tmp_path / "agg.csv"
    src.write_text(SAMPLE_CSV, encoding="utf-8")
    out = tmp_path / "out"

    networks = parse_cidrs(["10.2.83.0/25", "10.2.83.128/25"])
    n = aggregate(
        str(src), output_path=None, streaming=False,
        out_dir=str(out), networks=networks, start=5, end=0, time_unit="d",
        prefix="aggregation",
    )
    assert n == 3  # three distinct groups after aggregation

    names = _names(out)
    # three files: first /25, second /25, and other
    labels = {name.split("__", 1)[0] for name in names}
    assert labels == {
        "aggregation_10.2.83.0-25",
        "aggregation_10.2.83.128-25",
        "aggregation_other",
    }
    # every file name carries the __from__to__time- slice
    for name in names:
        assert "__time-" in name and name.endswith(".csv")

    # the first-/25 file holds only 10.2.83.10 (count 2), not the .200 row
    first = [n for n in names if n.startswith("aggregation_10.2.83.0-25")][0]
    body = (out / first).read_text(encoding="utf-8")
    assert "10.2.83.10" in body and "10.2.83.200" not in body
    assert ",2" in body  # the two identical rows collapsed to count 2

    # the outsider landed in `other`
    other = [n for n in names if n.startswith("aggregation_other")][0]
    assert "9.9.9.9" in (out / other).read_text(encoding="utf-8")


def test_out_dir_created_if_missing(tmp_path):
    src = tmp_path / "agg.csv"
    src.write_text(SAMPLE_CSV, encoding="utf-8")
    out = tmp_path / "nested" / "dir"
    aggregate(
        str(src), output_path=None, streaming=True,
        out_dir=str(out), networks=parse_cidrs(["10.2.83.0/24"]),
        start=1, end=0, time_unit="h",
    )
    assert out.is_dir() and _names(out)  # created + non-empty
