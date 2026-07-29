"""Tests for pyresolv/subnets.py — CIDR parsing, the octet prefix used for the
graylog server-side filter, subnet labels, row->subnet assignment, and the split
filename format."""
from __future__ import annotations

import ipaddress
from datetime import datetime

from pyresolv.subnets import (
    assign_label,
    octet_prefix,
    parse_cidrs,
    slice_filename,
    subnet_label,
)


def test_parse_cidrs_dedup_and_bare_ip():
    nets = parse_cidrs(["10.2.83.0/24", "10.2.83.0/24", "10.0.0.5", " ", "10.8.139.0/25"])
    assert [str(n) for n in nets] == ["10.2.83.0/24", "10.0.0.5/32", "10.8.139.0/25"]


def test_octet_prefix():
    assert octet_prefix(ipaddress.ip_network("10.2.83.0/24")) == "10.2.83."
    # a /25 uses the enclosing /24 prefix (string field can't express /25)
    assert octet_prefix(ipaddress.ip_network("10.2.83.0/25")) == "10.2.83."
    assert octet_prefix(ipaddress.ip_network("10.2.83.128/25")) == "10.2.83."
    assert octet_prefix(ipaddress.ip_network("10.8.0.0/16")) == "10.8."
    assert octet_prefix(ipaddress.ip_network("10.0.0.5/32")) == "10.0.0.5"


def test_subnet_label():
    assert subnet_label(ipaddress.ip_network("10.2.83.0/24")) == "10.2.83.0-24"
    assert subnet_label(ipaddress.ip_network("10.2.83.128/25")) == "10.2.83.128-25"


def test_assign_label_including_split_25_and_other():
    nets = parse_cidrs(["10.2.83.0/25", "10.2.83.128/25"])
    assert assign_label("10.2.83.100", nets) == "10.2.83.0-25"    # first half
    assert assign_label("10.2.83.200", nets) == "10.2.83.128-25"  # second half
    assert assign_label("9.9.9.9", nets) == "other"               # outside all
    assert assign_label("not-an-ip", nets) == "other"             # unparseable


def test_slice_filename_matches_example():
    base_now = datetime(2026, 7, 28, 12, 10)
    got = slice_filename("aggregation", "10.2.83.0-24", base_now, 5, 0, "d")
    assert got == "aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv"
