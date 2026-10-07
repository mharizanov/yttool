"""Transcript acquisition: youtube-transcript-api primary, yt-dlp VTT fallback.

The Whisper ASR last-resort lives in whisper_fallback.py; the orchestrator in
cli.py chains the three tiers and maps failures onto VideoStatus values.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .cache import Cache
from .chunking import clean_text, parse_ts
from .llm import log
from .models import TranscriptSegment, VideoMeta
from .pacing import THROTTLE, looks_rate_limited, youtube_gate

MISS_TTL_SECONDS = 6 * 3600
COALESCE_MAX_CHARS = 250
COALESCE_MAX_GAP = 2.5


class NoCaptions(RuntimeError):
    """Neither the transcript API nor yt-dlp subtitles produced captions."""


class TranscriptBlocked(RuntimeError):
    """YouTube refused the request (IP block / bot detection)."""


def _transcript_cache_key(video_id: str, lang: str, diarized: bool = False) -> str:
    suffix = ".diarized" if diarized else ""
    return f"transcripts/{video_id}.{lang}{suffix}.json"


def _miss_cache_key(video_id: str, lang: str) -> str:
    return f"transcripts/{video_id}.{lang}.miss.json"


# --- tier 1: youtube-transcript-api -----------------------------------------


def _classify_api_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ("IpBlocked", "RequestBlocked"):
        return "blocked"
    if name in ("NoTranscriptFound", "TranscriptsDisabled", "NoTranscriptAvailable"):
        return "no_captions"
    if name in ("VideoUnavailable", "InvalidVideoId", "AgeRestricted", "VideoUnplayable"):
        return "unavailable"
    return "error"


def _fetch_any_language(api, video_id: str):
    """Fall back to whatever caption track exists (manual preferred)."""
    try:
        transcripts = list(api.list(video_id))
    except Exception:
        return None
    if not transcripts:
        return None
    transcripts.sort(key=lambda t: t.is_generated)  # manually-created first
    track = transcripts[0]
    suffix = " (auto-generated)" if track.is_generated else ""
    log(f"{video_id}: no captions in preferred language, falling back to '{track.language_code}'{suffix}")
    try:
        return track.fetch()
    except Exception:
        return None


def fetch_via_api(video_id: str, lang: str) -> list[TranscriptSegment]:
    from youtube_transcript_api import YouTubeTranscriptApi

    if THROTTLE.api_blocked:
        # The API already returned IpBlocked this session; hitting it again for
        # every video only feeds the rate limiter.
        raise TranscriptBlocked("transcript API blocked earlier this session; skipping this tier")

    youtube_gate()
    api = YouTubeTranscriptApi()
    languages = [lang, "en"] if lang != "en" else ["en"]
    try:
        fetched = api.fetch(video_id, languages=languages)
    except Exception as exc:
        kind = _classify_api_error(exc)
        if kind == "blocked":
            THROTTLE.api_blocked = True
            raise TranscriptBlocked(
                "YouTube is blocking transcript requests from this IP. "
                "This usually means a cloud/VPN IP - run yttool from a residential connection or disable your VPN."
            ) from exc
        if kind == "no_captions":
            fetched = _fetch_any_language(api, video_id)
            if fetched is None:
                raise NoCaptions(str(exc)) from exc
        elif kind == "unavailable":
            raise NoCaptions(str(exc)) from exc
        else:
            raise
    entries = [(s.start, s.duration, s.text) for s in fetched]
    return coalesce_entries(entries)


# --- tier 2: yt-dlp subtitles ------------------------------------------------


def parse_vtt(content: str) -> list[tuple[float, float, str]]:
    """Parse WebVTT into (start, end, text) cues, deduplicating the rolling
    repeated lines that YouTube auto-captions produce."""
    time_re = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
    )
    tag_re = re.compile(r"<[^>]+>")
    cues: list[tuple[float, float, str]] = []
    last_lines: list[str] = []

    lines = content.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        match = time_re.search(lines[i])
        if not match:
            i += 1
            continue
        start = parse_ts(match.group(1))
        end = parse_ts(match.group(2))
        i += 1
        text_lines: list[str] = []
        # Cue boundaries are timing lines; YouTube auto-VTT cues contain
        # whitespace-only filler lines that must not terminate the cue.
        while i < len(lines) and not time_re.search(lines[i]):
            raw = lines[i].strip()
            i += 1
            if not raw or raw.isdigit():
                continue
            text = clean_text(tag_re.sub("", raw))
            if text:
                text_lines.append(text)
        # Rolling captions repeat the previous cue's line(s); keep only new lines.
        new_lines = [line for line in text_lines if line not in last_lines]
        if text_lines:
            last_lines = text_lines
        text = clean_text(" ".join(new_lines))
        if text and start is not None:
            cues.append((start, end if end is not None else start, text))
    return cues


def fetch_via_ytdlp(
    video_id: str,
    lang: str,
    player_clients: list[str],
    cache: Cache | None = None,
    comments_limit: int = 0,
) -> tuple[list[TranscriptSegment], bool]:
    """Returns (segments, is_auto). Tries player clients in order.

    Besides the preferred language, always request the original-language
    auto-caption track (".*-orig") so non-English videos still yield a
    transcript when the preferred language has no captions.

    Exact language codes only - a wildcard like "en.*" also matches YouTube's
    auto-TRANSLATED tracks, and requesting those is the confirmed trigger for
    hard 429 rate-limiting on the timedtext endpoint (yt-dlp issue #13831)."""
    sub_langs = f"{lang},{lang}-orig,.*-orig"
    last_error = ""
    tried: list[str] = []
    for client in player_clients:
        tried.append(client)
        with tempfile.TemporaryDirectory(prefix="yttool-subs-") as tmpdir:
            youtube_gate()
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                f"https://www.youtube.com/watch?v={video_id}",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                sub_langs,
                "--sub-format",
                "vtt",
                "--sleep-requests",
                "2",
                "--sleep-subtitles",
                "8",
                "--no-warnings",
                "--extractor-args",
                f"youtube:player_client={client}" + _comments_extractor_args(video_id, cache, comments_limit),
                "-o",
                str(Path(tmpdir) / "%(id)s.%(ext)s"),
            ]
            if _comments_extractor_args(video_id, cache, comments_limit):
                cmd += ["--write-info-json", "--write-comments"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            _capture_combined_comments(video_id, cache, Path(tmpdir), comments_limit)
            vtt_files = sorted(Path(tmpdir).glob("*.vtt"))
            if not vtt_files:
                last_error = "\n".join(proc.stderr.strip().splitlines()[-2:])
                if looks_rate_limited(proc.stderr):
                    # Client rotation cannot fix IP throttling; stop amplifying.
                    raise TranscriptBlocked(
                        f"yt-dlp subtitle fetch rate-limited (client {client}): {last_error}"
                    )
                continue
            # Prefer a manually-created track (no "auto" marker isn't visible in the
            # filename, so prefer exact lang match, e.g. id.en.vtt over id.en-orig.vtt).
            vtt_files.sort(key=lambda p: (len(p.name), p.name))
            content = vtt_files[0].read_text(encoding="utf-8", errors="replace")
            cues = parse_vtt(content)
            if cues:
                is_auto = "-orig" in vtt_files[0].name or len(vtt_files) == 1
                return coalesce_cues(cues), is_auto
    raise NoCaptions(f"yt-dlp produced no subtitles (clients tried: {', '.join(tried)}). {last_error}")


def _comments_extractor_args(video_id: str, cache: Cache | None, comments_limit: int) -> str:
    """Piggyback comment extraction on the subtitle fetch (one yt-dlp invocation
    instead of two) unless comments are already cached."""
    if not comments_limit or cache is None:
        return ""
    cached = cache.get(f"comments/{video_id}.json")
    if cached is not None and cached.get("limit", 0) >= comments_limit:
        return ""
    return f";comment_sort=top;max_comments={comments_limit}"


def _capture_combined_comments(video_id: str, cache: Cache | None, tmpdir: Path, comments_limit: int) -> None:
    if cache is None or not comments_limit:
        return
    from .comments import _normalize_comment

    for info_path in tmpdir.glob("*.info.json"):
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        raw = data.get("comments")
        if not isinstance(raw, list):
            continue
        comments = [c for c in (_normalize_comment(r) for r in raw if isinstance(r, dict)) if c["text"]]
        if comments:
            cache.put(f"comments/{video_id}.json", {"limit": comments_limit, "comments": comments})
            log(f"{video_id}: captured {len(comments)} comments alongside subtitles")
        return


# --- segment coalescing -------------------------------------------------------


def coalesce_entries(entries: list[tuple[float, float, str]]) -> list[TranscriptSegment]:
    """Coalesce (start, duration, text) caption entries into ~250-char segments."""
    cues = [(start, start + (duration or 0.0), text) for start, duration, text in entries]
    return coalesce_cues(cues)


def coalesce_cues(
    cues: list[tuple[float, float, str]],
    max_chars: int = COALESCE_MAX_CHARS,
    max_gap: float = COALESCE_MAX_GAP,
) -> list[TranscriptSegment]:
    """Merge consecutive (start, end, text) cues into segments of ~max_chars,
    breaking on gaps > max_gap seconds. Fewer, better timestamp anchors and
    lower token overhead than raw auto-caption cues."""
    segments: list[TranscriptSegment] = []
    cur_start: float | None = None
    cur_end: float | None = None
    cur_text: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_text, cur_len
        text = clean_text(" ".join(cur_text))
        if text:
            segments.append(TranscriptSegment(cur_start, cur_end, text))
        cur_start, cur_end, cur_text, cur_len = None, None, [], 0

    for start, end, text in cues:
        text = clean_text(text)
        if not text:
            continue
        gap_break = cur_end is not None and start is not None and (start - cur_end) > max_gap
        size_break = cur_len + len(text) + 1 > max_chars
        if cur_text and (gap_break or size_break):
            flush()
        if cur_start is None:
            cur_start = start
        cur_end = end if end is not None else cur_end
        cur_text.append(text)
        cur_len += len(text) + 1
    flush()
    return segments


# --- orchestration entry point ------------------------------------------------


def acquire_transcript(
    meta: VideoMeta,
    lang: str,
    cache: Cache,
    player_clients: list[str],
    force_refresh: bool = False,
    comments_limit: int = 0,
) -> tuple[list[TranscriptSegment], str]:
    """Fetch a transcript through tiers 1-2 with caching.

    Returns (segments, source) where source is "api", "vtt" or "vtt-auto".
    Raises NoCaptions if both tiers fail softly (caller may try Whisper),
    TranscriptBlocked info is logged but the yt-dlp tier is still attempted.
    """
    cache_key = _transcript_cache_key(meta.video_id, lang)
    miss_key = _miss_cache_key(meta.video_id, lang)

    if not force_refresh:
        cached = cache.get(cache_key)
        if cached and cached.get("segments"):
            return _segments_from_cache(cached), str(cached.get("source") or "api")
        miss = cache.get(miss_key, ttl_seconds=MISS_TTL_SECONDS)
        if miss:
            raise NoCaptions(f"cached miss: {miss.get('reason', 'no transcript')}")

    blocked_msg: str | None = None
    try:
        segments = fetch_via_api(meta.video_id, lang)
        if segments:
            _store(cache, cache_key, segments, "api")
            return segments, "api"
    except TranscriptBlocked as exc:
        blocked_msg = str(exc)
        log(f"{meta.video_id}: transcript API blocked, trying yt-dlp subtitles. {exc}")
    except NoCaptions:
        log(f"{meta.video_id}: no captions via transcript API, trying yt-dlp subtitles")
    except Exception as exc:
        log(f"{meta.video_id}: transcript API error ({type(exc).__name__}: {exc}), trying yt-dlp subtitles")

    try:
        segments, is_auto = fetch_via_ytdlp(
            meta.video_id, lang, player_clients, cache=cache, comments_limit=comments_limit
        )
        source = "vtt-auto" if is_auto else "vtt"
        _store(cache, cache_key, segments, source)
        return segments, source
    except TranscriptBlocked as exc:
        # Transient IP throttling: never cache as a miss - a later retry
        # after the cooldown must actually retry.
        reason = str(exc) if not blocked_msg else f"{blocked_msg} | {exc}"
        raise TranscriptBlocked(reason) from exc
    except NoCaptions as exc:
        reason = str(exc)
        if blocked_msg:
            # The API tier was blocked, so "no captions" is unreliable evidence.
            raise TranscriptBlocked(f"{blocked_msg} | {reason}") from exc
        cache.put(miss_key, {"reason": reason})
        raise


def _segments_from_cache(cached: dict) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            s.get("start"), s.get("end"), s.get("text") or "", speaker=str(s.get("speaker") or "")
        )
        for s in cached["segments"]
    ]


def _store(cache: Cache, cache_key: str, segments: list[TranscriptSegment], source: str) -> None:
    cache.put(
        cache_key,
        {
            "source": source,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in segments
            ],
        },
    )


def store_asr_transcript(
    cache: Cache, video_id: str, lang: str, segments: list[TranscriptSegment], source: str, diarized: bool = False
) -> None:
    _store(cache, _transcript_cache_key(video_id, lang, diarized), segments, source)
    cache.delete(_miss_cache_key(video_id, lang))


def load_cached_asr(cache: Cache, video_id: str, lang: str, diarized: bool = False) -> tuple[list[TranscriptSegment], str] | None:
    cached = cache.get(_transcript_cache_key(video_id, lang, diarized))
    if cached and cached.get("segments"):
        return _segments_from_cache(cached), str(cached.get("source") or "gemini")
    return None


def transcript_content_hash(segments: list[TranscriptSegment]) -> str:
    joined = "\n".join(f"{s.start}|{s.speaker}|{s.text}" for s in segments)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
