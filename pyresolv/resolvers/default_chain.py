"""default resolver: the composite chain GEO -> RDAP -> WHOIS.

Runs the provider resolvers in order, filling each RESOLVE_COLUMN only from the
first provider that returns a non-empty value, and stops early once every column
is filled (so WHOIS isn't queried when RDAP already covered everything). This is
the self-contained replacement for the external gunter service, and the default
resolver (`settings.default_resolver`).
"""
from __future__ import annotations

import sys

from pyresolv.i18n import _
from pyresolv.resolvers.base import Resolver, register_resolver
from pyresolv.resolvers.geo_maxmind import GeoMaxmindResolver
from pyresolv.resolvers.rdap import RdapResolver
from pyresolv.resolvers.whois import WhoisResolver
from pyresolv.schema import RESOLVE_COLUMNS


@register_resolver("default")
class DefaultResolver(Resolver):
    name = "default"

    def __init__(self) -> None:
        # Order = priority: MaxMind geolocation for country, then RDAP for
        # asn/descr/contacts (+ country code), then WHOIS to backfill.
        self._providers = [GeoMaxmindResolver(), RdapResolver(), WhoisResolver()]

    def resolve_one(self, key: str) -> dict:
        result = self._empty_result()
        for provider in self._providers:
            if all(result[col] for col in RESOLVE_COLUMNS):
                break
            try:
                partial = provider.resolve_one(key)
            except Exception as e:
                print(
                    _("[default][%(name)s][%(ip)s] provider failed: %(err)s")
                    % {"name": getattr(provider, "name", "?"), "ip": key, "err": e},
                    file=sys.stderr,
                )
                continue
            for col in RESOLVE_COLUMNS:
                if not result[col] and partial.get(col):
                    result[col] = partial[col]
        return result
