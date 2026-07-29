"""Tests for the gunter WHOIS contact extraction (`_extract_contacts`).

Gunter returns the raw ipwhois RDAP structure, where `ip_whois.objects` is a
DICT keyed by entity handle (not a list). These tests pin that shape and the
dedup/ordering, plus a legacy list shape and the empty/malformed cases. The
module function is pure, so no network or gunter config is needed."""
from __future__ import annotations

from pyresolv.resolvers.gunter import _extract_contacts


def _rdap(objects):
    return {"ip_whois": {"asn": "AS15169", "asn_description": "GOOGLE", "objects": objects}}


def test_objects_dict_keyed_by_handle():
    # The real gunter/RDAP shape: objects is a dict keyed by handle.
    objects = {
        "GOGL": {"contact": {"name": "Google LLC"}, "roles": ["registrant"]},
        "ABUSE5250-ARIN": {"contact": {"name": "Abuse"}},
        "GOGL2": {"contact": {"name": "Google LLC"}},   # duplicate name
        "NOCONTACT": {"roles": ["technical"]},           # no contact -> skipped
    }
    # dict order is insertion-ordered in Python 3.7+
    assert _extract_contacts(_rdap(objects)) == "Google LLC; Abuse"


def test_objects_list_legacy_shape_still_works():
    objects = [
        {"contact": {"name": "Alice"}},
        {"contact": {"name": "Bob"}},
        {"contact": {"name": "Alice"}},  # dedup
        "not-a-dict",                    # skipped
    ]
    assert _extract_contacts(_rdap(objects)) == "Alice; Bob"


def test_empty_and_missing():
    assert _extract_contacts(_rdap({})) == ""
    assert _extract_contacts(_rdap([])) == ""
    assert _extract_contacts({"ip_whois": {}}) == ""
    assert _extract_contacts({}) == ""
    # objects present but None / wrong type
    assert _extract_contacts(_rdap(None)) == ""
    assert _extract_contacts(_rdap("nope")) == ""


def test_blank_names_skipped():
    objects = {
        "A": {"contact": {"name": "  "}},   # whitespace-only -> skipped
        "B": {"contact": {"name": "Real"}},
        "C": {"contact": {}},               # no name -> skipped
    }
    assert _extract_contacts(_rdap(objects)) == "Real"
