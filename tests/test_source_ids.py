from yttool.mindmap import normalize_source_ids
from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.synthesize import (
    build_resolution_table,
    make_link_resolver,
    rewrite_source_ids,
)


def test_normalize_accepts_plain_and_prefixed():
    assert normalize_source_ids(["S0007", "V01.S0012", "junk"]) == ["S0007", "V01.S0012"]


def test_normalize_extracts_from_prose():
    assert normalize_source_ids("see V03.S0002 and S0009") == ["V03.S0002", "S0009"]


def test_rewrite_source_ids_recursive():
    summary = {
        "sections": [{"heading": "h", "bullets": [{"text": "t", "source_ids": ["S0001", "S0002"]}]}],
        "nodes": [{"label": "n", "source_ids": ["S0003"], "children": []}],
        "tl_dr": "S0001 mentioned in prose stays untouched",
    }
    out = rewrite_source_ids(summary, 3)
    assert out["sections"][0]["bullets"][0]["source_ids"] == ["V03.S0001", "V03.S0002"]
    assert out["nodes"][0]["source_ids"] == ["V03.S0003"]
    assert out["tl_dr"] == "S0001 mentioned in prose stays untouched"


def _result(index, video_id):
    segments = [TranscriptSegment(83.4, 90.0, "text", source_id="S0001")]
    return VideoResult(meta=VideoMeta(video_id=video_id, title="t"), index=index, segments=segments)


def test_resolution_table_and_links():
    results = [_result(1, "abc123"), _result(2, "def456")]
    table = build_resolution_table(results)
    assert table["V01.S0001"] == ("abc123", 83.4)
    resolver = make_link_resolver(table)
    seconds, url = resolver(["V02.S0001"])
    assert seconds == 83.4
    assert url == "https://www.youtube.com/watch?v=def456&t=83s"
    assert resolver(["V09.S9999"]) is None
