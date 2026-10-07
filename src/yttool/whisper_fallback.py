"""Last-resort ASR: download audio via yt-dlp, transcribe with faster-whisper.

faster-whisper decodes m4a/webm through its bundled PyAV, so no ffmpeg binary
is required. Heavy deps are behind the [whisper] extra and lazily imported.
macOS has no CUDA, so defaults are cpu / int8 / model "small".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .cache import Cache
from .llm import log
from .models import TranscriptSegment, VideoMeta
from .pacing import looks_rate_limited, youtube_gate


class WhisperUnavailable(RuntimeError):
    pass


class AudioDownloadError(RuntimeError):
    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


def _load_whisper_model(model_name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise WhisperUnavailable(
            "faster-whisper is not installed. Install the ASR fallback with: pip install 'yttool[whisper]'"
        ) from exc
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def download_audio(meta: VideoMeta, cache: Cache, player_client: str = "tv") -> Path:
    audio_dir = cache.path("audio")
    audio_dir.mkdir(parents=True, exist_ok=True)
    existing = [f for f in audio_dir.glob(f"{meta.video_id}.*") if not f.name.endswith(".part")]
    if existing:
        return existing[0]

    # The tv client falsely reports DRM on some videos; android_vr and the
    # default client mix are reliable fallbacks for audio.
    clients = [player_client]
    for fallback in ("android_vr", "default"):
        if fallback not in clients:
            clients.append(fallback)

    stderr_tail = ""
    tried: list[str] = []
    for client in clients:
        tried.append(client)
        youtube_gate()
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            meta.watch_url,
            "-f",
            "bestaudio[ext=m4a]/bestaudio",
            "--no-playlist",
            "--sleep-requests",
            "2",
            "--no-warnings",
            "--extractor-args",
            f"youtube:player_client={client}",
            "-o",
            str(audio_dir / "%(id)s.%(ext)s"),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        files = [f for f in audio_dir.glob(f"{meta.video_id}.*") if not f.name.endswith(".part")]
        if proc.returncode == 0 and files:
            return files[0]
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-2:])
        if looks_rate_limited(proc.stderr) and "DRM" not in proc.stderr:
            # IP throttling - rotating clients cannot help and only adds requests.
            raise AudioDownloadError(
                f"audio download rate-limited (client {client}): {stderr_tail}", blocked=True
            )
        log(f"{meta.video_id}: audio download via player_client={client} failed, trying next client")
    raise AudioDownloadError(f"audio download failed (clients tried: {', '.join(tried)}): {stderr_tail}")


def transcribe_video(
    meta: VideoMeta,
    cache: Cache,
    lang: str = "en",
    model_name: str = "small",
    player_client: str = "tv",
    keep_audio: bool = False,
) -> list[TranscriptSegment]:
    audio_path = download_audio(meta, cache, player_client)
    log(f"{meta.video_id}: transcribing {audio_path.name} with faster-whisper ({model_name}, cpu/int8) - this can take a while")
    model = _load_whisper_model(model_name)
    whisper_segments, info = model.transcribe(
        str(audio_path),
        language=lang if lang != "auto" else None,
        vad_filter=True,
    )
    segments = [
        TranscriptSegment(float(s.start), float(s.end), s.text.strip())
        for s in whisper_segments
        if s.text.strip()
    ]
    if not keep_audio:
        audio_path.unlink(missing_ok=True)
    log(f"{meta.video_id}: whisper produced {len(segments)} segments (detected language: {info.language})")
    return segments
