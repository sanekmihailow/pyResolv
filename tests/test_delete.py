"""Tests for the --delete flag: the stage removes its -i file after writing the
output successfully, never touches stdin/the output file, is a no-op for
collect, and keeps the input on error."""
from __future__ import annotations

import pytest

from pyresolv.cli import build_parser
from pyresolv.pipeline import dispatch

TRIMMED_CSV = """SrcIP,DstIP,DstPort,ac_action,url_domain,ac_rule_name
10.0.0.1,8.8.8.8,443,allow,google.com,r1
10.0.0.1,8.8.8.8,443,allow,google.com,r1
10.0.0.2,1.1.1.1,53,deny,,r2
"""


@pytest.fixture
def trimmed(tmp_path):
    p = tmp_path / "trimmed.csv"
    p.write_text(TRIMMED_CSV, encoding="utf-8")
    return p


def _run(argv):
    return dispatch(build_parser().parse_args(argv))


def test_delete_removes_input_after_success(trimmed, tmp_path):
    out = tmp_path / "aggregated.csv"
    _run(["--type", "aggregate", "-i", str(trimmed), "-o", str(out), "--delete"])

    assert not trimmed.exists(), "input file should be deleted after successful aggregation"
    assert out.exists() and out.read_text(encoding="utf-8").strip(), "result should remain non-empty"


def test_del_alias_works(trimmed, tmp_path):
    out = tmp_path / "aggregated.csv"
    _run(["--type", "aggregate", "-i", str(trimmed), "-o", str(out), "--del"])
    assert not trimmed.exists()


def test_without_flag_input_kept(trimmed, tmp_path):
    out = tmp_path / "aggregated.csv"
    _run(["--type", "aggregate", "-i", str(trimmed), "-o", str(out)])
    assert trimmed.exists(), "without --delete the input file should stay in place"


def test_delete_never_removes_output(trimmed, tmp_path):
    # input == output: the file is both input and output — it must not be deleted.
    _run(["--type", "aggregate", "-i", str(trimmed), "-o", str(trimmed), "--delete"])
    assert trimmed.exists(), "a file equal to -o must not be deleted"


def test_delete_noop_for_collect(tmp_path):
    # collect ignores its input; --delete must delete nothing. We exercise the
    # deletion helper directly, without running a real collect (network/config).
    import argparse

    from pyresolv.pipeline import _delete_inputs

    dummy = tmp_path / "some.csv"
    dummy.write_text("x\n", encoding="utf-8")
    ns = argparse.Namespace(type="collect", input=[str(dummy)], output=None, delete=True)
    _delete_inputs(ns)
    assert dummy.exists(), "collect must not delete the given -i file"


def test_input_kept_on_stage_error(trimmed, tmp_path, monkeypatch):
    # If the stage raises, the input must be left untouched.
    import pyresolv.pipeline as pipeline

    def boom(args):
        raise ValueError("stage failed")

    monkeypatch.setitem(pipeline.DISPATCH, "aggregate", boom)
    out = tmp_path / "aggregated.csv"
    with pytest.raises(ValueError):
        _run(["--type", "aggregate", "-i", str(trimmed), "-o", str(out), "--delete"])
    assert trimmed.exists(), "on stage error the input file should be preserved"
