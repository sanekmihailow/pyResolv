"""graylog source: ported build_payload + process_window from
get_dst_ip_ranges.py — the row-by-row output (search_after pagination) and the
IPv4 / non-private DstIP filter are ported 1:1, with no "simplifications".
"""
from __future__ import annotations

import ipaddress
import sys
from typing import Iterator, Optional

import requests
from tqdm import tqdm

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.schema import COLLECT_COLUMNS
from pyresolv.sources.base import Source, register_source
from pyresolv.subnets import octet_prefix, parse_cidrs


@register_source("graylog")
class GraylogSource(Source):
    def __init__(self) -> None:
        self._settings = get_settings().require_graylog()
        self._url = f"{self._settings.url}/{self._settings.index}_*/_search"
        # CIDR subnets from GRAYLOG__SRC_IP_CIDR: used for the server-side prefix
        # pre-filter and the exact client-side membership check below.
        self._networks = parse_cidrs(self._settings.src_ip_cidr)

    def _src_ip_allowed(self, src_ip_str: str, src_ip_obj) -> bool:
        """Exact client-side counterpart of the server-side SrcIP filter, combining
        the SRC_IP_LIST allowlist and the SRC_IP_CIDR subnets per SRC_IP_MATCH_MODE.
        No filter configured -> everything is allowed."""
        s = self._settings
        checks = []
        if s.src_ip_list:
            checks.append(src_ip_str in s.src_ip_list)
        if self._networks:
            checks.append(any(src_ip_obj in net for net in self._networks))
        if not checks:
            return True
        return all(checks) if s.src_ip_match_mode == "and" else any(checks)

    def _build_payload(self, time_gte: str, time_lt: str, search_after: Optional[list] = None) -> dict:
        s = self._settings
        payload = {
            "size": s.search_size,
            "_source": list(COLLECT_COLUMNS),
            "query": {
                "bool": {
                    "must": [
                        {
                            "range": {
                                "timestamp": {
                                    "gte": time_gte,
                                    "lt": time_lt,
                                }
                            }
                        },
                        {"exists": {"field": "SrcIP"}},
                        {"exists": {"field": "DstIP"}},
                    ],
                    "filter": [
                        {"term": {"streams": s.stream_id}},
                    ],
                }
            },
            "sort": [
                {"timestamp": "asc"},
            ],
        }

        # Two optional SrcIP sub-filters: an exact allowlist (SRC_IP_LIST -> terms)
        # and a subnet filter (SRC_IP_CIDR). SrcIP is a STRING field holding a plain
        # dotted-quad IPv4 (no mask), so a CIDR term query does not apply — we narrow
        # server-side with a `prefix` query on the octet-aligned prefix of each subnet
        # (10.2.83.0/25 -> "10.2.83.", i.e. the enclosing /24), OR-combined; the exact
        # mask is enforced client-side in fetch_window.
        src_ip_clauses = []
        if s.src_ip_list:
            src_ip_clauses.append({"terms": {"SrcIP": s.src_ip_list}})
        if self._networks:
            src_ip_clauses.append({
                "bool": {
                    "should": [
                        {"prefix": {"SrcIP": octet_prefix(net)}}
                        for net in self._networks
                    ],
                    "minimum_should_match": 1,
                }
            })

        if src_ip_clauses:
            # With one sub-filter (or SRC_IP_MATCH_MODE=and) the clauses go straight
            # into `filter`, which ANDs them. With both and mode=or (the default),
            # wrap them in a single bool.should so a SrcIP matching EITHER passes.
            if len(src_ip_clauses) == 1 or s.src_ip_match_mode == "and":
                payload["query"]["bool"]["filter"].extend(src_ip_clauses)
            else:
                payload["query"]["bool"]["filter"].append({
                    "bool": {"should": src_ip_clauses, "minimum_should_match": 1}
                })

        if search_after is not None:
            payload["search_after"] = search_after

        return payload

    def fetch_window(self, time_gte: str, time_lt: str) -> Iterator[dict]:
        search_after = None
        batch_num = 0
        written_rows = 0
        window_label = f"{time_gte}..{time_lt}"

        # A single live status line per window (like `resolve`'s tqdm bar) instead
        # of two printed lines per 5k-doc batch: bar advances by documents received,
        # the `wrote` postfix tracks rows kept after the client-side filter.
        bar = tqdm(
            desc=_("collect %(w)s") % {"w": window_label},
            unit="doc", unit_scale=True, file=sys.stderr, leave=False,
        )

        while True:
            payload = self._build_payload(time_gte, time_lt, search_after)
            response = requests.post(
                self._url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=self._settings.request_timeout,
            )
            response.raise_for_status()
            data = response.json()
            hits = data.get("hits", {}).get("hits", [])

            if not hits:
                break

            batch_num += 1

            for hit in hits:
                source = hit.get("_source", {})
                src_ip_str = source.get("SrcIP")
                dst_ip_str = source.get("DstIP")

                if not src_ip_str or not dst_ip_str:
                    continue

                try:
                    src_ip_obj = ipaddress.ip_address(src_ip_str)
                    dst_ip_obj = ipaddress.ip_address(dst_ip_str)

                    if src_ip_obj.version != 4 or dst_ip_obj.version != 4:
                        continue
                    if dst_ip_obj.is_private:
                        continue
                    # Exact SrcIP check mirroring the server-side filter: the
                    # `prefix` query only narrows to the enclosing /24, so the
                    # precise mask (e.g. /25) is pinned down here, combined with
                    # the allowlist per SRC_IP_MATCH_MODE.
                    if not self._src_ip_allowed(src_ip_str, src_ip_obj):
                        continue

                    yield {col: source.get(col) for col in COLLECT_COLUMNS}
                    written_rows += 1

                except ValueError:
                    continue

            bar.update(len(hits))
            bar.set_postfix(wrote=written_rows)

            last_sort = hits[-1].get("sort")
            if not last_sort:
                break

            search_after = last_sort

        bar.close()
        print(
            _("Done for window %(w)s: %(b)d batches, %(n)d rows")
            % {"w": window_label, "b": batch_num, "n": written_rows},
            file=sys.stderr,
        )
