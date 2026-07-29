"""rdap resolver: ASN / description / contacts / country via RDAP (ipwhois
`lookup_rdap`) — the modern successor to port-43 WHOIS, and what gunter uses
internally. Self-contained: no external gunter service.

Note: each unique IP triggers a direct query to the authoritative RIR RDAP
server (+ an ASN-origin lookup), so bulk runs can hit rate limits. The per-key
cache and --workers in resolvers/base.py bound this.
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
        self._timeout = get_settings().resolve.rdap_timeout

    def resolve_one(self, key: str) -> dict:
        result = self._empty_result()
        try:
            data = IPWhois(key, timeout=self._timeout).lookup_rdap(depth=1)
        except Exception as e:
            print(_("[rdap][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            return result

        result["asn"] = str(data.get("asn") or "").strip()
        result["asn_descr"] = str(data.get("asn_description") or "").strip()
        result["country"] = str(data.get("asn_country_code") or "").strip()
        result["contacts"] = contacts_from_objects(data.get("objects", {}))
        return result
