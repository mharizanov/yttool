"""ASR fallback via the Gemini API free tier.

Flash models accept audio files directly (up to 9.5h per prompt) and
transcribe well across languages - much faster than local CPU Whisper.
Needs a free API key from https://aistudio.google.com (GEMINI_API_KEY or
"geminiApiKey" in config.json) and the [gemini] extra (google-genai).
"""

from __future__ import annotations

import os
import re
import statistics
import time
from pathlib import Path

from .cache import Cache
from .chunking import clean_text, parse_ts
from .llm import default_config_path, load_config, log, resolve_config_value
from .models import TranscriptSegment, VideoMeta
from .whisper_fallback import download_audio

TRANSCRIBE_PROMPT = (
    "Transcribe this audio completely, in the language actually spoken. "
    "Output ONLY transcript lines, one segment of roughly 15-30 seconds per line, "
    "each in exactly this format:\n"
    "[HH:MM:SS] transcribed text\n"
    "where HH:MM:SS is the segment's start time in the audio. "
    "No commentary, no headers, no translation, no speaker labels."
)

DIARIZE_PROMPT = (
    "Transcribe this audio completely, in the language actually spoken, and identify the distinct speakers. "
    "Output ONLY transcript lines, starting a new line whenever the speaker changes "
    "(and at least every ~30 seconds within a long turn), each in exactly this format:\n"
    "[HH:MM:SS] SpeakerName: transcribed text\n"
    "where HH:MM:SS is the line's start time in the audio.\n"
    "Speaker naming rules:\n"
    "- If a speaker's real name is inferable from the conversation (introductions, being addressed by name), "
    "use that name, consistently and exactly the same spelling on every line.\n"
    "- Otherwise use Speaker 1, Speaker 2, ... consistently by voice.\n"
    "- Never merge different voices under one label.\n"
    "No commentary, no headers, no translation."
)

SPEAKER_RE = re.compile(r"^([^:]{1,40}?):\s+(.*)$")


URL_MODEL_DEFAULT = "gemini-flash-lite-latest"


class GeminiUnavailable(RuntimeError):
    pass


class GeminiQuotaExhausted(GeminiUnavailable):
    """The free-tier daily quota for the model is spent; skip the tier for the run."""


def gemini_api_key(config_path: Path | None = None) -> str | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    config = load_config(config_path or default_config_path())
    return resolve_config_value(config.get("geminiApiKey"))


def gemini_available(config_path: Path | None = None) -> bool:
    if not gemini_api_key(config_path):
        return False
    try:
        from google import genai  # noqa: F401
    except ImportError:
        return False
    return True


def parse_timestamped_lines(text: str, diarize: bool = False) -> list[TranscriptSegment]:
    """Parse '[HH:MM:SS] text' (or '[HH:MM:SS] Name: text' when diarizing) lines;
    end times are the next segment's start."""
    line_re = re.compile(r"^\W*\[?(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\]?[\s:\-]+(.+)$")
    segments: list[TranscriptSegment] = []
    for line in text.splitlines():
        match = line_re.match(line.strip())
        if not match:
            continue
        start = parse_ts(match.group(1))
        body = clean_text(match.group(2))
        if start is None or not body:
            continue
        speaker = ""
        if diarize:
            speaker_match = SPEAKER_RE.match(body)
            if speaker_match:
                speaker = speaker_match.group(1).strip()
                body = clean_text(speaker_match.group(2))
                if not body:
                    continue
        segments.append(TranscriptSegment(start, None, body, speaker=speaker))
    for current, following in zip(segments, segments[1:]):
        current.end = following.start
    return segments


