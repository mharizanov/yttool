from pathlib import Path

from yttool.transcripts import coalesce_cues, parse_vtt

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_vtt_dedupes_rolling_lines():
    cues = parse_vtt((FIXTURES / "sample_auto.vtt").read_text())
    texts = [text for _, _, text in cues]
    joined = " ".join(texts)
    assert joined.count("so today we're going to talk about") == 1
    assert "urban beekeeping basics" in joined
    assert "after a long pause we continue here" in joined


def test_parse_vtt_strips_inline_tags():
    cues = parse_vtt((FIXTURES / "sample_auto.vtt").read_text())
    for _, _, text in cues:
        assert "<" not in text and ">" not in text


def test_parse_vtt_timestamps():
    cues = parse_vtt((FIXTURES / "sample_auto.vtt").read_text())
    assert cues[0][0] == 0.24


def test_coalesce_breaks_on_gap():
    cues = [
        (0.0, 2.0, "first part"),
        (2.5, 4.0, "second part"),
        (10.0, 12.0, "after the gap"),
    ]
    segments = coalesce_cues(cues, max_chars=250, max_gap=2.5)
    assert len(segments) == 2
    assert segments[0].text == "first part second part"
    assert segments[1].start == 10.0


def test_coalesce_breaks_on_size():
    cues = [(float(i), float(i) + 1, "word " * 20) for i in range(10)]
    segments = coalesce_cues(cues, max_chars=250, max_gap=5.0)
    assert all(len(s.text) <= 260 for s in segments)
    assert len(segments) > 1
