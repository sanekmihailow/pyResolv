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


def _filters(payload: dict):
    return payload["query"]["bool"]["filter"]


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


def test_list_and_cidr_default_or_combined():
    # Both filters set + default mode 'or': a single bool.should wrapping the
    # terms clause and the prefix bool, so a SrcIP matching EITHER passes.
    s = GraylogSettings(
        url="http://x", stream_id="sid",
        src_ip_list=["10.2.83.133"], src_ip_cidr=["10.3.139.0/24"],
    )
    payload = _source_with(s)._build_payload("now-1h", "now")

    # The streams `term` filter stays; the two SrcIP sub-filters are OR-wrapped.
    src_bools = [f for f in _should_bools(payload) if "should" in f["bool"]]
    assert len(src_bools) == 1
    should = src_bools[0]["bool"]["should"]
    assert src_bools[0]["bool"]["minimum_should_match"] == 1
    assert {"terms": {"SrcIP": ["10.2.83.133"]}} in should
    assert any("bool" in clause and "should" in clause["bool"] for clause in should)
    # No bare `terms` sits directly in filter (it must be inside the OR wrapper).
    assert all("terms" not in f for f in _filters(payload))


def test_list_and_cidr_and_mode_are_anded():
    # mode 'and': both sub-filters go straight into `filter` (ANDed), no OR wrapper.
    s = GraylogSettings(
        url="http://x", stream_id="sid",
        src_ip_list=["10.2.83.133"], src_ip_cidr=["10.3.139.0/24"],
        src_ip_match_mode="and",
    )
    payload = _source_with(s)._build_payload("now-1h", "now")
    filters = _filters(payload)
    assert {"terms": {"SrcIP": ["10.2.83.133"]}} in filters
    prefix_bools = [f for f in filters if "bool" in f and "should" in f["bool"]]
    assert len(prefix_bools) == 1  # the CIDR-prefix bool sits at filter top level


def test_src_ip_allowed_or_vs_and():
    import ipaddress

    both = GraylogSettings(
        url="http://x", stream_id="sid",
        src_ip_list=["10.2.83.133"], src_ip_cidr=["10.3.139.0/24"],
    )
    src_or = _source_with(both)
    # OR: in the list OR in the subnet each pass; outside both fails.
    assert src_or._src_ip_allowed("10.2.83.133", ipaddress.ip_address("10.2.83.133"))
    assert src_or._src_ip_allowed("10.3.139.3", ipaddress.ip_address("10.3.139.3"))
    assert not src_or._src_ip_allowed("8.8.8.8", ipaddress.ip_address("8.8.8.8"))

    both_and = both.model_copy(update={"src_ip_match_mode": "and"})
    src_and = _source_with(both_and)
    # AND: needs to be in the list AND the subnet — the non-overlapping config
    # that caused the 0-rows bug now matches nothing (as explicitly requested).
    assert not src_and._src_ip_allowed("10.2.83.133", ipaddress.ip_address("10.2.83.133"))
    assert not src_and._src_ip_allowed("10.3.139.3", ipaddress.ip_address("10.3.139.3"))
