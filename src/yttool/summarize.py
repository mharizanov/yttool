"""Level-1 map-reduce: one video's transcript -> structured summary JSON.

Chunk and merge prompts both produce the same per-video shape, so a
single-chunk video (the common case) skips the merge call entirely.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .cache import Cache, stable_hash
from .chunking import assign_source_ids, chunk_segments, clean_text
from .llm import LlmConfig, call_llm_json, log
from .mindmap import clean_label, normalize_nodes, normalize_source_ids
from .models import TranscriptSegment, VideoResult
from .prompts_io import render_template
from .transcripts import transcript_content_hash

MAX_QUOTES_PER_VIDEO = 8
QUOTE_MIN_WORDS = 4
QUOTE_MAX_WORDS = 60
QUOTE_MATCH_THRESHOLD = 0.7  # share of a quote's content tokens that must appear near the cited segment

_TOKEN_RE = re.compile(r"[0-9a-z\u00c0-\u024f\u0400-\u04ff]+")


def _content_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, dropping 1-2 letter words (articles, 'a', 'of')
    that would make any two English sentences look alike."""
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3 or t.isdigit()]


def normalize_bullets(value: Any, max_items: int = 15) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    bullets: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            text = clean_text(item)
            source_ids: list[str] = []
        elif isinstance(item, dict):
            text = clean_text(str(item.get("text") or item.get("point") or item.get("bullet") or ""))
            source_ids = normalize_source_ids(item.get("source_ids") or item.get("source_id") or [])
        else:
            continue
        if text:
            bullets.append({"text": text, "source_ids": source_ids})
        if len(bullets) >= max_items:
            break
    return bullets


def normalize_quotes(value: Any, max_items: int = MAX_QUOTES_PER_VIDEO) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    quotes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        text = clean_text(str(item.get("quote") or item.get("text") or item.get("line") or ""))
        text = text.strip("\"'\u201c\u201d\u2018\u2019 ")
        words = text.split()
        if not (QUOTE_MIN_WORDS <= len(words) <= QUOTE_MAX_WORDS):
            continue
        key = " ".join(_content_tokens(text))
        if not key or key in seen:
            continue
        seen.add(key)
        quotes.append(
            {
                "quote": text,
                "speaker": clean_label(item.get("speaker") or "", 80),
                "why": clean_text(str(item.get("why") or ""))[:200],
                "source_ids": normalize_source_ids(item.get("source_ids") or item.get("source_id") or []),
            }
        )
        if len(quotes) >= max_items:
            break
    return quotes


