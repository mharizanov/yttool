"""Distill knowledge-bearing insights from a video's comment threads.

One cached LLM call per video over the captured comments. Upvotes are the
community's relevance vote and are passed through as a ranking signal; the
LLM only picks comments and distills them - author, like count, and the
comment permalink (watch?v=ID&lc=<comment_id>) are filled in by code from
the captured data, never trusted from the model.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .cache import Cache, stable_hash
from .chunking import clean_text
from .llm import LlmConfig, call_llm_json, log
from .models import VideoResult
from .prompts_io import render_template

INSIGHT_TYPES = ("correction", "experience", "counterpoint", "clarification", "tip")
MAX_INSIGHTS = 8


def comments_content_hash(comments: list[dict[str, Any]]) -> str:
    joined = "\n".join(f"{c.get('id')}|{c.get('like_count')}|{c.get('text', '')[:80]}" for c in comments)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def normalize_insights(data: dict[str, Any], comments_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    for item in data.get("insights") or []:
        if not isinstance(item, dict):
            continue
        comment = comments_by_id.get(str(item.get("comment_id") or ""))
        if comment is None:
            continue  # invented or mangled id - drop
        text = clean_text(str(item.get("insight") or item.get("text") or ""))
        if not text:
            continue
        insight_type = clean_text(str(item.get("type") or "")).lower()
        if insight_type not in INSIGHT_TYPES:
            insight_type = "tip"
        insights.append(
            {
                "comment_id": comment["id"],
                "type": insight_type,
                "insight": text,
                "author": comment.get("author") or "unknown",
                "like_count": comment.get("like_count") or 0,
                "is_uploader": bool(comment.get("is_uploader")),
            }
        )
        if len(insights) >= MAX_INSIGHTS:
            break
    return insights


def extract_comment_insights(
    client: Any,
    llm_config: LlmConfig,
    result: VideoResult,
    prompts: dict[str, str],
    cache: Cache,
    api_mode: str = "auto",
    max_output_tokens: int = 4000,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    if not result.comments:
        return []
    meta = result.meta
    prompts_hash = hashlib.sha256(
        (prompts["comments_system"] + prompts["comments_user"]).encode("utf-8")
    ).hexdigest()[:16]
    cache_key = (
        f"comment-insights/{meta.video_id}."
        f"{stable_hash(llm_config.model, prompts_hash, comments_content_hash(result.comments))}.json"
    )
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            log(f"{meta.video_id}: comment-insights cache hit")
            return cached

    comments_json = json.dumps(
        [
            {
                "comment_id": c["id"],
                "parent": c.get("parent", "root"),
                "author": c.get("author"),
                "like_count": c.get("like_count", 0),
                "is_uploader": c.get("is_uploader", False),
                "is_pinned": c.get("is_pinned", False),
                "text": c.get("text", "")[:1000],
            }
            for c in result.comments
        ],
        ensure_ascii=False,
        indent=2,
    )
    tl_dr = (result.summary or {}).get("tl_dr") or ""
    user_prompt = render_template(
        prompts["comments_user"],
        {
            "video_title": meta.title,
            "channel": meta.channel or "unknown",
            "tl_dr": tl_dr,
            "comments_json": comments_json,
        },
    )
    data = call_llm_json(
        client, llm_config, prompts["comments_system"], user_prompt,
        label=f"{meta.video_id} comment insights", api_mode=api_mode, max_output_tokens=max_output_tokens,
    )
    insights = normalize_insights(data, {c["id"]: c for c in result.comments})
    cache.put(cache_key, insights)
    return insights


def comment_link(video_id: str, comment_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
