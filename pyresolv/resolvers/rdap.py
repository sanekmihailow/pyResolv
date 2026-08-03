"""rdap resolver: ASN / description / contacts / country via RDAP (ipwhois
`lookup_rdap`) — the modern successor to port-43 WHOIS, and what gunter uses
internally. Self-contained: no external gunter service.

Note: each unique IP triggers a direct query to the authoritative RIR RDAP
server (+ an ASN-origin lookup), so bulk runs can hit rate limits. The per-key
cache and --workers in resolvers/base.py bound this.

Coverage fallbacks, each firing only when the previous step returned nothing:
  1. primary ASN-based lookup (direct RIR RDAP);
  2. the rdap.ss aggregator over HTTP (RESOLVE__RDAPSS) — a single reachable
     endpoint that proxies to the right registry, useful when the RIR RDAP is
     unreachable from this host;
  3. the RDAP bootstrap server (RESOLVE__RDAP_BOOTSTRAP), which follows referrals
     to the right RIR without an ASN lookup.
"""
from __future__ import annotations

import sys
from urllib.parse import quote

import requests
from ipwhois import IPWhois

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers._rdap import contacts_from_objects, contacts_from_rdap_entities
from pyresolv.resolvers.base import Resolver, register_resolver


def _expiration(events, action_key: str, date_key: str) -> str:
    """Find the 'expiration' event's date in an RDAP events list. Handles both
    ipwhois network events ({action, timestamp}) and raw RFC 7483 events
    ({eventAction, eventDate})."""
    if not isinstance(events, list):
        return ""
    for event in events:
        if isinstance(event, dict) and str(event.get(action_key, "")).lower() == "expiration":
            return str(event.get(date_key) or "").strip()
    return ""


@register_resolver("rdap")
class RdapResolver(Resolver):
    name = "rdap"

    def __init__(self) -> None:
        resolve = get_settings().resolve
        self._timeout = resolve.rdap_timeout
        self._bootstrap = resolve.rdap_bootstrap
        self._rdapss = resolve.rdapss
        self._rdapss_url = resolve.rdapss_url
        self._rdapss_timeout = resolve.rdapss_timeout

    def _lookup(self, key: str, bootstrap: bool) -> dict:
        return IPWhois(key, timeout=self._timeout).lookup_rdap(depth=1, bootstrap=bootstrap)

    def _rdapss_lookup(self, key: str) -> dict | None:
        """Query the rdap.ss aggregator; return its RFC 7483 `data` object or None."""
        try:
            resp = requests.get(self._rdapss_url + quote(str(key)), timeout=self._rdapss_timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            print(_("[rdap][%(ip)s] rdap.ss lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            return None
        if not payload.get("success"):
            return None
        return payload.get("data") or None

    def _extract_rdapss(self, data: dict) -> dict:
        """Map the rdap.ss `data` (raw RFC 7483 IP object) into RESOLVE_COLUMNS.
        The IP object carries country + entities (contacts) but no origin ASN."""
        result = self._empty_result()
        result["country"] = str(data.get("country") or "").strip()
        result["contacts"] = contacts_from_rdap_entities(data.get("entities", []))
        result["expires"] = _expiration(data.get("events"), "eventAction", "eventDate")
        return result

    def _extract(self, data: dict) -> dict:
        result = self._empty_result()
        result["asn"] = str(data.get("asn") or "").strip()
        result["asn_descr"] = str(data.get("asn_description") or "").strip()
        # asn_country_code comes from the ASN lookup (empty in bootstrap mode);
        # fall back to the RDAP network object's own country.
        country = str(data.get("asn_country_code") or "").strip()
        if not country:
            country = str((data.get("network") or {}).get("country") or "").strip()
        result["country"] = country
        result["contacts"] = contacts_from_objects(data.get("objects", {}))
        # Cache-TTL hint (rare for IP objects); not written to the CSV.
        result["expires"] = _expiration((data.get("network") or {}).get("events"), "action", "timestamp")
        return result

    def resolve_one(self, key: str) -> dict:
        try:
            data = self._lookup(key, bootstrap=False)
        except Exception as e:
            print(_("[rdap][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            data = None

        result = self._extract(data) if data else self._empty_result()

        # Fallback 1: rdap.ss aggregator (before the bootstrap) — reachable when
        # the direct RIR RDAP is not.
        if self._rdapss and self._rdapss_url and not any(result.values()):
            data = self._rdapss_lookup(key)
            if data:
                result = self._extract_rdapss(data)

        # Fallback 2: RDAP bootstrap when everything above returned nothing usable.
        if self._bootstrap and not any(result.values()):
            try:
                data = self._lookup(key, bootstrap=True)
            except Exception as e:
                print(
                    _("[rdap][%(ip)s] bootstrap lookup failed: %(err)s") % {"ip": key, "err": e},
                    file=sys.stderr,
                )
                data = None
            if data:
                result = self._extract(data)

        return result
