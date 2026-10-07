"""YouTube search via yt-dlp ytsearch (no API key, no quota)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .cache import Cache, stable_hash
from .llm import log
from .models import VideoMeta
from .pacing import youtube_gate

SEARCH_TTL_SECONDS = 24 * 3600
# Statuses that make a video useless for transcript fetching right now.
EXCLUDED_LIVE_STATUSES = {"is_live", "is_upcoming"}


class SearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchFilters:
    """Optional scoping for searches: one channel, a time window, a length range."""

    channel_url: str | None = None
    since_bucket: str | None = None  # hour | day | week | month | year (native, free)
    since_date: str | None = None  # YYYYMMDD (precise; costs per-candidate metadata fetches)
    min_seconds: int | None = None
    max_seconds: int | None = None

    @property
    def active(self) -> bool:
        return any((self.channel_url, self.since_bucket, self.since_date, self.min_seconds, self.max_seconds))

    def cache_repr(self) -> str:
        return f"{self.channel_url}|{self.since_bucket}|{self.since_date}|{self.min_seconds}|{self.max_seconds}"


SINCE_BUCKETS = {"hour": 1, "day": 2, "week": 3, "month": 4, "year": 5}


def compose_sp(
    since_bucket: str | None = None,
    min_seconds: int | None = None,
    max_seconds: int | None = None,
) -> str | None:
    """Build YouTube's `sp` search-filter parameter (a tiny protobuf, hand-rolled).

    Filter message: 0x12 <len> [0x08 <date_enum>] [0x18 <dur_enum>], base64url.
    Duration enums only pre-narrow (short <4min / long >20min); the precise cut
    is the duration post-filter."""
    inner = b""
    if since_bucket:
        inner += b"\x08" + bytes([SINCE_BUCKETS[since_bucket]])
    duration_enum = None
    if max_seconds is not None and max_seconds <= 240:
        duration_enum = 1  # short
    elif min_seconds is not None and min_seconds >= 1200:
        duration_enum = 2  # long
    if duration_enum:
        inner += b"\x18" + bytes([duration_enum])
    if not inner:
        return None
    return base64.urlsafe_b64encode(b"\x12" + bytes([len(inner)]) + inner).decode()


def _search_target(query: str, count: int, filters: SearchFilters | None) -> list[str]:
    """The yt-dlp target + extra args for a (possibly filtered) keyword search."""
    if filters and filters.channel_url:
        # Channel search pages ignore sp filters; post-filters do the cutting.
        url = f"{filters.channel_url}/search?query={urllib.parse.quote(query)}"
        return [url, "--playlist-items", f"1:{count}"]
    sp = compose_sp(filters.since_bucket, filters.min_seconds, filters.max_seconds) if filters else None
    if sp:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp={sp}"
        return [url, "--playlist-items", f"1:{count}"]
    return [f"ytsearch{count}:{query}"]


def _run_ytsearch(query: str, count: int, filters: SearchFilters | None = None) -> list[dict]:
    youtube_gate()
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        *_search_target(query, count, filters),
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 and not proc.stdout.strip():
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        raise SearchError(
            f"yt-dlp search failed (rc={proc.returncode}): {stderr_tail}\n"
            "Try updating yt-dlp (pip install -U yt-dlp) or re-running with --force-refresh."
        )
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def parse_search_entries(
    entries: list[dict], top: int | None = None, filters: SearchFilters | None = None
) -> list[VideoMeta]:
    """Filter flat-playlist entries down to usable videos, keeping search order."""
    metas: list[VideoMeta] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not video_id or not isinstance(video_id, str):
            continue
        if entry.get("live_status") in EXCLUDED_LIVE_STATUSES:
            continue
        if entry.get("availability") in ("subscriber_only", "premium_only", "needs_auth"):
            continue
        if filters is not None:
            duration = entry.get("duration")
            # Drop only when duration is known and out of range; unknowns pass.
            if duration is not None:
                if filters.min_seconds is not None and duration < filters.min_seconds:
                    continue
                if filters.max_seconds is not None and duration > filters.max_seconds:
                    continue
        metas.append(
            VideoMeta(
                video_id=video_id,
                title=str(entry.get("title") or video_id),
                channel=str(entry.get("channel") or entry.get("uploader") or ""),
                duration=entry.get("duration"),
                view_count=entry.get("view_count"),
            )
        )
        if top is not None and len(metas) >= top:
            break
    return metas


def apply_date_filter(
    metas: list[VideoMeta], filters: SearchFilters | None, cache: Cache, top: int
) -> list[VideoMeta]:
    """Precise --since date cut: fetch upload_date per candidate (cached, paced)
    until `top` survivors are found, capped at ~3x top metadata fetches."""
    if not filters or not filters.since_date:
        return metas
    kept: list[VideoMeta] = []
    examined = 0
    cap = max(top * 3, 10)
    for meta in metas:
        if len(kept) >= top:
            break
        if examined >= cap:
            log(f"date filter: metadata-fetch cap ({cap}) reached; {len(metas) - examined} candidate(s) skipped")
            break
        examined += 1
        try:
            full = fetch_video_meta(meta.video_id, cache)
            if not full.upload_date:
                full = fetch_video_meta(meta.video_id, cache, force_refresh=True)
        except SearchError as exc:
            log(f"{meta.video_id}: metadata fetch failed for date filter ({exc}); dropping")
            continue
        if full.upload_date and full.upload_date >= filters.since_date:
            meta.upload_date = full.upload_date
            kept.append(meta)
        elif not full.upload_date:
            log(f"{meta.video_id}: no upload date available; dropping under --since filter")
    return kept


def search(
    query: str,
    top: int,
    lang: str,
    cache: Cache,
    force_refresh: bool = False,
    filters: SearchFilters | None = None,
) -> list[VideoMeta]:
    filters_repr = filters.cache_repr() if filters else ""
    cache_key = f"search/{stable_hash(query, top, lang, filters_repr)}.json"
    if not force_refresh:
        cached = cache.get(cache_key, ttl_seconds=SEARCH_TTL_SECONDS)
        if cached is not None:
            log(f"search cache hit ({len(cached)} videos)")
            return [VideoMeta.from_dict(item) for item in cached]

    # Over-fetch so filtered-out live/members-only entries get backfilled.
    fetch_count = min(top + max(3, top // 2), 50)
    log(f"searching YouTube: {query!r} (top {fetch_count})")
    entries = _run_ytsearch(query, fetch_count, filters)
    metas = parse_search_entries(entries, None if filters and filters.since_date else top, filters)
    metas = apply_date_filter(metas, filters, cache, top)[: top] if filters and filters.since_date else metas
    if not metas:
        raise SearchError(
            f"no usable videos found for query: {query!r}. "
            "Compound topics often have no literal matches - smart search (the default) "
            "decomposes them; check you are not running with --no-smart-search."
        )
    cache.put(cache_key, [meta.to_dict() for meta in metas])
    return metas


# --- direct video input (URL or ID) -------------------------------------------

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
META_TTL_SECONDS = 7 * 24 * 3600


def extract_video_id(text: str) -> str | None:
    """Return the video ID if text is a YouTube URL or a bare video ID, else None."""
    text = text.strip()
    if VIDEO_ID_RE.match(text):
        # A plain lowercase 11-letter word ("engineering") is far more likely a
        # search term than an ID; real IDs virtually always mix case/digits.
        if text.islower() and text.isalpha():
            return None
        return text
    try:
        parsed = urllib.parse.urlparse(text if "//" in text else f"https://{text}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None
    if host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query and query["v"] and VIDEO_ID_RE.match(query["v"][0]):
            return query["v"][0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            return parts[1] if VIDEO_ID_RE.match(parts[1]) else None
    return None


CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@[\w.-]{3,}$")


def extract_channel_url(text: str) -> str | None:
    """Return a canonical channel URL if text is a channel ID, @handle, or
    channel URL, else None. Video URLs are not channels."""
    text = text.strip().rstrip("/")
    if CHANNEL_ID_RE.match(text):
        return f"https://www.youtube.com/channel/{text}"
    if HANDLE_RE.match(text):
        return f"https://www.youtube.com/{text}"
    if extract_video_id(text):
        return None
    try:
        parsed = urllib.parse.urlparse(text if "//" in text else f"https://{text}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in ("youtube.com", "music.youtube.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    if parts[0].startswith("@") and HANDLE_RE.match(parts[0]):
        return f"https://www.youtube.com/{parts[0]}"
    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
        return f"https://www.youtube.com/channel/{parts[1]}"
    if len(parts) >= 2 and parts[0] in ("c", "user"):
        return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None


def channel_latest(
    channel_url: str,
    count: int,
    cache: Cache,
    force_refresh: bool = False,
    filters: SearchFilters | None = None,
) -> list[VideoMeta]:
    """Fetch the channel's most recent `count` uploads (newest first)."""
    filters_repr = filters.cache_repr() if filters else ""
    cache_key = f"channel/{stable_hash(channel_url, count, filters_repr)}.json"
    if not force_refresh:
        cached = cache.get(cache_key, ttl_seconds=SEARCH_TTL_SECONDS)
        if cached is not None:
            log(f"channel cache hit ({len(cached)} videos)")
            return [VideoMeta.from_dict(item) for item in cached]

    videos_url = channel_url if channel_url.endswith("/videos") else f"{channel_url}/videos"
    fetch_count = min(count * 2, 50)  # over-fetch so filtered entries backfill
    log(f"listing latest uploads: {videos_url}")
    youtube_gate()
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        videos_url,
        "--flat-playlist",
        "--dump-json",
        "--playlist-items",
        f"1:{fetch_count}",
        "--no-warnings",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 and not proc.stdout.strip():
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        raise SearchError(f"could not list channel uploads for {channel_url}: {stderr_tail}")
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    metas = parse_search_entries(entries, None if filters and filters.since_date else count, filters)
    metas = apply_date_filter(metas, filters, cache, count)[:count] if filters and filters.since_date else metas
    if not metas:
        raise SearchError(f"channel has no usable uploads: {channel_url}")
    backfill_channel_name(metas, entries)
    for meta in metas:
        meta.found_by = "channel"
    cache.put(cache_key, [meta.to_dict() for meta in metas])
    return metas


