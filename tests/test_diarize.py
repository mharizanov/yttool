from yttool.chunking import assign_source_ids, segment_to_line
from yttool.gemini_transcriber import parse_timestamped_lines
from yttool.models import TranscriptSegment, VideoMeta, VideoResult
from yttool.report import render_transcript_md


def test_parse_diarized_lines():
    text = """[00:00:02] Мария: Здравейте и добре дошли.
[00:00:10] Петър: Инженер, това е позицията.
[00:00:19] Speaker 3: Кажи ни повече.
[00:00:25] line without a speaker prefix continues here"""
    segments = parse_timestamped_lines(text, diarize=True)
    assert [s.speaker for s in segments] == ["Мария", "Петър", "Speaker 3", ""]
    assert segments[1].text == "Инженер, това е позицията."
    assert segments[0].end == 10.0


def test_parse_non_diarized_keeps_colons():
    text = "[00:00:02] Note: this colon stays in the text"
    segments = parse_timestamped_lines(text, diarize=False)
    assert segments[0].speaker == ""
    assert segments[0].text.startswith("Note:")


def test_segment_to_line_includes_speaker():
    segments = [TranscriptSegment(83.0, 90.0, "important point", speaker="Ivan")]
    assign_source_ids(segments)
    assert segment_to_line(segments[0]) == "[S0001 00:01:23] Ivan: important point"


def test_transcript_md_includes_speaker():
    result = VideoResult(
        meta=VideoMeta(video_id="abc", title="Talk"),
        index=1,
        segments=[TranscriptSegment(5.0, 9.0, "hello", speaker="Anna")],
        transcript_source="gemini",
    )
    assert "[00:05] Anna: hello" in render_transcript_md(result)
