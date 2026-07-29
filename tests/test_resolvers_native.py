"""Tests for the native resolvers (rdap / whois / geo_maxmind / default chain).

All offline: `ipwhois.IPWhois` and the geoip2 reader are faked, so no network or
MaxMind DB is touched."""
from __future__ import annotations

import pytest

import pyresolv.resolvers.rdap as rdap_mod
import pyresolv.resolvers.whois as whois_mod
from pyresolv.resolvers.default_chain import DefaultResolver
from pyresolv.resolvers.geo_maxmind import GeoMaxmindResolver
from pyresolv.resolvers.rdap import RdapResolver
from pyresolv.resolvers.whois import WhoisResolver
from pyresolv.schema import RESOLVE_COLUMNS


# --- fake ipwhois -----------------------------------------------------------

class _FakeIPWhois:
    """Configurable stand-in for ipwhois.IPWhois. Class attrs hold the payloads."""
    rdap_result: dict = {}
    whois_result: dict = {}
    raise_on = None  # "rdap" | "whois" | None

    def __init__(self, ip, timeout=None):
        self.ip = ip

    def lookup_rdap(self, depth=1):
        if _FakeIPWhois.raise_on == "rdap":
            raise RuntimeError("boom")
        return _FakeIPWhois.rdap_result

    def lookup_whois(self):
        if _FakeIPWhois.raise_on == "whois":
            raise RuntimeError("boom")
        return _FakeIPWhois.whois_result


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeIPWhois.rdap_result = {}
    _FakeIPWhois.whois_result = {}
    _FakeIPWhois.raise_on = None
    yield


RDAP_RESULT = {
    "asn": "8075",
    "asn_description": "MICROSOFT-CORP-MSN-AS-BLOCK, US",
    "asn_country_code": "US",
    "objects": {  # RDAP dict keyed by handle
        "MSFT": {"contact": {"name": "Microsoft Corporation"}},
        "ABUSE": {"contact": {"name": "Microsoft Abuse Contact"}},
        "DUP": {"contact": {"name": "Microsoft Corporation"}},  # dedup
        "NOC": {"roles": ["technical"]},                        # no contact
    },
}


# --- rdap provider ----------------------------------------------------------

def test_rdap_maps_all_fields(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = RDAP_RESULT
    r = RdapResolver.__new__(RdapResolver)
    r._timeout = 10
    out = r.resolve_one("150.171.109.182")
    assert out["asn"] == "8075"
    assert out["asn_descr"] == "MICROSOFT-CORP-MSN-AS-BLOCK, US"
    assert out["country"] == "US"
    assert out["contacts"] == "Microsoft Corporation; Microsoft Abuse Contact"
    assert set(out) == set(RESOLVE_COLUMNS)  # full dict, no missing keys


def test_rdap_lookup_error_returns_empty(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.raise_on = "rdap"
    r = RdapResolver.__new__(RdapResolver)
    r._timeout = 10
    assert r.resolve_one("1.2.3.4") == {c: "" for c in RESOLVE_COLUMNS}


# --- whois provider ---------------------------------------------------------

def test_whois_maps_fields(monkeypatch):
    monkeypatch.setattr(whois_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.whois_result = {
        "asn": "8075",
        "asn_description": "MICROSOFT-CORP",
        "nets": [
            {"country": "US", "description": "Microsoft Corporation\nRedmond"},
            {"country": "US", "description": "Microsoft Corporation"},  # dedup
        ],
    }
    w = WhoisResolver.__new__(WhoisResolver)
    w._timeout = 15
    out = w.resolve_one("150.171.109.182")
    assert out["asn"] == "8075"
    assert out["country"] == "US"
    assert out["contacts"] == "Microsoft Corporation"  # first line, deduped


# --- geo_maxmind provider ---------------------------------------------------

class _FakeReader:
    def city(self, ip):
        class _R:
            class country:
                name = "United States"
        return _R()


def test_geo_maxmind_uses_reader():
    g = GeoMaxmindResolver.__new__(GeoMaxmindResolver)
    g._reader = _FakeReader()
    out = g.resolve_one("8.8.8.8")
    assert out["country"] == "United States"
    assert out["asn"] == "" and out["contacts"] == ""


def test_geo_maxmind_no_reader_is_noop():
    g = GeoMaxmindResolver.__new__(GeoMaxmindResolver)
    g._reader = None
    assert g.resolve_one("8.8.8.8") == {c: "" for c in RESOLVE_COLUMNS}


# --- default chain ----------------------------------------------------------

class _StubProvider:
    def __init__(self, name, payload, boom=False):
        self.name = name
        self._payload = payload
        self._boom = boom
        self.calls = 0

    def resolve_one(self, key):
        self.calls += 1
        if self._boom:
            raise RuntimeError("provider crashed")
        return {**{c: "" for c in RESOLVE_COLUMNS}, **self._payload}


def _chain(*providers):
    d = DefaultResolver.__new__(DefaultResolver)
    d._providers = list(providers)
    return d


def test_chain_geo_wins_country_rdap_fills_rest():
    geo = _StubProvider("geo_maxmind", {"country": "United States"})
    rdap = _StubProvider("rdap", {"country": "US", "asn": "8075", "asn_descr": "MSFT", "contacts": "MS"})
    whois = _StubProvider("whois", {"country": "XX", "asn": "999"})
    out = _chain(geo, rdap, whois).resolve_one("1.2.3.4")
    assert out["country"] == "United States"   # geo not overwritten by rdap
    assert out["asn"] == "8075" and out["contacts"] == "MS"
    assert whois.calls == 0                     # everything filled -> whois skipped


def test_chain_geo_empty_uses_rdap_country():
    geo = _StubProvider("geo_maxmind", {})
    rdap = _StubProvider("rdap", {"country": "US", "asn": "8075", "asn_descr": "MSFT", "contacts": "MS"})
    whois = _StubProvider("whois", {})
    out = _chain(geo, rdap, whois).resolve_one("1.2.3.4")
    assert out["country"] == "US"
    assert whois.calls == 0   # rdap filled all four columns -> whois skipped


def test_chain_whois_backfills_when_rdap_empty():
    geo = _StubProvider("geo_maxmind", {})
    rdap = _StubProvider("rdap", {})
    whois = _StubProvider("whois", {"country": "US", "asn": "8075", "asn_descr": "W", "contacts": "C"})
    out = _chain(geo, rdap, whois).resolve_one("1.2.3.4")
    assert out["asn"] == "8075" and out["country"] == "US" and out["contacts"] == "C"


def test_chain_provider_crash_is_swallowed():
    geo = _StubProvider("geo_maxmind", {"country": "US"}, boom=True)  # raises
    rdap = _StubProvider("rdap", {"asn": "8075", "asn_descr": "M", "contacts": "C", "country": "US"})
    out = _chain(geo, rdap, _StubProvider("whois", {})).resolve_one("1.2.3.4")
    assert out["asn"] == "8075" and out["country"] == "US"  # chain continued past the crash
