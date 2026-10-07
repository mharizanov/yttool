from yttool.chunking import (
    assign_source_ids,
    chunk_segments,
    fmt_mmss,
    fmt_ts,
    segment_to_line,
)
from yttool.models import TranscriptSegment


def make_segments(n=5):
    return [TranscriptSegment(float(i * 10), float(i * 10 + 9), f"segment number {i}") for i in range(n)]


def test_assign_source_ids():
    segments = make_segments(3)
    index = assign_source_ids(segments)
    assert segments[0].source_id == "S0001"
    assert segments[2].source_id == "S0003"
    assert index["S0002"] is segments[1]


def test_segment_to_line_includes_timestamp():
    segments = make_segments(2)
    assign_source_ids(segments)
    assert segment_to_line(segments[1]) == "[S0002 00:00:10] segment number 1"


def test_chunk_segments_respects_budget():
    segments = [TranscriptSegment(float(i), None, "x" * 100) for i in range(50)]
    assign_source_ids(segments)
    chunks = chunk_segments(segments, max_chars=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_fmt():
    assert fmt_ts(3661.2) == "01:01:01"
    assert fmt_mmss(83.4) == "01:23"
    assert fmt_mmss(3661) == "1:01:01"
