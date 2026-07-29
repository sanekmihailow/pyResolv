"""Subnet helpers (stdlib `ipaddress`) shared by the graylog source and the
`aggregate --out-dir` split.

`GRAYLOG__SRC_IP_CIDR` is a list of source subnets in CIDR (e.g. "10.2.83.0/24",
"10.2.83.0/25"). It serves two purposes:
  - collect filtering: narrow which SrcIPs are fetched from graylog;
  - aggregate --out-dir: bucket the final aggregation into one file per subnet.

The graylog `SrcIP` field is a STRING holding a plain dotted-quad IPv4 (no mask),
so a server-side CIDR `term` query does not apply. Collect uses a server-side
`prefix` query built from `octet_prefix()` (narrows to the enclosing /24) and an
exact client-side `ip in net` check for the precise mask. Splitting is a pure
client-side `ipaddress` operation.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import List


def parse_cidrs(values: List[str]) -> List[ipaddress.IPv4Network]:
    """Parse CIDR strings into networks, deduped, preserving order. A bare IP
    (no '/') becomes a /32. Non-strict, so host bits are allowed."""
    networks: List[ipaddress.IPv4Network] = []
    seen = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        net = ipaddress.ip_network(value, strict=False)
        if net not in seen:
            seen.add(net)
            networks.append(net)
    return networks


def octet_prefix(net: ipaddress.IPv4Network) -> str:
    """Largest octet-aligned dotted-quad string prefix containing `net`, for the
    graylog server-side `prefix` query on the string SrcIP field.

    Takes `prefixlen // 8` leading octets of the network address:
      10.2.83.0/24 -> "10.2.83."   (exact)
      10.2.83.0/25 -> "10.2.83."   (coarser: the enclosing /24; the exact /25 is
                                    then pinned down client-side)
      10.8.0.0/16  -> "10.8."
      10.0.0.1/32  -> "10.0.0.1"   (full IP, no trailing dot)
    """
    octets = str(net.network_address).split(".")
    keep = net.prefixlen // 8
    if keep >= 4:
        return ".".join(octets)  # /32 -> full IP, no trailing dot
    return ".".join(octets[:keep]) + "."


def subnet_label(net: ipaddress.IPv4Network) -> str:
    """Filename-safe subnet label: '10.2.83.0/24' -> '10.2.83.0-24'."""
    return str(net).replace("/", "-")


def assign_label(ip_str: str, networks: List[ipaddress.IPv4Network]) -> str:
    """Label of the first network containing `ip_str`, else 'other' (also on an
    unparseable address)."""
    try:
        ip = ipaddress.ip_address(str(ip_str).strip())
    except ValueError:
        return "other"
    for net in networks:
        if ip in net:
            return subnet_label(net)
    return "other"


def slice_filename(
    prefix: str,
    label: str,
    base_now: datetime,
    start: int,
    end: int,
    time_unit: str,
) -> str:
    """Build a split filename with the time slice, e.g.
    'aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv'.

    The from/to dates are computed like `collect` (via `shift_now`), and the
    trailing `time-HH-MM` is the run time (`base_now`)."""
    # Lazy import: pyresolv.sources.base pulls the sources package __init__ (which
    # registers graylog, which imports this module) — importing at module level
    # would create a cycle.
    from pyresolv.sources.base import shift_now

    d_from = shift_now(base_now, start, time_unit).strftime("%Y-%m-%d")
    d_to = shift_now(base_now, end, time_unit).strftime("%Y-%m-%d")
    hh_mm = base_now.strftime("%H-%M")
    return f"{prefix}_{label}__{d_from}__{d_to}__time-{hh_mm}.csv"
