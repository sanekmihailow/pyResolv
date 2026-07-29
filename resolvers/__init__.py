"""Resolver plugins for the `resolve` stage."""
from pyresolv.resolvers.base import RESOLVERS, Resolver, get_resolver, register_resolver

# The imports register the built-in resolvers in the RESOLVERS registry.
from pyresolv.resolvers import gunter  # noqa: E402,F401
from pyresolv.resolvers import geo_maxmind, rdap, whois  # noqa: E402,F401
from pyresolv.resolvers import default_chain  # noqa: E402,F401  (imports the three above)

__all__ = ["RESOLVERS", "Resolver", "get_resolver", "register_resolver"]
