"""Global pacing and rate-limit circuit breaker for all YouTube-touching calls.

YouTube throttles per endpoint per IP over a rolling window, and per-invocation
sleep flags cannot pace across the separate subprocesses this tool spawns. The
gate serializes every network hit process-wide with a minimum gap; the throttle
state turns repeated block signals into a circuit breaker so a rate-limited run
stops feeding the limiter instead of amplifying requests through fallbacks.
"""

from __future__ import annotations

import random
import threading
import time

MIN_GAP_SECONDS = 4.0
GAP_JITTER_SECONDS = 1.5
BREAKER_THRESHOLD = 3  # consecutive blocked videos before the breaker trips

_gate_lock = threading.Lock()
_next_allowed = 0.0


def youtube_gate() -> None:
    """Block until the process-wide minimum gap since the last YouTube call has
    passed. Sleeping under the lock intentionally queues concurrent callers."""
    global _next_allowed
    with _gate_lock:
        now = time.monotonic()
        if now < _next_allowed:
            time.sleep(_next_allowed - now)
        _next_allowed = time.monotonic() + MIN_GAP_SECONDS + random.uniform(0, GAP_JITTER_SECONDS)


class ThrottleState:
    def __init__(self, threshold: int = BREAKER_THRESHOLD):
        self._lock = threading.Lock()
        self.threshold = threshold
        self.consecutive_blocks = 0
        self.api_blocked = False  # transcript API returned IpBlocked this session
        self.tripped = False

    def record_block(self) -> None:
        with self._lock:
            self.consecutive_blocks += 1
            if self.consecutive_blocks >= self.threshold:
                self.tripped = True

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_blocks = 0

    def reset(self) -> None:
        with self._lock:
            self.consecutive_blocks = 0
            self.api_blocked = False
            self.tripped = False


THROTTLE = ThrottleState()


def looks_rate_limited(stderr: str) -> bool:
    """Recognize IP-throttling in yt-dlp stderr, as opposed to video-specific
    failures (DRM, members-only, no captions) where client rotation can help."""
    text = stderr or ""
    return "429" in text or "Too Many Requests" in text or "HTTP Error 403" in text