def backfill_channel_name(metas: list[VideoMeta], entries: list[dict]) -> None:
    """Channel-listing entries carry the channel name only in playlist_* fields."""
    channel_name = ""
    for entry in entries:
        if isinstance(entry, dict):
            channel_name = str(entry.get("playlist_channel") or entry.get("playlist_uploader") or "")
            if channel_name:
                break
    if channel_name:
        for meta in metas:
            if not meta.channel:
                meta.channel = channel_name


def fetch_video_meta(video_id: str, cache: Cache, force_refresh: bool = False) -> VideoMeta:
    """Fetch title/channel/duration for one directly-specified video."""
    cache_key = f"meta/{video_id}.json"
    if not force_refresh:
        cached = cache.get(cache_key, ttl_seconds=META_TTL_SECONDS)
        if cached is not None:
            return VideoMeta.from_dict(cached)

    youtube_gate()
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        f"https://www.youtube.com/watch?v={video_id}",
        "--skip-download",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-2:])
        raise SearchError(f"could not fetch metadata for video {video_id}: {stderr_tail}")
    data = json.loads(proc.stdout)
    meta = VideoMeta(
        video_id=video_id,
        title=str(data.get("title") or video_id),
        channel=str(data.get("channel") or data.get("uploader") or ""),
        duration=data.get("duration"),
        view_count=data.get("view_count"),
        upload_date=str(data.get("upload_date") or ""),
        found_by="direct",
    )
    cache.put(cache_key, meta.to_dict())
    return meta