def sanitize_timestamps(
    segments: list[TranscriptSegment], duration: float | None, video_id: str = ""
) -> list[TranscriptSegment]:
    """Repair flash-lite timestamp pathologies using the video's known duration.

    Two observed failure modes: emitting [MM:SS:cc] (centiseconds) that parses
    as H:M:S and inflates every stamp ~60x, and hallucinating transcript lines
    past the video's end. Reinterpret when the centisecond pattern fits, drop
    out-of-range tails, and return [] (caller falls through to the next tier)
    when too little survives to trust."""
    if not segments or not duration:
        return segments
    limit = float(duration) * 1.15 + 10
    if max(s.start for s in segments if s.start is not None) <= limit:
        return segments

    def reinterpret(value: float) -> float:
        h, rem = divmod(value, 3600)
        m, s = divmod(rem, 60)
        return h * 60 + m + s / 100

    starts = [s.start for s in segments if s.start is not None]
    raw_ok = sum(1 for v in starts if v <= limit)
    reint_ok = sum(1 for v in starts if reinterpret(v) <= limit)
    # Discriminator: a true [MM:SS:cc] misparse inflates every stamp ~60x, so
    # the MEDIAN far exceeds the duration; a mere hallucinated tail keeps the
    # median in range and must be truncated, not squashed.
    median_raw = statistics.median(starts)
    if median_raw > duration and reint_ok > raw_ok:
        for s in segments:
            if s.start is not None:
                s.start = round(reinterpret(s.start), 2)
        log(f"{video_id}: repaired [MM:SS:cc] timestamps misparsed as H:M:S")

    kept = [s for s in segments if s.start is not None and s.start <= limit]
    if len(kept) < len(segments):
        log(f"{video_id}: dropped {len(segments) - len(kept)} segment(s) past the video's end ({int(duration)}s)")
    for current, following in zip(kept, kept[1:]):
        current.end = following.start
    if kept:
        kept[-1].end = min(kept[-1].end or duration, duration)
    if len(kept) < max(3, len(segments) // 4):
        log(f"{video_id}: too few plausible timestamps survived sanitation; rejecting transcript")
        return []
    return kept


def transcribe_url_with_gemini(
    meta: VideoMeta,
    lang: str = "en",
    model_name: str = URL_MODEL_DEFAULT,
    config_path: Path | None = None,
    diarize: bool = False,
) -> list[TranscriptSegment]:
    """Transcribe by passing the YouTube URL directly to Gemini.

    Google fetches the video server-side, so this consumes NONE of our
    YouTube request budget - it keeps working even when our IP is
    rate-limited. Costs Gemini quota instead (free-tier caps are per model,
    so the default lite model keeps its own bucket separate from the
    audio-upload path). Raises GeminiQuotaExhausted when the model's quota
    is spent, GeminiUnavailable on per-video failure.
    """
    api_key = gemini_api_key(config_path)
    if not api_key:
        raise GeminiUnavailable(
            "no Gemini API key found. Get a free key at https://aistudio.google.com and "
            "set GEMINI_API_KEY (or geminiApiKey in config.json)."
        )
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiUnavailable("google-genai is not installed. Install with: pip install 'yttool[gemini]'") from exc

    client = genai.Client(api_key=api_key)
    prompt = DIARIZE_PROMPT if diarize else TRANSCRIBE_PROMPT
    log(f"{meta.video_id}: transcribing via Gemini URL ingestion ({model_name})" + (" (diarizing)" if diarize else ""))
    last_error: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=types.Content(parts=[
                    types.Part(file_data=types.FileData(file_uri=meta.watch_url)),
                    types.Part(text=prompt),
                ]),
            )
            segments = parse_timestamped_lines(response.text or "", diarize=diarize)
            segments = sanitize_timestamps(segments, meta.duration, meta.video_id)
            if segments:
                log(f"{meta.video_id}: Gemini URL ingestion produced {len(segments)} segments")
                return segments
            log(f"{meta.video_id}: URL ingestion attempt {attempt} returned no parseable lines")
        except Exception as exc:
            message = str(exc)
            if "RESOURCE_EXHAUSTED" in message:
                if attempt >= 2:
                    raise GeminiQuotaExhausted(
                        f"Gemini free-tier quota exhausted for {model_name}"
                    ) from exc
                time.sleep(30)  # could be the per-minute limit; one patient retry
                continue
            last_error = exc
            log(f"{meta.video_id}: URL ingestion attempt {attempt} failed ({type(exc).__name__})")
            time.sleep(15 * attempt)
    raise GeminiUnavailable(f"Gemini URL transcription failed for {meta.video_id}: {last_error}")


def transcribe_with_gemini(
    meta: VideoMeta,
    cache: Cache,
    lang: str = "en",
    model_name: str = "gemini-flash-latest",
    player_client: str = "tv",
    keep_audio: bool = False,
    config_path: Path | None = None,
    diarize: bool = False,
) -> list[TranscriptSegment]:
    api_key = gemini_api_key(config_path)
    if not api_key:
        raise GeminiUnavailable(
            "no Gemini API key found. Get a free key at https://aistudio.google.com and "
            "set GEMINI_API_KEY (or geminiApiKey in config.json)."
        )
    try:
        from google import genai
    except ImportError as exc:
        raise GeminiUnavailable("google-genai is not installed. Install with: pip install 'yttool[gemini]'") from exc

    audio_path = download_audio(meta, cache, player_client)
    client = genai.Client(api_key=api_key)
    log(f"{meta.video_id}: uploading {audio_path.name} to Gemini ({model_name})")
    uploaded = client.files.upload(file=str(audio_path))
    try:
        deadline = time.time() + 180
        while getattr(uploaded.state, "name", str(uploaded.state)) == "PROCESSING":
            if time.time() > deadline:
                raise GeminiUnavailable("Gemini file processing timed out")
            time.sleep(3)
            uploaded = client.files.get(name=uploaded.name)
        if getattr(uploaded.state, "name", str(uploaded.state)) == "FAILED":
            raise GeminiUnavailable("Gemini could not process the audio file")

        log(f"{meta.video_id}: transcribing with Gemini" + (" (diarizing)" if diarize else ""))
        prompt = DIARIZE_PROMPT if diarize else TRANSCRIBE_PROMPT
        segments: list[TranscriptSegment] = []
        for attempt in (1, 2):
            response = client.models.generate_content(model=model_name, contents=[prompt, uploaded])
            segments = parse_timestamped_lines(response.text or "", diarize=diarize)
            segments = sanitize_timestamps(segments, meta.duration, meta.video_id)
            if segments:
                break
            finish = None
            if getattr(response, "candidates", None):
                finish = getattr(response.candidates[0], "finish_reason", None)
            log(
                f"{meta.video_id}: Gemini attempt {attempt} returned no parseable lines "
                f"(finish_reason={finish}, text_len={len(response.text or '')}); "
                + ("retrying" if attempt == 1 else "giving up")
            )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    if not segments:
        raise GeminiUnavailable("Gemini returned no parseable transcript lines")
    if not keep_audio:
        audio_path.unlink(missing_ok=True)
    log(f"{meta.video_id}: Gemini produced {len(segments)} segments")
    return segments
