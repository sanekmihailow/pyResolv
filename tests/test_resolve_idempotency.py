"""Tests for the shared resolver mechanics (resolvers/base.py) without network:
we register a fake resolver that counts resolve_one calls and check that a
second run over an already-enriched file makes no new "requests" (idempotency
via _is_already_enriched), and that the second run's output is byte-identical to
the first.
"""
from __future__ import annotations

import pytest

from pyresolv.resolvers.base import Resolver, get_resolver, register_resolver

SAMPLE_AGGREGATED_CSV = """SrcIP,DstIP,DstPort,ac_action,url_domain,ac_rule_name,count
10.2.83.129,8.8.8.8,443,allow,google.com,rule1,3
10.2.83.130,1.1.1.1,443,allow,cloudflare.com,rule2,2
10.2.83.129,2.2.2.2,443,allow,,rule4,1
"""


class _CountingResolver(Resolver):
    name = "counting-test-resolver"
    call_count = 0
    calls = []

    def resolve_one(self, key: str) -> dict:
        _CountingResolver.call_count += 1
        _CountingResolver.calls.append(key)
        return {
            "country": f"Country-{key}",
            "asn": f"AS-{key}",
            "asn_descr": f"Descr-{key}",
            "contacts": f"Contact-{key}",
        }


@pytest.fixture(autouse=True)
def _register_and_reset():
    register_resolver("counting-test-resolver")(_CountingResolver)
    _CountingResolver.call_count = 0
    _CountingResolver.calls = []
    yield


@pytest.fixture
def sample_csv(tmp_path):
    p = tmp_path / "aggregated.csv"
    p.write_text(SAMPLE_AGGREGATED_CSV, encoding="utf-8")
    return p


def test_get_resolver_finds_registered_fake():
    r = get_resolver("counting-test-resolver")
    assert isinstance(r, _CountingResolver)


def test_resolve_then_resolve_again_is_idempotent(sample_csv, tmp_path):
    resolver = get_resolver("counting-test-resolver")
    out1 = tmp_path / "resolved1.csv"
    resolver.resolve(str(sample_csv), str(out1), key_column="DstIP", max_workers=2)

    assert _CountingResolver.call_count == 3  # 3 unique DstIP values
    first_calls = sorted(_CountingResolver.calls)
    assert first_calls == ["1.1.1.1", "2.2.2.2", "8.8.8.8"]

    out2 = tmp_path / "resolved2.csv"
    resolver2 = get_resolver("counting-test-resolver")
    resolver2.resolve(str(out1), str(out2), key_column="DstIP", max_workers=2)

    # No new calls on the second pass — every row is already fully enriched.
    assert _CountingResolver.call_count == 3
    assert out1.read_bytes() == out2.read_bytes()


def test_unknown_resolver_raises_with_available_list():
    with pytest.raises(ValueError, match="counting-test-resolver|gunter"):
        get_resolver("does-not-exist")
