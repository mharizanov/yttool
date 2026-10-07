"""Transcript chunking and source-ID assignment, vendored from an earlier transcription tool."""

from __future__ import annotations

import re

from .models import TranscriptSegment


def fmt_ts(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total_ms = max(0, int(round(float(seconds) * 1000)))
    total_s, _ms = divmod(total_ms, 1000)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_mmss(seconds: float | None) -> str:
    if seconds is None:
        return "00:00"
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_ts(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    match = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def assign_source_ids(segments: list[TranscriptSegment]) -> dict[str, TranscriptSegment]:
    source_index: dict[str, TranscriptSegment] = {}
    for index, segment in enumerate(segments, 1):
        segment.source_id = f"S{index:04d}"
        source_index[segment.source_id] = segment
    return source_index


def segment_to_line(segment: TranscriptSegment, include_timestamps: bool = True) -> str:
    text = clean_text(segment.text)
    if segment.speaker:
        text = f"{segment.speaker}: {text}"
    if include_timestamps and segment.start is not None:
        return f"[{segment.source_id} {fmt_ts(segment.start)}] {text}"
    return f"[{segment.source_id}] {text}"


def split_long_text(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_at = remaining.rfind(". ", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = remaining.rfind(" ", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = max_chars
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def chunk_segments(segments: list[TranscriptSegment], max_chars: int, include_timestamps: bool = True) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for segment in segments:
        line = segment_to_line(segment, include_timestamps)
        if not line:
            continue
        if current and current_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if len(line) > max_chars:
            for part in split_long_text(line, max_chars):
                if current:
                    chunks.append("\n".join(current))
                    current = []
                    current_len = 0
                chunks.append(part)
            continue
        current.append(line)
        current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks
