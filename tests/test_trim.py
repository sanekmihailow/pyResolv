"""Tests for the trim stage: dropping DROP_COLS, keeping empty fields empty
(not NaN), and preserving a literal "NA" as a value (not as missing)."""
from __future__ import annotations

import pytest

from pyresolv.stages.trim import trim

RAW_CSV = """timestamp,SrcIP,DstIP,DstPort,ac_action,ac_rule_name,url_domain,url_path
2026-07-18T10:00:00.000Z,10.2.83.129,8.8.8.8,443,allow,rule1,google.com,/
2026-07-18T10:00:03.000Z,10.2.83.130,1.1.1.1,80,deny,NA,,/blocked
"""


@pytest.fixture
def raw_csv(tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text(RAW_CSV, encoding="utf-8")
    return p


def test_trim_drops_columns_and_preserves_na_and_empty(raw_csv, tmp_path):
    out = tmp_path / "trimmed.csv"
    rows_written = trim(str(raw_csv), str(out), chunksize=1)

    assert rows_written == 2
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = lines[0].split(",")

    assert header == ["SrcIP", "DstIP", "DstPort", "ac_action", "ac_rule_name", "url_domain", "url_path"]
    assert "timestamp" not in header

    # Row with literal "NA" ac_rule_name and empty url_domain
    row2 = lines[2].split(",")
    row_dict = dict(zip(header, row2))
    assert row_dict["ac_rule_name"] == "NA", "literal 'NA' value must survive trim, not become empty"
    assert row_dict["url_domain"] == "", "missing field stays empty string"
    assert "nan" not in text.lower()


def test_trim_multi_chunk_matches_single_chunk(raw_csv, tmp_path):
    out_single = tmp_path / "single.csv"
    out_multi = tmp_path / "multi.csv"
    trim(str(raw_csv), str(out_single), chunksize=10_000)
    trim(str(raw_csv), str(out_multi), chunksize=1)
    assert out_single.read_bytes() == out_multi.read_bytes()
