from yttool.gemini_transcriber import sanitize_timestamps
from yttool.models import TranscriptSegment


def seg(start):
    return TranscriptSegment(float(start), None, f"text at {start}")


def test_plausible_timestamps_untouched():
    segments = [seg(5), seg(300), seg(1500)]
    out = sanitize_timestamps(segments, duration=1585)
    assert [s.start for s in out] == [5.0, 300.0, 1500.0]


def test_centisecond_misparse_repaired():
    # [MM:SS:cc] misparse inflates everything ~60x: [00:07:07]=7.07s parsed as
    # 427s. Realistic shape: nearly all stamps dwarf the 1585s duration.
    segments = [seg(427), seg(937), seg(1407), seg(2167), seg(20047), seg(40047), seg(60047), seg(80047)]
    out = sanitize_timestamps(segments, duration=1585)
    assert len(out) == 8
    assert out[0].start == 7.07
    assert out[1].start == 15.37
    assert out[4].start == 334.07  # 20047 -> 5h34m07s -> 5*60+34+7/100
    assert out[-1].start == 1334.07
    assert out[0].end == out[1].start


def test_hallucinated_tail_dropped():
    # real-format stamps but continuing past the end of a 600s video
    segments = [seg(10), seg(200), seg(400), seg(550), seg(1400), seg(2000)]
    out = sanitize_timestamps(segments, duration=600)
    assert [s.start for s in out] == [10.0, 200.0, 400.0, 550.0]
    assert out[-1].end == 600


def test_rejects_when_too_little_survives():
    segments = [seg(50)] + [seg(100000 + i) for i in range(20)]
    assert sanitize_timestamps(segments, duration=600) == []


def test_no_duration_passthrough():
    segments = [seg(999999)]
    assert sanitize_timestamps(segments, duration=None) == segments
