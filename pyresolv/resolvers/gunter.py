"""gunter resolver: ported the HTTP specifics from api/gunter/resolve.py
(_fetch_country, _fetch_whois, _extract_contacts). The shared mechanics
(ThreadPool, cache, idempotency) live in resolvers/base.py.
"""
from __future__ import annotations

import sys

import requests

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers._rdap import _safe_get, contacts_from_objects
from pyresolv.resolvers.base import Resolver, register_resolver


def _extract_contacts(whois_data: dict) -> str:
    """Contacts from a gunter /whois response (RDAP `objects` under `ip_whois`)."""
    return contacts_from_objects(_safe_get(whois_data, "ip_whois", "objects", default={}))


@register_resolver("gunter")
class GunterResolver(Resolver):
    name = "gunter"

    def __init__(self) -> None:
        self._settings = get_settings().require_gunter()

    def _fetch_country(self, session: requests.Session, ip: str) -> str:
        url = f"{self._settings.base_url}/geo-lookup/{ip}"
        response = session.get(url, timeout=self._settings.request_timeout)
        response.raise_for_status()
        data = response.json()
        return _safe_get(data, "country", "name", default="") or ""

    def _fetch_whois(self, session: requests.Session, ip: str) -> dict:
        url = f"{self._settings.base_url}/whois/{ip}"
        response = session.get(url, timeout=self._settings.request_timeout)
        response.raise_for_status()
        data = response.json()

        return {
            "asn": _safe_get(data, "ip_whois", "asn", default="") or "",
            "asn_descr": _safe_get(data, "ip_whois", "asn_description", default="") or "",
            "contacts": _extract_contacts(data),
        }

    def resolve_one(self, key: str) -> dict:
        result = self._empty_result()
        session = requests.Session()

        try:
            result["country"] = self._fetch_country(session, key)
        except Exception as e:
            print(_("[GUNTER][%(key)s] geo-lookup error: %(err)s") % {"key": key, "err": e}, file=sys.stderr)

        try:
            whois_data = self._fetch_whois(session, key)
            result.update(whois_data)
        except Exception as e:
            print(_("[GUNTER][%(key)s] whois error: %(err)s") % {"key": key, "err": e}, file=sys.stderr)

        return result
