from yttool.gemini_transcriber import parse_timestamped_lines


def test_parses_hms_and_ms_lines():
    text = """[00:00:02] hello and welcome to the show
[00:00:19] today we talk about cars
[01:23] short format line
garbage line without timestamp
[00:02:05] final segment"""
    segments = parse_timestamped_lines(text)
    assert len(segments) == 4
    assert segments[0].start == 2.0
    assert segments[0].end == 19.0  # end = next start
    assert segments[2].start == 83.0
    assert segments[-1].end is None
    assert segments[1].text == "today we talk about cars"


def test_tolerates_dash_and_no_brackets():
    text = "00:00:05 - first\n[00:00:10]: second"
    segments = parse_timestamped_lines(text)
    assert [s.start for s in segments] == [5.0, 10.0]


def test_empty_input():
    assert parse_timestamped_lines("no timestamps here at all") == []


def test_quota_error_is_a_gemini_unavailable():
    from yttool.gemini_transcriber import GeminiQuotaExhausted, GeminiUnavailable

    assert issubclass(GeminiQuotaExhausted, GeminiUnavailable)