# --- LLM-planned smart search -------------------------------------------------


def plan_queries(
    topic: str, client: Any, llm_config: Any, prompts: dict[str, str], api_mode: str, scope_note: str = ""
) -> list[str]:
    """Ask the LLM for diverse, practical YouTube search queries for the topic."""
    from .llm import call_llm_json
    from .prompts_io import render_template

    data = call_llm_json(
        client, llm_config, prompts["query_plan_system"],
        render_template(prompts["query_plan_user"], {"topic": topic, "scope_note": scope_note}),
        label="query plan", api_mode=api_mode, max_output_tokens=2000,
    )
    queries: list[str] = []
    for item in data.get("queries") or []:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("query") or "").strip()
        else:
            continue
        if text and text.lower() not in (q.lower() for q in queries):
            queries.append(text)
        if len(queries) >= 5:
            break
    return queries


def gather_candidates(
    queries: list[str], per_query: int, filters: SearchFilters | None = None
) -> list[VideoMeta]:
    """Run every query, merge results, dedupe by video id (first query wins)."""
    seen: set[str] = set()
    candidates: list[VideoMeta] = []
    for query in queries:
        log(f"searching YouTube: {query!r} (top {per_query})")
        try:
            entries = _run_ytsearch(query, per_query, filters)
        except SearchError as exc:
            log(f"query {query!r} failed: {exc}")
            continue
        metas = parse_search_entries(entries, filters=filters)
        hits = 0
        for meta in metas:
            if meta.video_id in seen:
                continue
            seen.add(meta.video_id)
            meta.found_by = query
            candidates.append(meta)
            hits += 1
        log(f"  -> {len(metas)} usable hits, {hits} new")
    return candidates


