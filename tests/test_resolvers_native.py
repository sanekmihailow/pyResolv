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
    rdap_bootstrap_result: dict = {}  # returned when lookup_rdap(bootstrap=True)
    whois_result: dict = {}
    raise_on = None  # "rdap" | "rdap_primary" | "whois" | None
    bootstrap_calls = 0  # how many times lookup_rdap ran with bootstrap=True

    def __init__(self, ip, timeout=None):
        self.ip = ip

    def lookup_rdap(self, depth=1, bootstrap=False):
        if bootstrap:
            _FakeIPWhois.bootstrap_calls += 1
            return _FakeIPWhois.rdap_bootstrap_result
        # "rdap" fails both paths; "rdap_primary" fails only the ASN-based call.
        if _FakeIPWhois.raise_on in ("rdap", "rdap_primary"):
            raise RuntimeError("boom")
        return _FakeIPWhois.rdap_result

    def lookup_whois(self):
        if _FakeIPWhois.raise_on == "whois":
            raise RuntimeError("boom")
        return _FakeIPWhois.whois_result


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeIPWhois.rdap_result = {}
    _FakeIPWhois.rdap_bootstrap_result = {}
    _FakeIPWhois.whois_result = {}
    _FakeIPWhois.raise_on = None
    _FakeIPWhois.bootstrap_calls = 0
    yield


def _rdap_resolver(bootstrap=False):
    r = RdapResolver.__new__(RdapResolver)
    r._timeout = 10
    r._bootstrap = bootstrap
    return r


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
    out = _rdap_resolver().resolve_one("150.171.109.182")
    assert out["asn"] == "8075"
    assert out["asn_descr"] == "MICROSOFT-CORP-MSN-AS-BLOCK, US"
    assert out["country"] == "US"
    assert out["contacts"] == "Microsoft Corporation; Microsoft Abuse Contact"
    assert set(out) == set(RESOLVE_COLUMNS)  # full dict, no missing keys
    assert _FakeIPWhois.bootstrap_calls == 0  # primary succeeded -> no fallback


def test_rdap_lookup_error_returns_empty(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.raise_on = "rdap"
    # bootstrap disabled -> a failing primary just yields an empty result.
    assert _rdap_resolver(bootstrap=False).resolve_one("1.2.3.4") == {c: "" for c in RESOLVE_COLUMNS}


def test_rdap_bootstrap_fallback_on_empty(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}  # primary returns nothing usable
    _FakeIPWhois.rdap_bootstrap_result = {
        "objects": {"E": {"contact": {"name": "APNIC Hostmaster"}}},
        "network": {"country": "AU"},
    }
    out = _rdap_resolver(bootstrap=True).resolve_one("1.1.1.1")
    assert _FakeIPWhois.bootstrap_calls == 1
    assert out["country"] == "AU"  # from network.country (asn_country_code empty)
    assert out["contacts"] == "APNIC Hostmaster"


def test_rdap_bootstrap_fallback_on_error(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.raise_on = "rdap_primary"  # primary raises, bootstrap still works
    _FakeIPWhois.rdap_bootstrap_result = {"asn": "13335", "network": {"country": "US"}}
    out = _rdap_resolver(bootstrap=True).resolve_one("1.1.1.1")
    assert _FakeIPWhois.bootstrap_calls == 1
    assert out["asn"] == "13335"
    assert out["country"] == "US"


def test_rdap_bootstrap_disabled_no_second_call(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}  # primary empty
    _FakeIPWhois.rdap_bootstrap_result = {"asn": "13335"}
    out = _rdap_resolver(bootstrap=False).resolve_one("1.1.1.1")
    assert _FakeIPWhois.bootstrap_calls == 0  # fallback off -> bootstrap never called
    assert out == {c: "" for c in RESOLVE_COLUMNS}


def test_rdap_country_from_network_when_asn_code_empty(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {"asn": "8075", "asn_country_code": "", "network": {"country": "US"}}
    out = _rdap_resolver().resolve_one("1.2.3.4")
    assert out["country"] == "US"
    assert _FakeIPWhois.bootstrap_calls == 0  # primary had data -> no fallback


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
