"""Data source abstraction for the `collect` stage + a name registry.

Ported the shared time-window logic from get_dst_ip_ranges.py
(`build_time_expr`, `shift_now`, `build_time_windows`). A concrete source
(e.g. `graylog.py`) implements only `fetch_window(gte, lt)`.
"""
from __future__ import annotations

import abc
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Tuple, Type

from pyresolv.i18n import _

# (time_gte_expr, time_lt_expr, from_value, to_value)
TimeWindow = Tuple[str, str, int, int]

SOURCES: Dict[str, Type["Source"]] = {}


def register_source(name: str):
    """Decorator that registers a source in the SOURCES registry by name."""

    def _decorator(cls: Type["Source"]) -> Type["Source"]:
        SOURCES[name] = cls
        return cls

    return _decorator


def get_source(name: str) -> "Source":
    try:
        cls = SOURCES[name]
    except KeyError:
        available = ", ".join(sorted(SOURCES)) or _("<no sources registered>")
        raise ValueError(
            _("Unknown source '%(name)s'. Available sources: %(available)s")
            % {"name": name, "available": available}
        ) from None
    return cls()


def build_time_expr(value: int, unit: str) -> str:
    if value == 0:
        return "now"
    return f"now-{value}{unit}"


def shift_now(base_now: datetime, value: int, unit: str) -> datetime:
    if unit == "d":
        return base_now - timedelta(days=value)
    if unit == "h":
        return base_now - timedelta(hours=value)
    raise ValueError(_("Unsupported unit: %(unit)s") % {"unit": unit})


def build_time_windows(start: int, end: int, unit: str) -> List[TimeWindow]:
    """Split [--start, --end) into windows 1 unit long."""
    if start <= end:
        raise ValueError(_("--start must be greater than --end, e.g. --start 5 --end 0"))

    windows: List[TimeWindow] = []
    current = end

    while current < start:
        next_value = current + 1
        gte_expr = build_time_expr(next_value, unit)
        lt_expr = build_time_expr(current, unit)
        windows.append((gte_expr, lt_expr, next_value, current))
        current = next_value

    return windows


class Source(abc.ABC):
    """Base source class. A subclass implements only fetch_window."""

    @abc.abstractmethod
    def fetch_window(self, time_gte: str, time_lt: str) -> Iterator[dict]:
        """Yield records row by row (dicts following the COLLECT_COLUMNS schema)
        for the window [time_gte, time_lt)."""
        raise NotImplementedError

    def fetch(self, windows: List[TimeWindow]) -> Iterator[dict]:
        for time_gte, time_lt, _from_value, _to_value in windows:
            yield from self.fetch_window(time_gte, time_lt)
