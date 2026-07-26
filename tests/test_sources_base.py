"""Tests for the shared source mechanics: time windows and the SOURCES registry."""
from __future__ import annotations

import pytest

from pyresolv.sources.base import (
    SOURCES,
    build_time_expr,
    build_time_windows,
    get_source,
)


def test_build_time_expr_zero_is_now():
    assert build_time_expr(0, "h") == "now"
    assert build_time_expr(5, "h") == "now-5h"
    assert build_time_expr(2, "d") == "now-2d"


def test_build_time_windows_hourly():
    # build_time_windows walks from `end` up to `start`, so the FIRST window
    # produced is the most recent one (closest to "now") and the LAST is the
    # oldest — this matches the original get_dst_ip_ranges.py algorithm.
    windows = build_time_windows(3, 0, "h")
    assert windows == [
        ("now-1h", "now", 1, 0),
        ("now-2h", "now-1h", 2, 1),
        ("now-3h", "now-2h", 3, 2),
    ]


def test_build_time_windows_start_must_exceed_end():
    with pytest.raises(ValueError):
        build_time_windows(0, 0, "h")
    with pytest.raises(ValueError):
        build_time_windows(1, 2, "h")


def test_graylog_registered_by_default():
    assert "graylog" in SOURCES


def test_get_source_unknown_raises_with_available_list():
    with pytest.raises(ValueError, match="graylog"):
        get_source("does-not-exist")
