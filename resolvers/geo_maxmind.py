"""geo_maxmind resolver: country from a local MaxMind GeoLite2-City .mmdb
(the `geoip2` library, an optional extra). Fills only `country` (the geolocation
country name). If no `RESOLVE__MMDB_PATH` is set or geoip2/the DB is unavailable,
it yields nothing so the chain falls back to RDAP/WHOIS country codes.
"""
from __future__ import annotations

import sys

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.resolvers.base import Resolver, register_resolver


@register_resolver("geo_maxmind")
class GeoMaxmindResolver(Resolver):
    name = "geo_maxmind"

    def __init__(self) -> None:
        # Open the reader once at construction (single-threaded); geoip2 Readers
        # are safe for concurrent reads afterwards. Missing lib/path/DB -> None,
        # and resolve_one becomes a no-op.
        self._reader = None
        path = get_settings().resolve.mmdb_path
        if not path:
            return
        try:
            import geoip2.database

            self._reader = geoip2.database.Reader(path)
        except Exception as e:
            print(
                _("geo_maxmind disabled (geoip2/mmdb unavailable): %(err)s") % {"err": e},
                file=sys.stderr,
            )

    def resolve_one(self, key: str) -> dict:
        result = self._empty_result()
        if self._reader is None:
            return result
        try:
            resp = self._reader.city(key)
            result["country"] = str(resp.country.name or "").strip()
        except Exception as e:
            print(_("[geo_maxmind][%(ip)s] lookup failed: %(err)s") % {"ip": key, "err": e}, file=sys.stderr)
        return result
