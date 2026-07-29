"""Shared RDAP/WHOIS helpers: safe nested-dict access and contact extraction.

Used by the `rdap` provider resolver and by `gunter` (which returns the same
RDAP `objects` structure via its HTTP API). Kept separate so the two don't
duplicate the (subtle) dict-vs-list handling of RDAP `objects`.
"""
from __future__ import annotations

from typing import Iterable


def _safe_get(data: dict, *keys, default=None):
    """Walk nested dicts by `keys`, returning `default` on any missing key or
    non-dict node."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def contacts_from_objects(objects) -> str:
    """Join the `contact.name` of every RDAP entity into a deduped "; " string.

    RDAP `objects` (ipwhois `lookup_rdap`, and gunter's passthrough) is a DICT
    keyed by entity handle; a list is also accepted defensively. Entities without
    a contact name are skipped; order is preserved.
    """
    if isinstance(objects, dict):
        entities: Iterable = objects.values()
    elif isinstance(objects, list):
        entities = objects
    else:
        return ""

    unique_contacts = []
    seen = set()
    for item in entities:
        if not isinstance(item, dict):
            continue
        name = _safe_get(item, "contact", "name", default="")
        name = str(name).strip() if name else ""
        if name and name not in seen:
            seen.add(name)
            unique_contacts.append(name)

    return "; ".join(unique_contacts)
