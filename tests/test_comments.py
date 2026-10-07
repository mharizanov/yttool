from yttool.comments import _normalize_comment, render_comments_md
from yttool.models import VideoMeta, VideoResult


def test_normalize_comment():
    raw = {
        "id": "c1",
        "parent": "root",
        "author": "@someone",
        "text": "great video",
        "like_count": 5,
        "_time_text": "2 months ago",
        "author_is_uploader": False,
        "is_pinned": True,
    }
    c = _normalize_comment(raw)
    assert c["id"] == "c1" and c["parent"] == "root" and c["is_pinned"]


def test_render_comments_md_threads_replies():
    result = VideoResult(meta=VideoMeta(video_id="abc", title="Talk"), index=1)
    comments = [
        {"id": "c1", "parent": "root", "author": "@alice", "text": "top comment", "like_count": 10, "time_text": "1 year ago", "is_uploader": False, "is_pinned": False},
        {"id": "c2", "parent": "c1", "author": "@bob", "text": "a reply", "like_count": 0, "time_text": "", "is_uploader": True, "is_pinned": False},
        {"id": "c3", "parent": "root", "author": "@carol", "text": "another\nmultiline", "like_count": 0, "time_text": "", "is_uploader": False, "is_pinned": False},
    ]
    md = render_comments_md(result, comments)
    assert "# Comments: Talk" in md
    assert "**@alice** · 👍 10 · 1 year ago" in md
    # reply nested under its parent with uploader badge
    assert "  - **@bob** (uploader)" in md
    assert md.index("top comment") < md.index("a reply") < md.index("another")
    assert "> another\n    > multiline".replace("    ", "  ") in md.replace("    ", "  ")


def test_render_comments_md_empty():
    result = VideoResult(meta=VideoMeta(video_id="abc", title="Talk"), index=1)
    assert "_No comments captured._" in render_comments_md(result, [])
