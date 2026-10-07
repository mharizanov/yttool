"""Comment-thread fetching via yt-dlp --write-comments.

Comments are fetched with the default web client (comment extraction needs it),
sorted by top, capped at a configurable count, cached per video, and rendered
as threaded Markdown next to the transcript in the run directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .cache import Cache
from .models import VideoMeta, VideoResult
from .pacing import THROTTLE, youtube_gate


def _comments_cache_key(video_id: str) -> str:
    return f"comments/{video_id}.json"


def _normalize_comment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or ""),
        "parent": str(raw.get("parent") or "root"),
        "author": str(raw.get("author") or "unknown"),
        "text": str(raw.get("text") or "").strip(),
        "like_count": raw.get("like_count") or 0,
        "time_text": str(raw.get("_time_text") or ""),
        "is_uploader": bool(raw.get("author_is_uploader")),
        "is_pinned": bool(raw.get("is_pinned")),
    }


def fetch_comments(
    meta: VideoMeta,
    cache: Cache,
    limit: int = 30,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` top-sorted comments (threads included) for a video."""
    cache_key = _comments_cache_key(meta.video_id)
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None and cached.get("limit", 0) >= limit:
            return cached.get("comments") or []
    if THROTTLE.tripped:
        raise RuntimeError("rate-limit circuit breaker tripped; comments deferred")

    youtube_gate()
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        meta.watch_url,
        "--skip-download",
        "--write-comments",
        "--dump-single-json",
        "--sleep-requests",
        "2",
        "--no-warnings",
        "--extractor-args",
        f"youtube:comment_sort=top;max_comments={limit}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not proc.stdout.strip():
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-2:])
        raise RuntimeError(f"comment fetch failed: {stderr_tail}")
    data = json.loads(proc.stdout)
    comments = [_normalize_comment(c) for c in data.get("comments") or [] if isinstance(c, dict)]
    comments = [c for c in comments if c["text"]]
    cache.put(cache_key, {"limit": limit, "comments": comments})
    return comments


def render_comments_md(result: VideoResult, comments: list[dict[str, Any]]) -> str:
    meta = result.meta
    lines = [
        f"# Comments: {meta.title}",
        "",
        f"- **Video:** [{meta.title}]({meta.watch_url})",
        f"- **Comments captured:** {len(comments)} (sorted by top)",
        "",
    ]

    def fmt(comment: dict[str, Any]) -> str:
        author = comment["author"]
        badges = []
        if comment["is_uploader"]:
            badges.append("uploader")
        if comment["is_pinned"]:
            badges.append("pinned")
        badge_text = f" ({', '.join(badges)})" if badges else ""
        likes = f" · 👍 {comment['like_count']}" if comment["like_count"] else ""
        when = f" · {comment['time_text']}" if comment["time_text"] else ""
        text = comment["text"].replace("\n", "\n  > ")
        return f"**{author}**{badge_text}{likes}{when}\n  > {text}"

    replies_by_parent: dict[str, list[dict[str, Any]]] = {}
    top_level: list[dict[str, Any]] = []
    for comment in comments:
        if comment["parent"] == "root":
            top_level.append(comment)
        else:
            replies_by_parent.setdefault(comment["parent"], []).append(comment)

    for comment in top_level:
        lines.append(f"- {fmt(comment)}")
        for reply in replies_by_parent.get(comment["id"], []):
            reply_text = fmt(reply).replace("\n  > ", "\n    > ")
            lines.append(f"  - {reply_text}")
        lines.append("")
    if not top_level:
        lines.append("_No comments captured._")
        lines.append("")
    return "\n".join(lines)
