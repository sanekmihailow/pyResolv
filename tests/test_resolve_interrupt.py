"""Tests for an interrupted `resolve`: Ctrl+C / SIGTERM must keep everything
resolved so far (partial CSV written, input preserved) and the next run must pick
up where it stopped. All offline — the fake resolvers raise KeyboardInterrupt
themselves, exactly as a real interrupt reaches the loop through future.result()."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pandas as pd
import pytest

from pyresolv import pipeline
from pyresolv.io import open_output
from pyresolv.resolvers.base import ResolveInterrupted, Resolver, register_resolver
from pyresolv.resolvers.cache import NullCache, SqliteCache
from pyresolv.runner import run_pipeline
from pyresolv.schema import RESOLVE_COLUMNS

_IPS = [f"10.0.0.{i}" for i in range(1, 9)]


def _frame(ips=_IPS):
    cols = {"DstIP": list(ips)}
    for c in RESOLVE_COLUMNS:
        cols[c] = [""] * len(ips)
    return pd.DataFrame(cols)


def _csv(tmp_path, name="in.csv", ips=_IPS):
    p = tmp_path / name
    _frame(ips).to_csv(p, index=False)
    return p


@register_resolver("interrupting")
class _Interrupting(Resolver):
    """Resolves `limit` keys, then interrupts — the stand-in for a user's Ctrl+C."""
    name = "interrupting"
    limit = 3

    def __init__(self) -> None:
        self.done = 0

    def resolve_one(self, key):
        if self.done >= type(self).limit:
            # With one worker the keys run in order, so this pause lets the main
            # thread drain the already-finished futures before the interrupt —
            # otherwise as_completed may hand them over in any order and the test
            # races with the pool instead of testing the interrupt path.
            time.sleep(0.2)
            raise KeyboardInterrupt
        self.done += 1
        r = self._empty_result()
        r["country"] = "RU"
        r["asn"] = "1"
        r["asn_descr"] = "d"
        r["contacts"] = "c"
        return r


def _enriched_rows(df):
    return sum(all(str(df.iloc[i][c]).strip() for c in RESOLVE_COLUMNS) for i in range(len(df)))


# --- the enrich core --------------------------------------------------------

def test_enrich_raises_with_the_partial_frame():
    r = _Interrupting()
    with pytest.raises(ResolveInterrupted) as exc:
        r.enrich(_frame(), "DstIP", 1, cache=NullCache())

    assert exc.value.done == _Interrupting.limit
    assert exc.value.total == len(_IPS)
    assert exc.value.resolver == "interrupting" and exc.value.key_column == "DstIP"
    assert _enriched_rows(exc.value.frame) == _Interrupting.limit  # the rest untouched


def test_resolve_writes_the_partial_csv(tmp_path):
    out = tmp_path / "out.csv"
    with pytest.raises(ResolveInterrupted):
        _Interrupting().resolve(str(_csv(tmp_path)), str(out), "DstIP", 1, cache=NullCache())

    assert _enriched_rows(pd.read_csv(out, keep_default_na=False, dtype=str)) == _Interrupting.limit


def test_rerun_only_resolves_what_is_missing(tmp_path):
    """The partial output fed back in: filled rows are skipped, the rest retried."""
    out = tmp_path / "out.csv"
    with pytest.raises(ResolveInterrupted):
        _Interrupting().resolve(str(_csv(tmp_path)), str(out), "DstIP", 1, cache=NullCache())

    class _Rest(_Interrupting):
        limit = 99  # finishes this time

    resumed = _Rest()
    resumed.resolve(str(out), str(tmp_path / "final.csv"), "DstIP", 1, cache=NullCache())
    # Only the keys left unresolved by the interrupted run went to the network.
    assert resumed.done == len(_IPS) - _Interrupting.limit
    final = pd.read_csv(tmp_path / "final.csv", keep_default_na=False, dtype=str)
    assert _enriched_rows(final) == len(_IPS)


def test_interrupt_keeps_resolved_keys_in_the_cache(tmp_path):
    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    r = _Interrupting()
    with pytest.raises(ResolveInterrupted):
        r.enrich(_frame(), "DstIP", 1, cache=cache)

    cached = [k for k in _IPS if cache.get(r._cache_key(k)) is not None]
    assert len(cached) == _Interrupting.limit


