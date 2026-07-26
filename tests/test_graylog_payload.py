"""Tests for how the graylog source builds the OpenSearch query: multiple
regexes in GRAYLOG__SRC_IP_REGEX are OR-combined (bool.should +
minimum_should_match=1), a single regex yields a single should. The instance is
created via __new__ to avoid touching config/network in __init__."""
from __future__ import annotations

from pyresolv.config import GraylogSettings
from pyresolv.sources.graylog import GraylogSource


def _source_with(settings: GraylogSettings) -> GraylogSource:
    src = GraylogSource.__new__(GraylogSource)
    src._settings = settings
    src._url = f"{settings.url}/{settings.index}_*/_search"
    return src


def _regexp_bools(payload: dict):
    return [f for f in payload["query"]["bool"]["filter"] if "bool" in f]


def test_multiple_regex_are_or_combined():
    s = GraylogSettings(
        url="http://x", stream_id="sid",
        src_ip_regex=[r"10\.8\.139\.\d+", r"10\.9\..*"],
    )
    payload = _source_with(s)._build_payload("now-1h", "now")

    bools = _regexp_bools(payload)
    assert len(bools) == 1
    inner = bools[0]["bool"]
    assert inner["minimum_should_match"] == 1
    values = [clause["regexp"]["SrcIP"]["value"] for clause in inner["should"]]
    assert values == [r"10\.8\.139\.\d+", r"10\.9\..*"]


def test_single_regex_still_works():
    s = GraylogSettings(url="http://x", stream_id="sid", src_ip_regex=[r"10\.8\.139\.\d+"])
    payload = _source_with(s)._build_payload("now-1h", "now")

    inner = _regexp_bools(payload)[0]["bool"]
    assert inner["minimum_should_match"] == 1
    assert len(inner["should"]) == 1
    assert inner["should"][0]["regexp"]["SrcIP"]["value"] == r"10\.8\.139\.\d+"


def test_no_regex_no_bool_filter():
    s = GraylogSettings(url="http://x", stream_id="sid", src_ip_regex=[])
    payload = _source_with(s)._build_payload("now-1h", "now")
    assert _regexp_bools(payload) == []
