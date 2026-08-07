"""Tests for the --log-file stderr tee (pyresolv/logfile.py).

The tee mirrors sys.stderr into a file: the file copy is line-oriented and
timestamped, tqdm-style \\r redraws collapse to their latest state, and stdout
must never leak into the log.
"""
from __future__ import annotations

import re
import sys

from pyresolv.logfile import tee_stderr

_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def test_noop_when_path_is_none():
    original = sys.stderr
    with tee_stderr(None):
        assert sys.stderr is original  # nothing installed
    assert sys.stderr is original


def test_mirrors_stderr_to_file_and_restores(tmp_path):
    log = tmp_path / "run.log"
    original = sys.stderr
    with tee_stderr(str(log)):
        assert sys.stderr is not original  # tee installed
        print("hello", file=sys.stderr)
    assert sys.stderr is original  # restored on exit

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("===== pyresolv started ")  # session header
    body = [ln for ln in lines[1:] if ln]
    assert len(body) == 1
    assert _STAMP.match(body[0])  # timestamped
    assert body[0].endswith("hello")


def test_carriage_return_collapses_to_latest_state(tmp_path):
    log = tmp_path / "run.log"
    with tee_stderr(str(log)):
        # tqdm-style redraws: only the final state before the newline survives.
        sys.stderr.write("progress 10%\rprogress 50%\rprogress 100%\n")

    body = [ln for ln in log.read_text(encoding="utf-8").splitlines()[1:] if ln]
    assert len(body) == 1
    assert body[0].endswith("progress 100%")
    assert "10%" not in body[0] and "50%" not in body[0]


def test_unterminated_line_is_flushed_on_exit(tmp_path):
    log = tmp_path / "run.log"
    with tee_stderr(str(log)):
        sys.stderr.write("no newline here")  # never terminated by \n

    body = [ln for ln in log.read_text(encoding="utf-8").splitlines()[1:] if ln]
    assert body and body[-1].endswith("no newline here")


def test_appends_across_runs(tmp_path):
    log = tmp_path / "run.log"
    with tee_stderr(str(log)):
        print("first", file=sys.stderr)
    with tee_stderr(str(log)):
        print("second", file=sys.stderr)

    text = log.read_text(encoding="utf-8")
    assert text.count("===== pyresolv started ") == 2  # one header per run
    assert "first" in text and "second" in text
