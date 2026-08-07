"""Optional tee of stderr to a log file (for unattended / cron runs).

Every stage already prints its status and progress to ``sys.stderr`` (stdout is
reserved for the CSV wire format), so mirroring ``sys.stderr`` captures the full
run log without touching a single stage. Installing the tee once at the top of
``cli.main()`` — before any parser builds a tqdm bar or any stage runs — is
enough: ``print(..., file=sys.stderr)`` and ``tqdm(file=sys.stderr)`` both look
up ``sys.stderr`` late, so they pick up the replacement.

The file copy is line-oriented and timestamped: the real terminal still shows
the live tqdm animation (raw ``\\r`` redraws), while the file collapses each
carriage-return redraw to the latest state and writes one timestamped line per
completed line — so the log stays readable instead of full of ``\\r`` spam.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime

from pyresolv.i18n import _


class _StderrTee:
    """A ``sys.stderr`` replacement that mirrors writes into a log file."""

    def __init__(self, terminal, log):
        self._terminal = terminal
        self._log = log
        self._pending = ""  # current, not-yet-terminated line for the log copy

    def write(self, data: str) -> int:
        # The terminal keeps the raw stream: live tqdm bars, colors, \r redraws.
        self._terminal.write(data)
        # The log copy is line-oriented and timestamped.
        for ch in data:
            if ch == "\r":
                # tqdm redraw: drop the partial line, keep only the latest state.
                self._pending = ""
            elif ch == "\n":
                self._write_line()
            else:
                self._pending += ch
        return len(data)

    def _write_line(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log.write("[%s] %s\n" % (stamp, self._pending))
        self._log.flush()
        self._pending = ""

    def flush(self) -> None:
        self._terminal.flush()
        self._log.flush()

    def isatty(self) -> bool:
        # Defer to the real terminal so tqdm still auto-detects a TTY correctly.
        return self._terminal.isatty()

    def __getattr__(self, name):
        # Everything else (encoding, fileno, ...) is delegated to the terminal.
        return getattr(self._terminal, name)


@contextmanager
def tee_stderr(path: str | None):
    """Mirror ``sys.stderr`` into ``path`` (append) for the duration of the block.

    A no-op when ``path`` is falsy, so callers can pass the raw flag value.
    """
    if not path:
        yield
        return

    log = open(path, "a", encoding="utf-8", buffering=1)
    header = _("===== pyresolv started %(ts)s: %(cmd)s =====") % {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cmd": " ".join(sys.argv),
    }
    log.write(header + "\n")
    log.flush()

    original = sys.stderr
    sys.stderr = _StderrTee(original, log)
    try:
        yield
    finally:
        # Flush any unfinished progress line, then restore and close.
        tee, sys.stderr = sys.stderr, original
        if isinstance(tee, _StderrTee) and tee._pending:
            tee._write_line()
        log.close()
