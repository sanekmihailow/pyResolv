"""Resolver plugins for the `resolve` stage."""
from pyresolv.resolvers.base import RESOLVERS, Resolver, get_resolver, register_resolver

# The import registers the built-in resolvers in the RESOLVERS registry.
from pyresolv.resolvers import gunter  # noqa: E402,F401

__all__ = ["RESOLVERS", "Resolver", "get_resolver", "register_resolver"]
