"""End-to-end pipeline test with the LLM stubbed out: transcript segments in,
per-video markdown + report + mindmap HTML out, deep links resolved by code."""

import json

import pytest

import yttool.summarize as summarize_mod
import yttool.synthesize as synthesize_mod
from yttool.cache import Cache
from yttool.llm import LlmConfig
from yttool.mindmap import inject_deep_links, mindmap_to_markdown, write_html
from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.prompts_io import load_prompts
from yttool.report import render_report_md, render_video_md
from yttool.summarize import summarize_video
from yttool.synthesize import build_resolution_table, make_link_resolver, synthesize

CHUNK_RESPONSE = {
    "title": "Beekeeping basics",
    "tl_dr": "Healthy hives need steady inspection routines.",
    "sections": [
        {"heading": "Core ideas", "bullets": [{"text": "Inspections catch problems early", "source_ids": ["S0002"]}]}
    ],
    "claims": [{"claim": "Most colony losses trace to missed inspections", "stance": "opinion", "source_ids": ["S0003"]}],
    "themes": ["beekeeping"],
    "quotes": [
        {"quote": "Most losses come from missed checks.", "speaker": "", "why": "the thesis", "source_ids": ["S0003"]},
        {"quote": "Bees are basically tiny accountants.", "speaker": "", "why": "never said", "source_ids": ["S0002"]},
    ],
    "watch_verdict": {"score_1_5": 4, "reason": "clear intro"},
    "nodes": [{"label": "Hive care", "source_ids": ["S0002"], "children": [{"label": "Inspection routine", "source_ids": ["S0003"], "children": []}]}],
}

SYNTHESIS_RESPONSE = {
    "overview": "Both videos agree inspections matter.",
    "common_themes": [{"theme": "Inspection routine", "explanation": "Check hives on a schedule", "source_ids": ["V01.S0002", "V02.S0002"]}],
    "disagreements": [],
    "watch_recommendations": [
        {"video_index": 2, "rank": 1, "verdict": "watch first", "why": "deeper"},
        {"video_index": 1, "rank": 2, "verdict": "good intro", "why": "shorter"},
    ],
    "best_quotes": [{"id": "V02.Q1", "why": "lands"}, {"id": "V02.Q7", "why": "invented"}],
}

CROSSMAP_RESPONSE = {
    "title": "Urban beekeeping",
    "summary": "What the videos say",
    "nodes": [{"label": "Hive care", "source_ids": ["V02.S0002"], "children": []}],
}


def fake_call_llm_json(client, config, system_prompt, user_prompt, label, **kwargs):
    if "synthesis" in label:
        return json.loads(json.dumps(SYNTHESIS_RESPONSE))
    if "crossmap" in label:
        return json.loads(json.dumps(CROSSMAP_RESPONSE))
    return json.loads(json.dumps(CHUNK_RESPONSE))


@pytest.fixture
def stub_llm(monkeypatch):
    monkeypatch.setattr(summarize_mod, "call_llm_json", fake_call_llm_json)
    monkeypatch.setattr(synthesize_mod, "call_llm_json", fake_call_llm_json)


def make_result(index, video_id):
    segments = [
        TranscriptSegment(0.0, 5.0, "welcome to the workshop"),
        TranscriptSegment(10.0, 20.0, "a hive inspection follows a routine"),
        TranscriptSegment(30.5, 40.0, "most losses come from missed checks"),
    ]
    meta = VideoMeta(video_id=video_id, title=f"Video {index}", channel="chan", duration=300)
    return VideoResult(meta=meta, index=index, segments=segments)


def test_full_pipeline(tmp_path, stub_llm):
    cache = Cache(tmp_path / "cache")
    prompts, prompts_hash = load_prompts()
    cfg = LlmConfig(provider="test", model="test-model", api_key="x")
    results = [make_result(1, "aaaaaaaaaaa"), make_result(2, "bbbbbbbbbbb")]

    for result in results:
        result.summary = summarize_video(None, cfg, result, prompts, prompts_hash, cache)
        assert result.summary["title"] == "Beekeeping basics"
        # the invented quote is dropped by the transcript check, the verbatim one kept
        assert [q["quote"] for q in result.summary["quotes"]] == ["Most losses come from missed checks."]

    # summary caching: a second call with same inputs must not invoke the LLM
    def boom(*a, **k):
        raise AssertionError("LLM called despite cache")

    import yttool.summarize as sm
    orig = sm.call_llm_json
    sm.call_llm_json = boom
    try:
        cached = summarize_video(None, cfg, results[0], prompts, prompts_hash, cache)
        assert cached == results[0].summary
    finally:
        sm.call_llm_json = orig

    report, mindmap = synthesize(None, cfg, results, "test query", prompts)
    assert report["watch_recommendations"][0]["video_index"] == 2

    table = build_resolution_table(results)
    resolver = make_link_resolver(table)

    # per-video markdown with local deep links
    local_table = {s.source_id: (results[0].meta.video_id, s.start) for s in results[0].segments}
    md = render_video_md(results[0], make_link_resolver(local_table))
    assert "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=10s" in md

    # cross-video report resolves V-prefixed ids
    from yttool.models import VideoStatus
    for r in results:
        r.status = VideoStatus.OK
        r.transcript_source = "api"
    report_md = render_report_md(report, results, resolver, "test query", synthesized=True)
    assert "watch?v=aaaaaaaaaaa&t=10s" in report_md
    assert "## What to actually watch" in report_md
    assert "## Lines worth quoting" in report_md
    assert report["best_quotes"][0]["id"] == "V02.Q1" and len(report["best_quotes"]) == 1
    assert "> \u201cMost losses come from missed checks.\u201d" in report_md
    assert "[Video 2](https://www.youtube.com/watch?v=bbbbbbbbbbb&t=30s) at 00:30" in report_md
    assert "## Quotable lines" in md

    # mindmap deep links + html
    inject_deep_links(mindmap, resolver)
    markdown = mindmap_to_markdown(mindmap)
    assert "[00:10](https://www.youtube.com/watch?v=bbbbbbbbbbb&t=10s)" in markdown
    html_path = tmp_path / "mindmap.html"
    write_html(markdown, "t", html_path)
    assert "markmap" in html_path.read_text()
