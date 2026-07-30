"""Diagnose why `collect --source graylog` returns 0 rows.

Runs the EXACT query that GraylogSource builds, then strips it clause by clause.
The first layer that returns > 0 hits tells you which clause is filtering
everything out. Also prints the field names of a sample document so you can
check that `timestamp` / `SrcIP` / `DstIP` / `streams` actually exist and are
spelled the way the query expects.

Run it on a host that has the prod `.env` (it reads the same config as
`pyresolv`):

    python -m tools.diagnose_graylog
    python -m tools.diagnose_graylog --start 5 --end 0 --time-unit d

It only issues read-only `_count` / `_search?size=1` requests.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys

import requests

from pyresolv.sources.base import build_time_windows
from pyresolv.sources.graylog import GraylogSource


def _count(url: str, timeout: int, query: dict) -> int:
    """POST a query to the _count endpoint and return the hit count (-1 on error)."""
    count_url = url.replace("/_search", "/_count")
    try:
        resp = requests.post(
            count_url,
            headers={"Content-Type": "application/json"},
            json={"query": query},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("count", -1)
    except requests.RequestException as e:
        print(f"    ERROR: {e}", file=sys.stderr)
        return -1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--time-unit", choices=["d", "h"], default="d")
    args = parser.parse_args()

    src = GraylogSource()
    s = src._settings
    timeout = s.request_timeout

    print("=== config ===")
    print(f"URL           : {src._url}")
    print(f"index pattern : {s.index}_*")
    print(f"stream_id     : {s.stream_id}")
    print(f"src_ip_list   : {s.src_ip_list}")
    print(f"src_ip_cidr   : {s.src_ip_cidr}")
    print()

    # A single window to test against.
    gte, lt, *_ = build_time_windows(args.start, args.end, args.time_unit)[0]
    print(f"=== test window: {gte} .. {lt} ===\n")

    # 0) How many docs does the index pattern hold at all?
    print("[0] index pattern, match_all (ignores every filter)")
    print(f"    hits = {_count(src._url, timeout, {'match_all': {}})}\n")

    # 1) The full query, exactly as collect builds it.
    full = src._build_payload(gte, lt)["query"]
    print("[1] FULL query (time + exists + streams + src filters)")
    print(f"    hits = {_count(src._url, timeout, full)}\n")

    bool_q = full["bool"]

    # 2) Drop the SrcIP allowlist / CIDR-prefix filters (keep streams + time + exists).
    no_src = copy.deepcopy(bool_q)
    no_src["filter"] = [f for f in no_src["filter"] if "term" in f and "streams" in f.get("term", {})]
    print("[2] without SrcIP list / CIDR filters")
    print(f"    hits = {_count(src._url, timeout, {'bool': no_src})}\n")

    # 3) Also drop the streams filter (keep only time + exists).
    no_streams = copy.deepcopy(no_src)
    no_streams["filter"] = []
    print("[3] also without the streams filter")
    print(f"    hits = {_count(src._url, timeout, {'bool': no_streams})}\n")

    # 4) Also drop exists(SrcIP)/exists(DstIP) (keep only the time range).
    time_only = {"bool": {"must": [bool_q["must"][0]]}}
    print("[4] time range only (drops exists SrcIP/DstIP)")
    print(f"    hits = {_count(src._url, timeout, time_only)}\n")

    # 5) streams filter alone, no time range (is any data in this stream, ever?).
    streams_only = {"term": {"streams": s.stream_id}}
    print("[5] streams filter only, no time range")
    print(f"    hits = {_count(src._url, timeout, streams_only)}\n")

    # 6) Sample document: what field names actually exist?
    print("[6] sample document field names (check spelling/case of the query fields)")
    try:
        resp = requests.post(
            f"{src._url}?size=1",
            headers={"Content-Type": "application/json"},
            json={"query": {"match_all": {}}},
            timeout=timeout,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        if hits:
            source = hits[0].get("_source", {})
            print("    _source keys:", sorted(source.keys()))
            for f in ("timestamp", "SrcIP", "DstIP", "streams"):
                mark = "OK " if f in source else "MISSING"
                print(f"      [{mark}] {f}: {source.get(f)!r}")
        else:
            print("    no documents in the index pattern at all")
    except requests.RequestException as e:
        print(f"    ERROR: {e}", file=sys.stderr)

    print("\n=== how to read this ===")
    print("The FIRST layer above that jumps from 0 to > 0 is the clause that")
    print("filters everything out:")
    print("  [1]>0            -> query is fine; 0 rows must come from the client-side")
    print("                      IPv4 / non-private DstIP / exact-CIDR filter (see below)")
    print("  [1]=0, [2]>0     -> your SrcIP list / CIDR is wrong (no such source IPs)")
    print("  [2]=0, [3]>0     -> stream_id is wrong (GRAYLOG__STREAM_ID)")
    print("  [3]=0, [4]>0     -> the SrcIP/DstIP field names differ (see [6])")
    print("  [4]=0, [0]>0     -> the timestamp field/format is wrong (see [6])")
    print("  [0]=0            -> wrong index pattern or URL (nothing matches at all)")
    print("If [1]>0 but collect still wrote 0 rows: every hit is being dropped")
    print("client-side (DstIP private, non-IPv4, or SrcIP outside the exact CIDR).")


if __name__ == "__main__":
    main()