def verify_quotes(quotes: list[dict[str, Any]], segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    """Keep only quotes whose words actually occur in the transcript around the cited
    segment. The LLM is asked for near-verbatim lines; this is the check that makes
    them citable - a reworded or invented line is dropped, the same way invented
    source ids and comment ids are. The cited source_id is rewritten to the segment
    that matched best, so the deep link lands where the line is spoken."""
    if not quotes or not segments:
        return []
    by_id = {seg.source_id: i for i, seg in enumerate(segments) if seg.source_id}
    kept: list[dict[str, Any]] = []
    for quote in quotes:
        tokens = _content_tokens(quote["quote"])
        if not tokens:
            continue
        needed = set(tokens)
        best_score, best_sid = (0.0, 0.0), ""
        for sid in quote.get("source_ids") or []:
            center = by_id.get(sid)
            if center is None:
                continue
            # The quote may straddle segment boundaries, so a segment is scored on its
            # own text plus its neighbours; the own-text share breaks ties so the anchor
            # lands on the segment where the line is actually spoken. Centre first.
            for i in (center, center - 1, center + 1):
                if not 0 <= i < len(segments):
                    continue
                own = set(_content_tokens(segments[i].text))
                window = set(own)
                if i > 0:
                    window |= set(_content_tokens(segments[i - 1].text))
                if i + 1 < len(segments):
                    window |= set(_content_tokens(segments[i + 1].text))
                score = (len(needed & window) / len(needed), len(needed & own) / len(needed))
                if score > best_score:
                    best_score, best_sid = score, segments[i].source_id
        if best_score[0] >= QUOTE_MATCH_THRESHOLD:
            kept.append({**quote, "source_ids": [best_sid] + [s for s in quote["source_ids"] if s != best_sid]})
    return kept


def normalize_video_summary(data: dict[str, Any], default_title: str = "") -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for section in data.get("sections") or data.get("summary_md_sections") or []:
        if not isinstance(section, dict):
            continue
        heading = clean_label(section.get("heading") or section.get("title") or "")
        bullets = normalize_bullets(section.get("bullets") or section.get("points") or [])
        if heading and bullets:
            sections.append({"heading": heading, "bullets": bullets})

    claims: list[dict[str, Any]] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        text = clean_text(str(claim.get("claim") or claim.get("text") or ""))
        if not text:
            continue
        stance = clean_text(str(claim.get("stance") or "asserted")).lower()
        if stance not in ("asserted", "opinion", "speculative"):
            stance = "asserted"
        claims.append(
            {
                "claim": text,
                "stance": stance,
                "source_ids": normalize_source_ids(claim.get("source_ids") or claim.get("source_id") or []),
            }
        )

    themes = [clean_label(t, 80) for t in (data.get("themes") or []) if clean_label(t, 80)][:10]

    verdict_raw = data.get("watch_verdict") or {}
    if not isinstance(verdict_raw, dict):
        verdict_raw = {}
    try:
        score = int(verdict_raw.get("score_1_5") or verdict_raw.get("score") or 3)
    except (TypeError, ValueError):
        score = 3
    score = max(1, min(5, score))
    verdict = {"score_1_5": score, "reason": clean_text(str(verdict_raw.get("reason") or ""))}

    return {
        "title": clean_label(data.get("title") or default_title),
        "tl_dr": clean_text(str(data.get("tl_dr") or data.get("tldr") or data.get("summary") or "")),
        "sections": sections,
        "claims": claims,
        "themes": themes,
        "quotes": normalize_quotes(data.get("quotes") or []),
        "watch_verdict": verdict,
        "nodes": normalize_nodes(data.get("nodes") or []),
    }


def summary_cache_key(video_id: str, model: str, prompts_hash: str, transcript_hash: str) -> str:
    return f"summaries/{video_id}.{stable_hash(model, prompts_hash, transcript_hash)}.json"


def summarize_video(
    client: Any,
    llm_config: LlmConfig,
    result: VideoResult,
    prompts: dict[str, str],
    prompts_hash: str,
    cache: Cache,
    api_mode: str = "auto",
    chunk_chars: int = 12000,
    max_output_tokens: int = 10000,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Summarize one video, returning the normalized per-video summary JSON."""
    meta = result.meta
    # Deterministic, so safe to assign before the cache check - and REQUIRED
    # there: cached summaries cite source ids, and link resolution reads them
    # off the segments even when no LLM call happens.
    assign_source_ids(result.segments)
    cache_key = summary_cache_key(
        meta.video_id, llm_config.model, prompts_hash, transcript_content_hash(result.segments)
    )
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached:
            log(f"{meta.video_id}: summary cache hit")
            return cached

    chunks = chunk_segments(result.segments, chunk_chars)
    if not chunks:
        raise ValueError("transcript produced no chunks")

    common = {
        "video_title": meta.title,
        "channel": meta.channel or "unknown",
        "duration": str(int(meta.duration)) + "s" if meta.duration else "unknown",
    }

    partials: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        log(f"{meta.video_id}: summarizing chunk {index}/{len(chunks)}")
        user_prompt = render_template(
            prompts["video_chunk_user"],
            {
                **common,
                "chunk_index": str(index),
                "chunk_count": str(len(chunks)),
                "transcript_chunk": chunk,
            },
        )
        data = call_llm_json(
            client, llm_config, prompts["video_chunk_system"], user_prompt,
            label=f"{meta.video_id} chunk {index}", api_mode=api_mode, max_output_tokens=max_output_tokens,
        )
        partial = normalize_video_summary(data, meta.title)
        partial["quotes"] = verify_quotes(partial["quotes"], result.segments)
        partials.append(partial)

    if len(partials) == 1:
        summary = partials[0]
    else:
        log(f"{meta.video_id}: merging {len(partials)} partial summaries")
        user_prompt = render_template(
            prompts["video_merge_user"],
            {**common, "partials_json": json.dumps(partials, ensure_ascii=False, indent=2)},
        )
        # The merge emits the full summary plus per-node tldrs; give it headroom
        # so tldr text never competes with map detail for output budget.
        data = call_llm_json(
            client, llm_config, prompts["video_merge_system"], user_prompt,
            label=f"{meta.video_id} merge", api_mode=api_mode,
            max_output_tokens=max(max_output_tokens, 16000),
        )
        summary = normalize_video_summary(data, meta.title)
        # The merge is told to copy quotes verbatim; re-verify in case it reworded one.
        summary["quotes"] = verify_quotes(summary["quotes"], result.segments)

    if not summary["title"]:
        summary["title"] = meta.title
    cache.put(cache_key, summary)
    return summary
