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


def _fn_from_vcard(vcard) -> str:
    """Extract the `fn` (full name) value from a raw RFC 7095 vcardArray:
    `["vcard", [ ["version", {}, "text", "4.0"], ["fn", {}, "text", "VK ADM"], ... ]]`."""
    if not (isinstance(vcard, list) and len(vcard) == 2 and isinstance(vcard[1], list)):
        return ""
    for item in vcard[1]:
        if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
            return str(item[3]).strip()
    return ""


def contacts_from_rdap_entities(entities) -> str:
    """Join the `fn` of every RFC 7483 entity (raw RDAP, not ipwhois-normalized)
    into a deduped "; " string, recursing into nested `entities`.

    Unlike `contacts_from_objects` (which reads ipwhois's `objects` dict with an
    already-parsed `contact.name`), this walks the raw RDAP `entities` LIST whose
    names live in each entity's `vcardArray` (as returned by the rdap.ss aggregator).
    """
    names = []
    seen = set()

    def _walk(ents) -> None:
        if not isinstance(ents, list):
            return
        for entity in ents:
            if not isinstance(entity, dict):
                continue
            name = _fn_from_vcard(entity.get("vcardArray"))
            if name and name not in seen:
                seen.add(name)
                names.append(name)
            _walk(entity.get("entities"))

    _walk(entities)
    return "; ".join(names)
