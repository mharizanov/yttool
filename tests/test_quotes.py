"""Quotable lines: normalization of the LLM's quotes and the transcript check that
makes them citable (invented or reworded lines are dropped, like invented ids)."""

from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.report import render_video_md
from yttool.summarize import (
    assign_source_ids,
    normalize_quotes,
    normalize_video_summary,
    verify_quotes,
)
from yttool.synthesize import build_quote_pool, make_link_resolver, normalize_report


def segments():
    segs = [
        TranscriptSegment(0.0, 5.0, "welcome everyone to the workshop on hive care"),
        TranscriptSegment(10.0, 20.0, "a hive inspection follows a routine and most colony losses trace"),
        TranscriptSegment(20.0, 30.0, "back to inspections that somebody skipped when they were busy"),
        TranscriptSegment(30.5, 40.0, "so buy a good smoker before you buy a second hive"),
    ]
    assign_source_ids(segs)
    return segs


def test_normalize_quotes_filters_and_dedupes():
    raw = [
        {"quote": "  “Most colony losses trace back to inspections somebody skipped.” ", "speaker": "Maria", "why": "the thesis", "source_ids": ["S0002"]},
        {"quote": "Most colony losses trace back to inspections somebody skipped", "source_ids": ["S0002"]},  # duplicate
        {"quote": "Too short", "source_ids": ["S0001"]},
        {"text": "Buy a good smoker before you buy a second hive.", "source_id": "S0004"},
        "bare string is ignored",
    ]
    out = normalize_quotes(raw)
    assert [q["quote"] for q in out] == [
        "Most colony losses trace back to inspections somebody skipped.",
        "Buy a good smoker before you buy a second hive.",
    ]
    assert out[0]["speaker"] == "Maria" and out[0]["why"] == "the thesis"
    assert out[0]["source_ids"] == ["S0002"] and out[1]["source_ids"] == ["S0004"]


def test_verify_keeps_verbatim_and_drops_invented():
    quotes = normalize_quotes([
        # straddles S0002/S0003 - cited on S0002, matched via the neighbour window
        {"quote": "Most colony losses trace back to inspections that somebody skipped.", "source_ids": ["S0002"]},
        # punctuation/casing added, filler dropped: still verbatim enough
        {"quote": "Buy a good smoker before you buy a second hive.", "source_ids": ["S0004"]},
        # never said anywhere in the transcript
        {"quote": "Beekeeping is ninety percent patience and ten percent sugar water.", "source_ids": ["S0003"]},
        # said, but cited far from where it was said
        {"quote": "Buy a good smoker before you buy a second hive.", "source_ids": ["S0001"]},
        # unknown id only
        {"quote": "Buy a good smoker before you buy a second hive.", "source_ids": ["S9999"]},
    ], max_items=10)
    kept = verify_quotes(quotes, segments())
    assert [q["quote"][:20] for q in kept] == ["Most colony losses t", "Buy a good smoker be"]
    assert kept[1]["source_ids"][0] == "S0004"


def test_verify_rewrites_anchor_to_best_matching_segment():
    quotes = normalize_quotes([{"quote": "so buy a good smoker before you buy a second hive", "source_ids": ["S0003", "S0004"]}])
    kept = verify_quotes(quotes, segments())
    assert kept and kept[0]["source_ids"][0] == "S0004"


def test_cyrillic_quotes_verify():
    segs = [TranscriptSegment(0.0, 5.0, "пчелите имат нужда от редовни прегледи през пролетта")]
    assign_source_ids(segs)
    quotes = normalize_quotes([{"quote": "Пчелите имат нужда от редовни прегледи през пролетта.", "source_ids": ["S0001"]}])
    assert len(verify_quotes(quotes, segs)) == 1


def test_summary_carries_quotes_and_markdown_renders_them():
    segs = segments()
    summary = normalize_video_summary({
        "title": "Hive care",
        "quotes": [{"quote": "Buy a good smoker before you buy a second hive.", "why": "gear priority", "source_ids": ["S0004"]}],
    })
    summary["quotes"] = verify_quotes(summary["quotes"], segs)
    meta = VideoMeta(video_id="aaaaaaaaaaa", title="Video 1", channel="Bee Channel")
    result = VideoResult(meta=meta, index=1, segments=segs, summary=summary)
    resolver = make_link_resolver({s.source_id: (meta.video_id, s.start) for s in segs})
    md = render_video_md(result, resolver)
    assert "## Quotable lines" in md
    assert "> “Buy a good smoker before you buy a second hive.”" in md
    assert "— Bee Channel, [Video 1](https://www.youtube.com/watch?v=aaaaaaaaaaa&t=30s) at 00:30" in md
    assert "> _gear priority_" in md


def test_best_quotes_selected_by_id_only():
    segs = segments()
    meta = VideoMeta(video_id="aaaaaaaaaaa", title="Video 1", channel="Bee Channel")
    result = VideoResult(meta=meta, index=1, segments=segs, summary={
        "quotes": [
            {"quote": "Buy a good smoker before you buy a second hive.", "speaker": "", "why": "gear", "source_ids": ["S0004"]},
            {"quote": "Most colony losses trace back to inspections that somebody skipped.", "speaker": "Maria", "why": "thesis", "source_ids": ["S0002"]},
        ]
    })
    pool, lookup = build_quote_pool([result])
    assert [p["id"] for p in pool] == ["V01.Q1", "V01.Q2"]
    report = normalize_report({
        "best_quotes": [
            {"id": "v01.q2", "why": "lands for the topic"},
            {"id": "V01.Q2"},            # repeat - dropped
            {"id": "V07.Q1", "why": "x"},  # invented - dropped
            {"id": "V01.Q9"},             # out of range - dropped
            "V01.Q1",                     # bare id string accepted
        ]
    }, None, lookup)
    best = report["best_quotes"]
    assert [q["id"] for q in best] == ["V01.Q2", "V01.Q1"]
    assert best[0]["why"] == "lands for the topic" and best[0]["speaker"] == "Maria"
    assert best[0]["source_ids"] == ["V01.S0002"]
    assert best[1]["why"] == "gear"  # falls back to the per-video why
