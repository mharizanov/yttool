from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.report import render_transcript_md


def test_render_transcript_md():
    result = VideoResult(
        meta=VideoMeta(video_id="abc123", title="Talk", channel="Chan"),
        index=1,
        segments=[
            TranscriptSegment(0.0, 5.0, "hello world"),
            TranscriptSegment(83.4, 90.0, "second segment"),
            TranscriptSegment(None, None, "no timestamp"),
        ],
        transcript_source="api",
    )
    md = render_transcript_md(result)
    assert "# Transcript: Talk" in md
    assert "[00:00] hello world" in md
    assert "[01:23] second segment" in md
    assert "no timestamp" in md
    assert "**Source:** api" in md
    # exactly one URL (the header) - per-segment links are redundant context
    assert md.count("https://www.youtube.com/watch") == 1
    assert "&t=<seconds>s" in md
