"""whois resolver: ASN / description / country / contacts via legacy port-43
WHOIS (ipwhois `lookup_whois`). A fallback for cases RDAP doesn't cover; contact
data is coarser than RDAP (WHOIS `nets` have free-text `description`/`name`
rather than structured entities).
"""
from __future__ import annotations

import sys

from ipwhois import IPWhois

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers.base import Resolver, register_resolver


@register_resolver("whois")
class WhoisResolver(Resolver):
    name = "whois"

    def __init__(self) -> None:
        self._timeout = get_settings().resolve.whois_timeout

    def resolve_one(self, key: str) -> dict:
        result = self._empty_result()
        try:
            data = IPWhois(key, timeout=self._timeout).lookup_whois()
        except Exception as e:
            print(_("[whois][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            return result

        nets = data.get("nets") or []
        first = nets[0] if nets and isinstance(nets[0], dict) else {}

        result["asn"] = str(data.get("asn") or "").strip()
        result["asn_descr"] = str(data.get("asn_description") or "").strip()
        result["country"] = str(first.get("country") or "").strip()

        # Contacts: best-effort — the first line of each net's description/name.
        names = []
        seen = set()
        for net in nets:
            if not isinstance(net, dict):
                continue
            raw = net.get("description") or net.get("name") or ""
            name = str(raw).strip().splitlines()[0].strip() if raw else ""
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        result["contacts"] = "; ".join(names)
        return result
