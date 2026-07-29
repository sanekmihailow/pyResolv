"""Data source plugins for the `collect` stage."""
from pyresolv.sources.base import SOURCES, Source, get_source, register_source

# The import registers the built-in sources in the SOURCES registry.
from pyresolv.sources import graylog  # noqa: E402,F401

__all__ = ["SOURCES", "Source", "get_source", "register_source"]
