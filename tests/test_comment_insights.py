from yttool.comment_insights import (
    comment_link,
    comments_content_hash,
    normalize_insights,
)
from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.report import render_video_md
from yttool.synthesize import make_link_resolver

COMMENTS = [
    {"id": "c1", "parent": "root", "author": "@doc", "text": "I'm a beekeeper, this misses X", "like_count": 500, "is_uploader": False, "is_pinned": False},
    {"id": "c2", "parent": "c1", "author": "@creator", "text": "good point, clarified", "like_count": 12, "is_uploader": True, "is_pinned": False},
]


def test_normalize_insights_fills_from_captured_data():
    data = {
        "insights": [
            {"comment_id": "c1", "type": "counterpoint", "insight": "A beekeeper disputes X."},
            {"comment_id": "c2", "type": "clarification", "insight": "Creator concedes the point.", "like_count": 99999},
            {"comment_id": "invented", "type": "tip", "insight": "should be dropped"},
            {"comment_id": "c1", "type": "WEIRD_TYPE", "insight": "unknown type coerced"},
        ]
    }
    out = normalize_insights(data, {c["id"]: c for c in COMMENTS})
    assert len(out) == 3
    assert out[0]["like_count"] == 500  # from captured data, not the LLM
    assert out[1]["like_count"] == 12 and out[1]["is_uploader"]
    assert out[2]["type"] == "tip"  # coerced


def test_comment_link():
    assert comment_link("vid123", "c1") == "https://www.youtube.com/watch?v=vid123&lc=c1"


def test_hash_changes_with_likes():
    changed = [dict(COMMENTS[0], like_count=501), COMMENTS[1]]
    assert comments_content_hash(COMMENTS) != comments_content_hash(changed)


def test_normalize_report_community_signal_validates_refs():
    from yttool.synthesize import normalize_report

    lookup = {
        (1, "c1"): {"comment_id": "c1", "video_id": "vidA", "author": "@doc", "like_count": 500},
        (2, "c9"): {"comment_id": "c9", "video_id": "vidB", "author": "@x", "like_count": 3},
    }
    data = {
        "community_signal": [
            {"observation": "Viewers across videos report swarming.", "refs": [
                {"video_index": 1, "comment_id": "c1"},
                {"video_index": 2, "comment_id": "c9"},
                {"video_index": 3, "comment_id": "invented"},
            ]},
            {"observation": "Only invented refs.", "refs": [{"video_index": 9, "comment_id": "nope"}]},
        ]
    }
    out = normalize_report(data, lookup)
    assert len(out["community_signal"]) == 1
    refs = out["community_signal"][0]["refs"]
    assert [r["video_id"] for r in refs] == ["vidA", "vidB"]
    assert refs[0]["like_count"] == 500


def test_render_video_md_includes_community_insights():
    result = VideoResult(
        meta=VideoMeta(video_id="vid123", title="Talk"),
        index=1,
        segments=[TranscriptSegment(0.0, 5.0, "text", source_id="S0001")],
        summary={"title": "Talk", "tl_dr": "x", "sections": [], "claims": [], "themes": [], "watch_verdict": {"score_1_5": 3, "reason": ""}, "nodes": []},
        comment_insights=[
            {"comment_id": "c1", "type": "counterpoint", "insight": "A beekeeper disputes X.", "author": "@doc", "like_count": 500, "is_uploader": False},
            {"comment_id": "c2", "type": "clarification", "insight": "Creator concedes.", "author": "@creator", "like_count": 12, "is_uploader": True},
        ],
    )
    md = render_video_md(result, make_link_resolver({}))
    assert "## Community insights" in md
    assert "**(counterpoint, 👍 500)** A beekeeper disputes X. — [@doc](https://www.youtube.com/watch?v=vid123&lc=c1)" in md
    assert "@creator · uploader" in md
