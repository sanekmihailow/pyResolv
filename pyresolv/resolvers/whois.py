"""whois resolver: ASN / description / country / contacts via legacy port-43
WHOIS (ipwhois `lookup_whois`). A fallback for cases RDAP doesn't cover; contact
data is coarser than RDAP (WHOIS `nets` have free-text `description`/`name`
rather than structured entities).

Fallback tier: when the ipwhois whois returns nothing and RESOLVE__TCINET is on,
query the TCI domain whois (whois.tcinet.ru:43) for .ru/.su/.рф. That is a DOMAIN
registry (keyed by url_domain), so it only yields data for a domain key — for an
IP it returns nothing.
"""
from __future__ import annotations

import socket
import sys

from ipwhois import IPWhois

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers.base import Resolver, register_resolver

# TCI-operated zones (Cyrillic + their punycode forms) served by whois.tcinet.ru.
_TCINET_ZONES = (".ru", ".su", ".рф", ".дети", ".tatar", ".xn--p1ai", ".xn--d1acj3b")


@register_resolver("whois")
class WhoisResolver(Resolver):
    name = "whois"

    def __init__(self) -> None:
        resolve = get_settings().resolve
        self._timeout = resolve.whois_timeout
        self._tcinet = resolve.tcinet
        self._tcinet_host = resolve.tcinet_host
        self._tcinet_timeout = resolve.tcinet_timeout

    def resolve_one(self, key: str) -> dict:
        try:
            data = IPWhois(key, timeout=self._timeout).lookup_whois()
        except Exception as e:
            print(_("[whois][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
            data = None

        result = self._extract_ipwhois(data) if data else self._empty_result()

        # Fallback: TCI domain whois for .ru/.su/.рф when ipwhois returned nothing.
        if self._tcinet and not any(result.values()):
            raw = self._tcinet_lookup(key)
            if raw:
                result = self._extract_tcinet(raw)

        return result

    def _extract_ipwhois(self, data: dict) -> dict:
        result = self._empty_result()
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

    def _tcinet_lookup(self, key: str) -> str | None:
        """Query TCI domain whois for a .ru/.su/.рф domain; return the raw text or
        None (not a TCI zone / error). IDN zones are punycode-encoded first."""
        domain = str(key).strip().lower().rstrip(".")
        if not domain or "." not in domain or not domain.endswith(_TCINET_ZONES):
            return None
        try:
            query = domain.encode("idna").decode("ascii")  # punycode for .рф/.дети
        except Exception:
            query = domain
        try:
            return self._tcinet_query(query)
        except Exception as e:
            print(_("[whois][%(key)s] tcinet lookup failed: %(err)s") % {"key": key, "err": e}, file=sys.stderr)
            return None

    def _tcinet_query(self, query: str) -> str:
        """Raw port-43 whois exchange (separated so tests can mock the network)."""
        chunks = []
        with socket.create_connection((self._tcinet_host, 43), timeout=self._tcinet_timeout) as sock:
            sock.sendall((query + "\r\n").encode("ascii"))
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _extract_tcinet(self, text: str) -> dict:
        """Parse `key: value` lines of a .ru/.su whois into RESOLVE_COLUMNS. Domain
        whois has no ASN/country — only `contacts` (org / registrar / nserver)."""
        result = self._empty_result()
        fields: dict[str, list[str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("%") or ":" not in line:
                continue
            k, _sep, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if v:
                fields.setdefault(k, []).append(v)

        parts: list[str] = []
        for field in ("org", "registrar", "nserver"):
            for v in fields.get(field, []):
                if v not in parts:
                    parts.append(v)
        result["contacts"] = "; ".join(parts)
        # Cache-TTL hint from the domain's paid-till; not written to the CSV.
        paid_till = fields.get("paid-till", [])
        if paid_till:
            result["expires"] = paid_till[0]
        return result
