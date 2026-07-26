"""Tests for the merge stage: concatenating several inputs into one stream with
the header taken from the first non-empty input."""
from __future__ import annotations

import pytest

from pyresolv.stages.merge import merge


@pytest.fixture
def two_csvs(tmp_path):
    p1 = tmp_path / "a.csv"
    p2 = tmp_path / "b.csv"
    p1.write_text("SrcIP,DstIP\n1.1.1.1,2.2.2.2\n1.1.1.1,3.3.3.3\n", encoding="utf-8")
    p2.write_text("SrcIP,DstIP\n4.4.4.4,5.5.5.5\n", encoding="utf-8")
    return p1, p2


def test_merge_concatenates_in_order(two_csvs, tmp_path):
    p1, p2 = two_csvs
    out = tmp_path / "merged.csv"
    n = merge([str(p1), str(p2)], str(out))

    assert n == 3
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "SrcIP,DstIP"
    assert lines[1:] == ["1.1.1.1,2.2.2.2", "1.1.1.1,3.3.3.3", "4.4.4.4,5.5.5.5"]


def test_merge_no_inputs_raises(tmp_path, monkeypatch):
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    out = tmp_path / "out.csv"
    with pytest.raises(ValueError):
        merge([str(empty)], str(out))
