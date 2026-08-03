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


def _rdap_resolver(bootstrap=False, rdapss=False):
    r = RdapResolver.__new__(RdapResolver)
    r._timeout = 10
    r._bootstrap = bootstrap
    r._rdapss = rdapss
    r._rdapss_url = "https://rdap.ss/api/query?q=" if rdapss else ""
    r._rdapss_timeout = 15
    return r


def _whois_resolver(tcinet=False):
    w = WhoisResolver.__new__(WhoisResolver)
    w._timeout = 15
    w._tcinet = tcinet
    w._tcinet_host = "whois.tcinet.ru"
    w._tcinet_timeout = 15
    return w


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
    assert set(RESOLVE_COLUMNS) <= set(out)  # all columns present (+ optional meta)
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


# --- rdap.ss aggregator fallback --------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = 0
    def get(self, url, timeout=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return _FakeResp(self.payload)


RDAPSS_PAYLOAD = {
    "success": True,
    "data": {
        "country": "RU",
        "name": "VK-FRONT",
        "entities": [
            {"vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "VK ADM"]]],
             "entities": [{"vcardArray": ["vcard", [["fn", {}, "text", "VK NOC"]]]}]},
            {"vcardArray": ["vcard", [["fn", {}, "text", "VK ADM"]]]},  # dedup
        ],
    },
}


def test_contacts_from_rdap_entities_parses_and_dedups():
    from pyresolv.resolvers._rdap import contacts_from_rdap_entities
    assert contacts_from_rdap_entities(RDAPSS_PAYLOAD["data"]["entities"]) == "VK ADM; VK NOC"


def test_fn_from_vcard_edge_cases():
    from pyresolv.resolvers._rdap import _fn_from_vcard
    assert _fn_from_vcard(["vcard", [["fn", {}, "text", "X"]]]) == "X"
    assert _fn_from_vcard(None) == ""
    assert _fn_from_vcard(["vcard", []]) == ""
    assert _fn_from_vcard("nope") == ""


def test_rdap_rdapss_fallback_before_bootstrap(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}  # primary empty
    _FakeIPWhois.rdap_bootstrap_result = {"network": {"country": "XX"}}
    fake_req = _FakeRequests(payload=RDAPSS_PAYLOAD)
    monkeypatch.setattr(rdap_mod, "requests", fake_req)
    out = _rdap_resolver(bootstrap=True, rdapss=True).resolve_one("95.163.61.56")
    assert fake_req.calls == 1                 # rdap.ss queried
    assert _FakeIPWhois.bootstrap_calls == 0   # rdap.ss filled -> bootstrap NOT reached
    assert out["country"] == "RU"
    assert out["contacts"] == "VK ADM; VK NOC"
    assert out["asn"] == ""                    # rdap.ss IP object carries no ASN


def test_rdap_rdapss_empty_falls_through_to_bootstrap(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}
    _FakeIPWhois.rdap_bootstrap_result = {"network": {"country": "US"}}
    monkeypatch.setattr(rdap_mod, "requests", _FakeRequests(payload={"success": False}))
    out = _rdap_resolver(bootstrap=True, rdapss=True).resolve_one("1.1.1.1")
    assert _FakeIPWhois.bootstrap_calls == 1
    assert out["country"] == "US"


def test_rdap_rdapss_disabled_not_called(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}
    fake_req = _FakeRequests(payload=RDAPSS_PAYLOAD)
    monkeypatch.setattr(rdap_mod, "requests", fake_req)
    out = _rdap_resolver(bootstrap=False, rdapss=False).resolve_one("1.1.1.1")
    assert fake_req.calls == 0
    assert out == {c: "" for c in RESOLVE_COLUMNS}


def test_rdap_rdapss_error_returns_empty(monkeypatch):
    monkeypatch.setattr(rdap_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.rdap_result = {}
    monkeypatch.setattr(rdap_mod, "requests", _FakeRequests(exc=RuntimeError("net down")))
    out = _rdap_resolver(bootstrap=False, rdapss=True).resolve_one("1.1.1.1")
    assert out == {c: "" for c in RESOLVE_COLUMNS}


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
    out = _whois_resolver().resolve_one("150.171.109.182")
    assert out["asn"] == "8075"
    assert out["country"] == "US"
    assert out["contacts"] == "Microsoft Corporation"  # first line, deduped


# --- whois tcinet fallback --------------------------------------------------

TCINET_RU = """% TCI Whois Server
domain:        VK.RU
org:           LLC "V Kontakte"
registrar:     RUCENTER-RU
nserver:       ns1.vk.com.
nserver:       ns2.vk.com.
state:         REGISTERED, DELEGATED, VERIFIED
paid-till:     2025-01-01T21:00:00Z
"""


def test_whois_tcinet_fallback_on_empty(monkeypatch):
    monkeypatch.setattr(whois_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.whois_result = {}  # ipwhois returns nothing -> tcinet tier
    w = _whois_resolver(tcinet=True)
    monkeypatch.setattr(w, "_tcinet_query", lambda q: TCINET_RU)
    out = w.resolve_one("vk.ru")
    assert 'LLC "V Kontakte"' in out["contacts"]
    assert "RUCENTER-RU" in out["contacts"]
    assert "ns1.vk.com." in out["contacts"]
    assert out["country"] == "" and out["asn"] == ""  # domain whois has no ASN/country


def test_whois_tcinet_skips_non_tci_zone(monkeypatch):
    monkeypatch.setattr(whois_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.whois_result = {}
    w = _whois_resolver(tcinet=True)
    calls = {"n": 0}
    monkeypatch.setattr(w, "_tcinet_query", lambda q: calls.__setitem__("n", calls["n"] + 1) or "")
    out = w.resolve_one("example.com")  # not a TCI zone -> no network call
    assert calls["n"] == 0
    assert out == {c: "" for c in RESOLVE_COLUMNS}


def test_whois_tcinet_punycodes_idn(monkeypatch):
    monkeypatch.setattr(whois_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.whois_result = {}
    w = _whois_resolver(tcinet=True)
    seen = {}
    monkeypatch.setattr(w, "_tcinet_query", lambda q: seen.__setitem__("q", q) or "org: t\n")
    w.resolve_one("пример.рф")
    assert seen["q"] == "пример.рф".encode("idna").decode("ascii")


def test_whois_tcinet_disabled_not_called(monkeypatch):
    monkeypatch.setattr(whois_mod, "IPWhois", _FakeIPWhois)
    _FakeIPWhois.whois_result = {}
    w = _whois_resolver(tcinet=False)
    calls = {"n": 0}
    monkeypatch.setattr(w, "_tcinet_query", lambda q: calls.__setitem__("n", calls["n"] + 1) or "")
    w.resolve_one("vk.ru")
    assert calls["n"] == 0


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
