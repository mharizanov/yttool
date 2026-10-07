from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class TranscriptSegment:
    start: float | None
    end: float | None
    text: str
    source_id: str = ""
    speaker: str = ""  # set only by the diarizing Gemini path


class VideoStatus(str, Enum):
    OK = "ok"
    OK_WHISPER = "ok_whisper"
    SKIPPED_NO_TRANSCRIPT = "skipped_no_transcript"
    SKIPPED_NO_WHISPER = "skipped_no_whisper"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_SUMMARIZE = "failed_summarize"
    BLOCKED = "blocked"
    DEFERRED = "deferred"  # skipped by the rate-limit circuit breaker; retry later


@dataclass
class VideoMeta:
    video_id: str
    title: str
    channel: str = ""
    duration: float | None = None
    view_count: int | None = None
    upload_date: str = ""  # YYYYMMDD when known (full metadata fetches only)
    found_by: str = ""  # which planned search query surfaced this video
    why: str = ""  # LLM's one-line reason for selecting it

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel": self.channel,
            "duration": self.duration,
            "view_count": self.view_count,
            "upload_date": self.upload_date,
            "found_by": self.found_by,
            "why": self.why,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VideoMeta:
        return cls(
            video_id=str(data.get("video_id") or ""),
            title=str(data.get("title") or ""),
            channel=str(data.get("channel") or ""),
            duration=data.get("duration"),
            view_count=data.get("view_count"),
            upload_date=str(data.get("upload_date") or ""),
            found_by=str(data.get("found_by") or ""),
            why=str(data.get("why") or ""),
        )


@dataclass
class VideoResult:
    meta: VideoMeta
    index: int  # 1-based position in the run
    status: VideoStatus | None = None
    transcript_source: str | None = None  # api | vtt | vtt-auto | whisper
    segments: list[TranscriptSegment] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    comments: list[dict[str, Any]] = field(default_factory=list)
    comment_insights: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def has_transcript(self) -> bool:
        return bool(self.segments)

    @property
    def summarized(self) -> bool:
        return self.summary is not None
