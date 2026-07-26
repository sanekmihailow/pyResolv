"""Shared stage I/O: read from stdin/-i, write to stdout/-o.

All stages use the same `open_input`/`open_output` so that the
"path -> file, no path -> stdin/stdout" behavior isn't duplicated and doesn't
drift between stages.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Optional, Union

PathArg = Optional[Union[str, Path]]


@contextmanager
def open_input(path: PathArg) -> Iterator[IO[str]]:
    """Open the data source: path -> file for reading, None/'-' -> stdin."""
    if path is None or path == "-":
        yield sys.stdin
        return
    with open(path, "r", newline="", encoding="utf-8") as f:
        yield f


@contextmanager
def open_output(path: PathArg) -> Iterator[IO[str]]:
    """Open the data sink: path -> file for writing (directories created as
    needed), None/'-' -> stdout."""
    if path is None or path == "-":
        yield sys.stdout
        return
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        yield f
