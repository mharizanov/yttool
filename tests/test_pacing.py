import time

from yttool import pacing
from yttool.pacing import ThrottleState, looks_rate_limited


def test_throttle_trips_after_threshold():
    t = ThrottleState(threshold=3)
    t.record_block()
    t.record_block()
    assert not t.tripped
    t.record_block()
    assert t.tripped


def test_success_resets_streak():
    t = ThrottleState(threshold=3)
    t.record_block()
    t.record_block()
    t.record_success()
    t.record_block()
    t.record_block()
    assert not t.tripped


def test_looks_rate_limited():
    assert looks_rate_limited("ERROR: HTTP Error 429: Too Many Requests")
    assert looks_rate_limited("unable to download video data: HTTP Error 403: Forbidden")
    assert not looks_rate_limited("ERROR: This video is DRM protected")
    assert not looks_rate_limited("no subtitles available")
    assert not looks_rate_limited("")


def test_gate_enforces_minimum_gap(monkeypatch):
    monkeypatch.setattr(pacing, "MIN_GAP_SECONDS", 0.2)
    monkeypatch.setattr(pacing, "GAP_JITTER_SECONDS", 0.0)
    pacing._next_allowed = 0.0
    start = time.monotonic()
    pacing.youtube_gate()
    pacing.youtube_gate()
    pacing.youtube_gate()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4  # 2 enforced gaps between 3 calls
