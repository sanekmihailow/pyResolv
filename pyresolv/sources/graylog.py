"""graylog source: ported build_payload + process_window from
get_dst_ip_ranges.py — the row-by-row output (search_after pagination) and the
IPv4 / non-private DstIP filter are ported 1:1, with no "simplifications".
"""
from __future__ import annotations

import ipaddress
import sys
from typing import Iterator, Optional

import requests

from pyresolv.config import get_settings
from pyresolv.i18n import _
from pyresolv.schema import COLLECT_COLUMNS
from pyresolv.sources.base import Source, register_source


@register_source("graylog")
class GraylogSource(Source):
    def __init__(self) -> None:
        self._settings = get_settings().require_graylog()
        self._url = f"{self._settings.url}/{self._settings.index}_*/_search"

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

        if s.src_ip_list:
            payload["query"]["bool"]["filter"].append({
                "terms": {
                    "SrcIP": s.src_ip_list,
                }
            })

        if s.src_ip_regex:
            # Multiple regexes are OR-combined: SrcIP must match at least one
            # pattern (minimum_should_match=1). For a single pattern the behavior
            # is identical to the old single regexp filter.
            payload["query"]["bool"]["filter"].append({
                "bool": {
                    "should": [
                        {"regexp": {"SrcIP": {"value": pattern}}}
                        for pattern in s.src_ip_regex
                    ],
                    "minimum_should_match": 1,
                }
            })

        if search_after is not None:
            payload["search_after"] = search_after

        return payload

    def fetch_window(self, time_gte: str, time_lt: str) -> Iterator[dict]:
        search_after = None
        batch_num = 0
        written_rows = 0
        window_label = f"{time_gte}..{time_lt}"

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
            batch_written = 0
            print(
                _("[%(w)s] Batch %(b)d: received %(n)d documents")
                % {"w": window_label, "b": batch_num, "n": len(hits)},
                file=sys.stderr,
            )

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

                    yield {col: source.get(col) for col in COLLECT_COLUMNS}
                    written_rows += 1
                    batch_written += 1

                except ValueError:
                    continue

            print(
                _("[%(w)s] Batch %(b)d: wrote %(n)d rows")
                % {"w": window_label, "b": batch_num, "n": batch_written},
                file=sys.stderr,
            )

            last_sort = hits[-1].get("sort")
            if not last_sort:
                break

            search_after = last_sort

        print(
            _("Done for window %(w)s: %(b)d batches, %(n)d rows")
            % {"w": window_label, "b": batch_num, "n": written_rows},
            file=sys.stderr,
        )