def select_videos(
    topic: str,
    candidates: list[VideoMeta],
    top: int,
    client: Any,
    llm_config: Any,
    prompts: dict[str, str],
    api_mode: str,
) -> list[VideoMeta]:
    """Ask the LLM to pick the most relevant candidates from metadata alone."""
    from .llm import call_llm_json
    from .prompts_io import render_template

    if len(candidates) <= top:
        return candidates

    candidates_json = json.dumps(
        [
            {
                "video_id": meta.video_id,
                "title": meta.title,
                "channel": meta.channel,
                "duration_seconds": meta.duration,
                "view_count": meta.view_count,
                "found_by_query": meta.found_by,
            }
            for meta in candidates
        ],
        ensure_ascii=False,
        indent=2,
    )
    values = {"topic": topic, "top": str(top), "candidates_json": candidates_json}
    data = call_llm_json(
        client, llm_config,
        render_template(prompts["video_select_system"], values),
        render_template(prompts["video_select_user"], values),
        label="video selection", api_mode=api_mode, max_output_tokens=4000,
    )
    by_id = {meta.video_id: meta for meta in candidates}
    selected: list[VideoMeta] = []
    for item in data.get("selected") or []:
        if not isinstance(item, dict):
            continue
        meta = by_id.get(str(item.get("video_id") or ""))
        if meta is None or meta in selected:
            continue
        meta.why = str(item.get("reason") or "").strip()
        selected.append(meta)
        if len(selected) >= top:
            break
    if not selected:
        log("video selection returned nothing usable; falling back to search order")
        return candidates[:top]
    return selected


def smart_search(
    topic: str,
    top: int,
    lang: str,
    cache: Cache,
    client: Any,
    llm_config: Any,
    prompts: dict[str, str],
    api_mode: str = "auto",
    force_refresh: bool = False,
    filters: SearchFilters | None = None,
) -> list[VideoMeta]:
    """LLM-planned search: plan queries -> search each -> merge -> LLM-select top N."""
    filters_repr = filters.cache_repr() if filters else ""
    cache_key = f"search/plan-{stable_hash(topic, top, lang, filters_repr)}.json"
    if not force_refresh:
        cached = cache.get(cache_key, ttl_seconds=SEARCH_TTL_SECONDS)
        if cached is not None:
            log(f"smart-search cache hit ({len(cached)} videos)")
            return [VideoMeta.from_dict(item) for item in cached]

    scope_note = ""
    if filters and filters.channel_url:
        scope_note = (
            f"\nScope: every query will be run ONLY within the channel {filters.channel_url} - "
            "do not include the channel's name in the queries themselves."
        )
    try:
        queries = plan_queries(topic, client, llm_config, prompts, api_mode, scope_note=scope_note)
    except Exception as exc:
        log(f"query planning failed ({type(exc).__name__}: {exc}); using the topic as-is")
        queries = []
    if not queries:
        queries = [topic]
    else:
        log(f"planned {len(queries)} search queries: " + "; ".join(repr(q) for q in queries))

    per_query = max(8, top)
    candidates = gather_candidates(queries, per_query, filters)
    if not candidates and topic not in queries:
        log("no candidates from planned queries; retrying with the raw topic")
        candidates = gather_candidates([topic], per_query, filters)
    candidates = apply_date_filter(candidates, filters, cache, top * 2)
    if not candidates:
        raise SearchError(f"no usable videos found for any query derived from: {topic!r}")

    log(f"{len(candidates)} unique candidates; selecting the best {top}")
    try:
        selected = select_videos(topic, candidates, top, client, llm_config, prompts, api_mode)
    except Exception as exc:
        log(f"video selection failed ({type(exc).__name__}: {exc}); using search order")
        selected = candidates[:top]

    cache.put(cache_key, [meta.to_dict() for meta in selected])
    return selected
