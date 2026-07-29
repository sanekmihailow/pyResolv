"""Tests for how the graylog source builds the OpenSearch query: subnets in
GRAYLOG__SRC_IP_CIDR become an OR-combined `prefix` filter on the string SrcIP
(bool.should + minimum_should_match=1), narrowing to the enclosing octet prefix
(a /25 uses its /24 prefix). The instance is created via __new__ to avoid
touching config/network in __init__; `_networks` is set the way __init__ would."""
from __future__ import annotations

from pyresolv.config import GraylogSettings
from pyresolv.sources.graylog import GraylogSource
from pyresolv.subnets import parse_cidrs


def _source_with(settings: GraylogSettings) -> GraylogSource:
    src = GraylogSource.__new__(GraylogSource)
    src._settings = settings
    src._url = f"{settings.url}/{settings.index}_*/_search"
    src._networks = parse_cidrs(settings.src_ip_cidr)
    return src


def _should_bools(payload: dict):
    return [f for f in payload["query"]["bool"]["filter"] if "bool" in f]


def test_multiple_cidr_are_or_combined():
    s = GraylogSettings(
        url="http://x", stream_id="sid",
        src_ip_cidr=["10.8.139.0/24", "10.9.0.0/16"],
    )
    payload = _source_with(s)._build_payload("now-1h", "now")

    bools = _should_bools(payload)
    assert len(bools) == 1
    inner = bools[0]["bool"]
    assert inner["minimum_should_match"] == 1
    prefixes = [clause["prefix"]["SrcIP"] for clause in inner["should"]]
    assert prefixes == ["10.8.139.", "10.9."]


def test_single_cidr_still_works():
    s = GraylogSettings(url="http://x", stream_id="sid", src_ip_cidr=["10.8.139.0/24"])
    payload = _source_with(s)._build_payload("now-1h", "now")

    inner = _should_bools(payload)[0]["bool"]
    assert inner["minimum_should_match"] == 1
    assert len(inner["should"]) == 1
    assert inner["should"][0]["prefix"]["SrcIP"] == "10.8.139."


def test_slash25_uses_enclosing_slash24_prefix():
    # A /25 cannot be expressed server-side on a string field, so the prefix is
    # the enclosing /24; the exact /25 is enforced client-side in fetch_window.
    s = GraylogSettings(url="http://x", stream_id="sid", src_ip_cidr=["10.2.83.0/25"])
    payload = _source_with(s)._build_payload("now-1h", "now")
    inner = _should_bools(payload)[0]["bool"]
    assert inner["should"][0]["prefix"]["SrcIP"] == "10.2.83."


def test_no_cidr_no_bool_filter():
    s = GraylogSettings(url="http://x", stream_id="sid", src_ip_cidr=[])
    payload = _source_with(s)._build_payload("now-1h", "now")
    assert _should_bools(payload) == []
