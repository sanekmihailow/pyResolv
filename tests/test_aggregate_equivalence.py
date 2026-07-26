"""Key equivalence test: aggregate() with --no-streaming and with --streaming
(at various chunk_size, including smaller than the row count, to guarantee that
duplicate groups get split across different chunks) must produce byte-identical
output: the same set of groups, the same count, the same sort order, the same
dtypes.
"""
from __future__ import annotations

import io

import pytest

from pyresolv.stages.aggregate import aggregate

SAMPLE_TRIMMED_CSV = """SrcIP,DstIP,DstPort,ac_action,ac_rule_name,url_domain,url_path
10.2.83.129,8.8.8.8,443,allow,rule1,google.com,/
10.2.83.129,8.8.8.8,443,allow,rule1,google.com,/
10.2.83.130,1.1.1.1,443,allow,rule2,cloudflare.com,/
10.2.83.130,1.1.1.1,80,deny,rule3,,/blocked
10.2.83.129,8.8.8.8,443,allow,rule1,google.com,/
10.2.83.130,9.9.9.9,53,allow,NA,example.org,/
10.2.83.130,9.9.9.9,53,allow,NA,example.org,/
10.2.83.129,2.2.2.2,443,allow,rule4,,/
10.2.83.130,1.1.1.1,443,allow,rule2,cloudflare.com,/
10.2.83.129,8.8.8.8,8080,allow,rule5,google.com,/x
"""


@pytest.fixture
def sample_csv(tmp_path):
    p = tmp_path / "trimmed.csv"
    p.write_text(SAMPLE_TRIMMED_CSV, encoding="utf-8")
    return p


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 7, 1000])
def test_streaming_matches_full_on_various_chunk_sizes(sample_csv, tmp_path, chunk_size):
    full_out = tmp_path / f"full_{chunk_size}.csv"
    streaming_out = tmp_path / f"streaming_{chunk_size}.csv"

    aggregate(str(sample_csv), str(full_out), streaming=False)
    aggregate(str(sample_csv), str(streaming_out), streaming=True, chunk_size=chunk_size)

    full_bytes = full_out.read_bytes()
    streaming_bytes = streaming_out.read_bytes()

    assert full_bytes == streaming_bytes, (
        f"chunk_size={chunk_size}: full and streaming output differ\n"
        f"full:\n{full_bytes.decode()}\nstreaming:\n{streaming_bytes.decode()}"
    )


def test_aggregate_output_shape_and_sort_order(sample_csv, tmp_path):
    out = tmp_path / "aggregated.csv"
    n = aggregate(str(sample_csv), str(out), streaming=False)

    lines = out.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    assert header == ["SrcIP", "DstIP", "DstPort", "ac_action", "url_domain", "ac_rule_name", "count"]

    rows = [dict(zip(header, line.split(","))) for line in lines[1:]]
    assert n == len(rows) == 6

    counts = [int(r["count"]) for r in rows]
    assert counts == sorted(counts, reverse=True), "count must be sorted descending"

    # NA (a literal rule-name value) must survive as text, not be treated as missing.
    na_row = [r for r in rows if r["ac_rule_name"] == "NA"]
    assert len(na_row) == 1
    assert na_row[0]["count"] == "2"

    # Empty url_domain stays an empty string, not "nan".
    assert "nan" not in out.read_text(encoding="utf-8").lower()


def test_streaming_with_chunk_size_larger_than_file(sample_csv, tmp_path):
    """Edge case: chunk_size > number of rows -> a single chunk, must still match."""
    full_out = tmp_path / "full.csv"
    streaming_out = tmp_path / "streaming.csv"
    aggregate(str(sample_csv), str(full_out), streaming=False)
    aggregate(str(sample_csv), str(streaming_out), streaming=True, chunk_size=10_000)
    assert full_out.read_bytes() == streaming_out.read_bytes()


def _counts(out_path) -> list[int]:
    lines = out_path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    idx = header.index("count")
    return [int(line.split(",")[idx]) for line in lines[1:]]


# Groups in SAMPLE_TRIMMED_CSV have counts: 3, 2, 2, 1, 1, 1 (6 groups total).
@pytest.mark.parametrize(
    "min_count, expected_counts",
    [
        (1, [3, 2, 2, 1, 1, 1]),   # 1 = no filtering, everything stays
        (2, [3, 2, 2]),            # count < 2 (the three ones) dropped
        (3, [3]),                  # only the most frequent group remains
        (4, []),                   # threshold above any count -> header only
    ],
)
def test_min_count_drops_groups_below_threshold(sample_csv, tmp_path, min_count, expected_counts):
    out = tmp_path / f"agg_{min_count}.csv"
    n = aggregate(str(sample_csv), str(out), streaming=False, min_count=min_count)

    counts = _counts(out)
    assert counts == expected_counts
    assert n == len(expected_counts)
    # No remaining group may be below the threshold.
    assert all(c >= min_count for c in counts)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 1000])
@pytest.mark.parametrize("min_count", [1, 2, 3, 4])
def test_min_count_streaming_matches_full(sample_csv, tmp_path, chunk_size, min_count):
    """The filter is applied after the full aggregation, so streaming and full
    must produce a byte-identical result at any chunk_size — even when a
    duplicate group is split across different chunks."""
    full_out = tmp_path / f"full_{min_count}_{chunk_size}.csv"
    streaming_out = tmp_path / f"streaming_{min_count}_{chunk_size}.csv"

    aggregate(str(sample_csv), str(full_out), streaming=False, min_count=min_count)
    aggregate(str(sample_csv), str(streaming_out), streaming=True, chunk_size=chunk_size, min_count=min_count)

    assert full_out.read_bytes() == streaming_out.read_bytes()


def test_min_count_1_equals_no_filter(sample_csv, tmp_path):
    """min_count=1 (default) must not change anything vs. no filter at all."""
    default_out = tmp_path / "default.csv"
    explicit_out = tmp_path / "explicit.csv"
    aggregate(str(sample_csv), str(default_out), streaming=False)
    aggregate(str(sample_csv), str(explicit_out), streaming=False, min_count=1)
    assert default_out.read_bytes() == explicit_out.read_bytes()


def test_min_count_can_empty_result_but_keeps_header(sample_csv, tmp_path):
    """Threshold above every count -> empty result, but the header is kept."""
    out = tmp_path / "empty.csv"
    n = aggregate(str(sample_csv), str(out), streaming=False, min_count=999)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert n == 0
    assert len(lines) == 1  # header only
    assert lines[0].split(",")[-1] == "count"
