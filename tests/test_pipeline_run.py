"""Tests for the single-process pipeline engine (Variant B, pyresolv/runner.py):
config parsing/validation, in-memory step chaining, and byte-identical output vs.
the path-based Variant A stages. Fake source/resolver plugins keep it offline.
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pandas as pd
import pytest

from pyresolv import pipeline
from pyresolv.resolvers.base import Resolver, register_resolver
from pyresolv.runner import ResolveParams, _run_resolve, load_pipeline_config, run_pipeline
from pyresolv.sources.base import Source, register_source
from pyresolv.stages.aggregate import aggregate
from pyresolv.stages.trim import trim

RAW_CSV = """timestamp,SrcIP,DstIP,DstPort,ac_action,ac_rule_name,url_domain,url_path,SrcPort,source,message
t1,10.0.0.1,8.8.8.8,443,allow,r1,google.com,/,55,srcA,m1
t2,10.0.0.1,8.8.8.8,443,allow,r1,google.com,/,56,srcA,m2
t3,10.0.0.2,1.1.1.1,53,deny,r2,,/,57,srcB,m3
t4,10.0.0.1,8.8.8.8,443,allow,r1,google.com,/,58,srcA,m4
"""


@pytest.fixture
def raw_csv(tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text(RAW_CSV, encoding="utf-8")
    return p


# --- fake plugins ---------------------------------------------------------

_FAKE_ROWS = [
    dict(timestamp="t1", SrcIP="10.0.0.1", DstIP="8.8.8.8", DstPort="443",
         ac_action="allow", ac_rule_name="r1", url_domain="google.com", url_path="/"),
    dict(timestamp="t2", SrcIP="10.0.0.1", DstIP="8.8.8.8", DstPort="443",
         ac_action="allow", ac_rule_name="r1", url_domain="google.com", url_path="/"),
    dict(timestamp="t3", SrcIP="10.0.0.2", DstIP="1.1.1.1", DstPort="53",
         ac_action="deny", ac_rule_name="r2", url_domain="", url_path="/"),
]


@register_source("fake_src")
class _FakeSource(Source):
    def fetch_window(self, time_gte, time_lt):
        yield from _FAKE_ROWS


@register_resolver("fake_res")
class _FakeResolver(Resolver):
    name = "fake_res"
    calls = 0

    def resolve_one(self, key):
        type(self).calls += 1
        return {"country": "US", "asn": "AS15169", "asn_descr": "GOOGLE", "contacts": "abuse@x"}


@register_resolver("rec_workers")
class _RecWorkersResolver(Resolver):
    """Records the max_workers it was handed by the worker-count wiring."""
    name = "rec_workers"
    last_workers = None

    def resolve_one(self, key):
        return self._empty_result()

    def enrich(self, df, key_column, max_workers, skip_already_enriched=True, cache=None):
        type(self).last_workers = max_workers
        type(self).last_cache = type(cache).__name__
        return df


@register_resolver("boom_res")
class _BoomResolver(Resolver):
    """Raises inside enrich — used to check streaming temp cleanup on error."""
    name = "boom_res"

    def resolve_one(self, key):
        return self._empty_result()

    def enrich(self, df, key_column, max_workers, skip_already_enriched=True, cache=None):
        raise RuntimeError("boom")


# --- config validation ----------------------------------------------------

def _write(tmp_path, text):
    p = tmp_path / "pipe.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_config_bare_and_mapping_steps(tmp_path):
    steps = load_pipeline_config(_write(tmp_path, "- trim\n- aggregate: {min_count: 5}\n"))
    assert steps == [("trim", {}), ("aggregate", {"min_count": 5})]


def test_config_rejects_non_list(tmp_path):
    with pytest.raises(ValueError, match="list of steps"):
        load_pipeline_config(_write(tmp_path, "trim: {}\n"))


def test_config_rejects_unknown_step(tmp_path):
    with pytest.raises(ValueError, match="Unknown pipeline step"):
        load_pipeline_config(_write(tmp_path, "- frobnicate\n"))


def test_config_rejects_empty(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        load_pipeline_config(_write(tmp_path, "[]\n"))


def test_unknown_param_fails_fast(tmp_path, raw_csv):
    # A typo in a param name must fail before anything runs (extra="forbid").
    cfg = _write(tmp_path, "- trim\n- aggregate: {min_counts: 5}\n")
    with pytest.raises(ValueError, match="min_counts"):
        run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"))


# --- end-to-end -----------------------------------------------------------

def test_trim_aggregate_matches_variant_a(tmp_path, raw_csv):
    cfg = _write(tmp_path, "- trim\n- aggregate: {min_count: 2}\n")
    out_b = tmp_path / "b.csv"
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(out_b))

    # Variant A: trim then aggregate --no-streaming, same min_count.
    trimmed = tmp_path / "trimmed.csv"
    out_a = tmp_path / "a.csv"
    trim(str(raw_csv), str(trimmed))
    aggregate(str(trimmed), str(out_a), streaming=False, min_count=2)

    assert out_b.read_bytes() == out_a.read_bytes()


def test_collect_trim_aggregate(tmp_path):
    cfg = _write(tmp_path, "- collect: {source: fake_src, start: 1, end: 0}\n- trim\n- aggregate\n")
    out = tmp_path / "o.csv"
    n = run_pipeline(cfg, output_path=str(out))
    text = out.read_text(encoding="utf-8")
    # 3 fake rows -> 2 groups (the two identical google rows collapse).
    assert n == 2
    assert "timestamp" not in text.splitlines()[0]  # trim dropped it
    assert "8.8.8.8,443,allow,google.com,r1,2" in text


def test_resolve_enriches(tmp_path, raw_csv):
    _FakeResolver.calls = 0
    cfg = _write(tmp_path, "- trim\n- aggregate\n- resolve: {resolver: fake_res, workers: 2, cache: false}\n")
    out = tmp_path / "o.csv"
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(out))
    text = out.read_text(encoding="utf-8")
    assert "country" in text.splitlines()[0]
    assert "US" in text and "AS15169" in text
    # Two unique DstIPs after aggregation -> two resolve_one calls.
    assert _FakeResolver.calls == 2


# --- shared worker-count wiring (independent of resolver type) ------------

def test_runner_resolve_default_workers_from_resolve_settings():
    # No `workers` param -> default comes from settings.resolve.workers, for any resolver.
    _RecWorkersResolver.last_workers = None
    s = SimpleNamespace(default_resolver="rec_workers", resolve=SimpleNamespace(workers=5))
    _run_resolve(pd.DataFrame({"DstIP": ["8.8.8.8"]}), ResolveParams(resolver="rec_workers", cache=False), s)
    assert _RecWorkersResolver.last_workers == 5


def test_runner_resolve_workers_param_overrides():
    _RecWorkersResolver.last_workers = None
    s = SimpleNamespace(default_resolver="rec_workers", resolve=SimpleNamespace(workers=5))
    _run_resolve(pd.DataFrame({"DstIP": ["8.8.8.8"]}),
                 ResolveParams(resolver="rec_workers", workers=9, cache=False), s)
    assert _RecWorkersResolver.last_workers == 9


def test_pipeline_resolve_default_workers_from_resolve_settings(monkeypatch, tmp_path):
    _RecWorkersResolver.last_workers = None
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(default_resolver="rec_workers", resolve=SimpleNamespace(workers=6)),
    )
    inp = tmp_path / "in.csv"
    inp.write_text("DstIP,country,asn,asn_descr,contacts\n8.8.8.8,,,,\n", encoding="utf-8")
    args = SimpleNamespace(resolver=None, workers=None, input=[str(inp)],
                           output=str(tmp_path / "o.csv"), key_column="DstIP", cache=False)
    pipeline.run_resolve(args)
    assert _RecWorkersResolver.last_workers == 6


# --- CLI step-param overrides for `run` (CLI > YAML > config) --------------

def test_override_min_count_beats_yaml(tmp_path, raw_csv):
    # YAML says min_count 1 (keep all); CLI override 3 keeps only the count-3 group.
    cfg = _write(tmp_path, "- trim\n- aggregate: {min_count: 1}\n")
    n = run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"),
                     overrides={"min_count": 3})
    assert n == 1


def test_override_only_applies_to_steps_with_field(tmp_path, raw_csv):
    # `start` is not a TrimParams field: it must be skipped for trim (extra="forbid"
    # would otherwise raise), while `min_count` still overrides aggregate.
    cfg = _write(tmp_path, "- trim\n- aggregate: {min_count: 1}\n")
    n = run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"),
                     overrides={"start": 9, "min_count": 3})
    assert n == 1


def test_override_out_dir(tmp_path, raw_csv, monkeypatch):
    from types import SimpleNamespace
    outdir = tmp_path / "od"
    stub = SimpleNamespace(min_uniq_count=1, graylog=SimpleNamespace(src_ip_cidr=["10.0.0.0/24"]))
    monkeypatch.setattr("pyresolv.runner.get_settings", lambda: stub)
    cfg = _write(tmp_path, "- trim\n- aggregate\n")  # no out_dir in YAML
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"),
                 overrides={"out_dir": str(outdir)})
    assert list(outdir.glob("*.csv"))  # per-subnet files written to the overridden dir


def test_override_cache_false_uses_null_cache(tmp_path, raw_csv):
    _RecWorkersResolver.last_cache = None
    cfg = _write(tmp_path, "- trim\n- aggregate\n- resolve: {resolver: rec_workers}\n")
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"),
                 overrides={"cache": False})
    assert _RecWorkersResolver.last_cache == "NullCache"


def test_run_parser_collects_overrides():
    from pyresolv.cli import build_run_parser
    a = build_run_parser().parse_args(["--config", "p", "--out-dir", "/x", "--no-cache"])
    assert a.out_dir == "/x" and a.cache is False
    b = build_run_parser().parse_args(["--config", "p"])
    assert b.out_dir is None and b.cache is None and b.start is None


def test_first_step_needs_input_but_none(tmp_path):
    # trim first, no -i, no stdin data -> pandas raises on empty; engine surfaces
    # a ValueError-family error. We just assert it does not silently succeed.
    cfg = _write(tmp_path, "- trim\n")
    with pytest.raises(Exception):
        run_pipeline(cfg, input_path=str(tmp_path / "does_not_exist.csv"))


# --- streaming engine (run --streaming, bounded memory) -------------------

def test_streaming_matches_in_memory(tmp_path, raw_csv):
    # The bounded-memory engine must produce byte-identical output to in-memory.
    cfg = _write(tmp_path, "- trim\n- aggregate: {min_count: 2}\n")
    out_mem = tmp_path / "mem.csv"
    out_str = tmp_path / "str.csv"
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(out_mem))
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(out_str), streaming=True)
    assert out_str.read_bytes() == out_mem.read_bytes()


def test_streaming_collect_trim_aggregate(tmp_path):
    cfg = _write(tmp_path, "- collect: {source: fake_src, start: 1, end: 0}\n- trim\n- aggregate\n")
    out = tmp_path / "o.csv"
    n = run_pipeline(cfg, output_path=str(out), streaming=True)
    assert n == 2  # 3 fake rows -> 2 groups
    assert "8.8.8.8,443,allow,google.com,r1,2" in out.read_text(encoding="utf-8")


def test_streaming_uses_temp_log_path_and_cleans_up(tmp_path, raw_csv, monkeypatch):
    temp_root = tmp_path / "streamtmp"
    stub = SimpleNamespace(min_uniq_count=1,
                           streaming=SimpleNamespace(temp_log_path=str(temp_root)))
    monkeypatch.setattr("pyresolv.runner.get_settings", lambda: stub)

    created = {}
    real_td = tempfile.TemporaryDirectory

    def spy_td(*a, **k):
        td = real_td(*a, **k)
        created["dir"], created["root_arg"] = td.name, k.get("dir")
        return td

    monkeypatch.setattr("pyresolv.runner.tempfile.TemporaryDirectory", spy_td)

    cfg = _write(tmp_path, "- trim\n- aggregate\n")
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"), streaming=True)

    assert created["root_arg"] == str(temp_root)     # temp dir created under our path
    assert str(temp_root) in created["dir"]
    assert not os.path.exists(created["dir"])         # removed after the run
    assert temp_root.exists()                          # the configured root itself remains


def test_streaming_temp_cleaned_on_error(tmp_path, raw_csv, monkeypatch):
    created = {}
    real_td = tempfile.TemporaryDirectory

    def spy_td(*a, **k):
        td = real_td(*a, **k)
        created["dir"] = td.name
        return td

    monkeypatch.setattr("pyresolv.runner.tempfile.TemporaryDirectory", spy_td)

    cfg = _write(tmp_path, "- trim\n- aggregate\n- resolve: {resolver: boom_res, cache: false}\n")
    with pytest.raises(RuntimeError, match="boom"):
        run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"), streaming=True)
    assert not os.path.exists(created["dir"])          # temp dir removed even on failure


def test_streaming_out_dir_last_step(tmp_path, raw_csv, monkeypatch):
    outdir = tmp_path / "od"
    stub = SimpleNamespace(min_uniq_count=1,
                           streaming=SimpleNamespace(temp_log_path=None),
                           graylog=SimpleNamespace(src_ip_cidr=["10.0.0.0/24"]))
    monkeypatch.setattr("pyresolv.runner.get_settings", lambda: stub)
    cfg = _write(tmp_path, "- trim\n- aggregate\n")
    run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"),
                 overrides={"out_dir": str(outdir)}, streaming=True)
    assert list(outdir.glob("*.csv"))                  # per-subnet files written


def test_streaming_out_dir_not_last_errors(tmp_path, raw_csv, monkeypatch):
    stub = SimpleNamespace(min_uniq_count=1,
                           streaming=SimpleNamespace(temp_log_path=None),
                           graylog=SimpleNamespace(src_ip_cidr=["10.0.0.0/24"]))
    monkeypatch.setattr("pyresolv.runner.get_settings", lambda: stub)
    # aggregate --out-dir is not the terminal step -> must fail fast.
    cfg = _write(tmp_path, "- trim\n- aggregate: {out_dir: X}\n- trim\n")
    with pytest.raises(ValueError, match="not the last step"):
        run_pipeline(cfg, input_path=str(raw_csv), output_path=str(tmp_path / "o.csv"), streaming=True)


def test_run_parser_streaming_flag():
    from pyresolv.cli import build_run_parser
    assert build_run_parser().parse_args(["--config", "p", "--streaming"]).streaming is True
    assert build_run_parser().parse_args(["--config", "p"]).streaming is False