# --- the --delete guarantee -------------------------------------------------

def test_interrupt_does_not_delete_the_input(tmp_path):
    """--delete must not eat the source when the stage did not finish."""
    inp = _csv(tmp_path)
    args = SimpleNamespace(
        type="resolve", input=[str(inp)], output=str(tmp_path / "out.csv"), delete=True,
        resolver="interrupting", key_column="DstIP", workers=1, cache=False, cache_ttl=None,
    )
    with pytest.raises(ResolveInterrupted):
        pipeline.dispatch(args)

    assert inp.exists()


# --- both `run` engines -----------------------------------------------------

@pytest.mark.parametrize("streaming", [False, True])
def test_run_pipeline_writes_the_partial_frame(tmp_path, streaming):
    cfg = tmp_path / "pipe.yaml"
    cfg.write_text("- resolve: {resolver: interrupting, workers: 1, cache: false}\n", encoding="utf-8")
    out = tmp_path / "out.csv"

    with pytest.raises(ResolveInterrupted):
        run_pipeline(str(cfg), str(_csv(tmp_path)), str(out), streaming=streaming)

    assert _enriched_rows(pd.read_csv(out, keep_default_na=False, dtype=str)) == _Interrupting.limit


# --- atomic output ----------------------------------------------------------

def test_open_output_is_atomic(tmp_path):
    target = tmp_path / "out.csv"
    target.write_text("old\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with open_output(str(target)) as f:
            f.write("half written")
            raise RuntimeError("boom")

    assert target.read_text(encoding="utf-8") == "old\n"          # previous file intact
    assert list(tmp_path.glob(".*tmp")) == []                     # no leftover temp file

    with open_output(str(target)) as f:
        f.write("new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert list(tmp_path.glob(".*tmp")) == []


# --- resume hint points at what was actually written ------------------------

def _hint(out_dir=None, output_path=None):
    from pyresolv.cli import _resume_hint
    exc = ResolveInterrupted(_frame(), 1, 2, "rdap", "DstIP")
    exc.out_dir, exc.output_path = out_dir, output_path
    return _resume_hint(exc)


def test_resume_hint_for_a_single_file():
    assert _hint(output_path="out.csv") == (
        "pyresolv --type resolve -i out.csv -o out.csv --resolver rdap --key-column DstIP"
    )


def test_resume_hint_for_a_per_subnet_split():
    """With out_dir the -o file is never written, so the hint must walk the split."""
    hint = _hint(out_dir="/srv/report")
    assert hint.startswith("for f in /srv/report/*.csv; do")
    assert '-i "$f" -o "$f"' in hint


def test_resume_hint_is_none_for_stdout():
    assert _hint(output_path=None) is None and _hint(output_path="-") is None


def test_run_with_out_dir_reports_the_split_dir(tmp_path, monkeypatch):
    """The interrupt must carry the split directory, not the ignored -o path."""
    out_dir = tmp_path / "split"
    stub = SimpleNamespace(
        min_uniq_count=1, default_resolver="interrupting",
        graylog=SimpleNamespace(src_ip_cidr=["10.2.83.0/24"]),
        resolve=SimpleNamespace(workers=1, cache="none", cache_ttl=None),
    )
    monkeypatch.setattr("pyresolv.runner.get_settings", lambda: stub)

    src = tmp_path / "in.csv"
    pd.DataFrame({
        "SrcIP": [f"10.2.83.{i}" for i in range(1, 9)],
        "DstIP": _IPS,
        "DstPort": ["443"] * 8,
        "ac_action": ["allow"] * 8,
        "url_domain": ["x"] * 8,
        "ac_rule_name": ["r"] * 8,
    }).to_csv(src, index=False)

    cfg = tmp_path / "pipe.yaml"
    cfg.write_text(
        f"- aggregate: {{out_dir: {out_dir}}}\n"
        "- resolve: {resolver: interrupting, workers: 1, cache: false}\n",
        encoding="utf-8",
    )

    with pytest.raises(ResolveInterrupted) as exc:
        run_pipeline(str(cfg), str(src), str(tmp_path / "ignored.csv"))

    assert exc.value.out_dir == str(out_dir)
    assert exc.value.output_path is None
    assert list(out_dir.glob("*.csv"))                       # the split was written
    assert not (tmp_path / "ignored.csv").exists()           # -o really is ignored
