"""Single source of truth for the CSV data schema shared by all pyresolv stages.

Previously `DROP_COLS`/`GROUP_COLS` were duplicated in get_dst_ip_ranges.py and
resolver.py and could drift apart when one file was edited without the other.
This is now the only place that defines them.
"""
from __future__ import annotations

# Columns the source emits from the `collect` stage. The order is fixed and
# matches the order of the "_source" fields in the OpenSearch query and the
# column order that process_window/build_payload used to write.
COLLECT_COLUMNS = [
    "timestamp",
    "SrcIP",
    "DstIP",
    "DstPort",
    "ac_action",
    "ac_rule_name",
    "url_domain",
    "url_path",
]

# Columns dropped by the `trim` stage.
DROP_COLS = ["timestamp", "SrcPort", "source", "message"]

# Group-by columns for the `aggregate` stage (order = grouping priority).
GROUP_COLS = ["SrcIP", "DstIP", "DstPort", "ac_action", "url_domain", "ac_rule_name"]

# Sort order for the `aggregate` result: count descending, everything else
# ascending. Intersected with the columns actually present, so partial schemas
# (not all GROUP_COLS present in the input CSV) don't break the sort.
SORT_CANDIDATES = ["count", "ac_action", "SrcIP", "DstIP", "DstPort"]

# Columns the `resolve` stage adds/fills based on the key column
# (DstIP by default).
RESOLVE_COLUMNS = ["country", "asn", "asn_descr", "contacts"]

DEFAULT_KEY_COLUMN = "DstIP"

DEFAULT_TRIM_CHUNKSIZE = 10_000
DEFAULT_AGGREGATE_CHUNKSIZE = 500_000
DEFAULT_RESOLVE_WORKERS = 3

# Unified CSV read parameters for ALL stages that use pandas (trim, aggregate,
# resolve). This matters for two reasons:
#
# 1. NA handling: keep_default_na=False guarantees that an empty field stays an
#    empty string rather than becoming NaN — and, more importantly, that real
#    values like "NA"/"NULL"/"N/A" (which can legitimately occur in
#    ac_rule_name or url_domain) are not silently treated as missing and erased.
#    The original api/gunter/resolve.py already did this (keep_default_na=False);
#    here it is applied uniformly at EVERY step of the CSV pipe between stages.
#
# 2. dtype=str: without it, pandas infers column types from the data. With
#    chunked reads (aggregate --streaming) a column's type can differ from chunk
#    to chunk (e.g. DstPort — int64 in a chunk with no gaps, float64 in a chunk
#    with a gap), which can make --streaming and full-load produce different
#    results (different value formatting, different sort order). Forcing
#    dtype=str for ALL fields removes this source of divergence and makes
#    aggregate/aggregate --streaming guaranteed identical.
PANDAS_READ_KWARGS = dict(
    low_memory=False,
    keep_default_na=False,
    dtype=str,
)
