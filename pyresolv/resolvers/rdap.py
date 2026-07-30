"""rdap resolver: ASN / description / contacts / country via RDAP (ipwhois
`lookup_rdap`) — the modern successor to port-43 WHOIS, and what gunter uses
internally. Self-contained: no external gunter service.

Note: each unique IP triggers a direct query to the authoritative RIR RDAP
server (+ an ASN-origin lookup), so bulk runs can hit rate limits. The per-key
cache and --workers in resolvers/base.py bound this.

Coverage fallback: when the primary ASN-based lookup yields nothing (RIR
misdetected/unreachable), retry via the RDAP bootstrap server, which follows
referrals to the right RIR without an ASN lookup. Controlled by
RESOLVE__RDAP_BOOTSTRAP; the extra request fires only on an empty result.
"""
from __future__ import annotations

import sys

from ipwhois import IPWhois

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers._rdap import contacts_from_objects
from pyresolv.resolvers.base import Resolver, register_resolver


@register_resolver("rdap")
class RdapResolver(Resolver):
    name = "rdap"

    def __init__(self) -> None:
        resolve = get_settings().resolve
        self._timeout = resolve.rdap_timeout
        self._bootstrap = resolve.rdap_bootstrap

    def _lookup(self, key: str, bootstrap: bool) -> dict:
        return IPWhois(key, timeout=self._timeout).lookup_rdap(depth=1, bootstrap=bootstrap)

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
        return result

    def resolve_one(self, key: str) -> dict:
        try:
            data = self._lookup(key, bootstrap=False)
        except Exception as e:
            print(_("[rdap][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            data = None

        result = self._extract(data) if data else self._empty_result()

        # Fallback: RDAP bootstrap when the primary lookup returned nothing usable.
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
