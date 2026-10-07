"""Markdown renderers: per-video summaries, cross-video report, console status table."""

from __future__ import annotations

from typing import Any

from .chunking import fmt_mmss
from .models import VideoResult, VideoStatus

STATUS_LABELS = {
    VideoStatus.OK: "ok",
    VideoStatus.OK_WHISPER: "ok (whisper)",
    VideoStatus.SKIPPED_NO_TRANSCRIPT: "skipped: no transcript",
    VideoStatus.SKIPPED_NO_WHISPER: "skipped: whisper not installed",
    VideoStatus.FAILED_DOWNLOAD: "failed: download",
    VideoStatus.FAILED_SUMMARIZE: "failed: summarize",
    VideoStatus.BLOCKED: "blocked by YouTube",
    VideoStatus.DEFERRED: "deferred: rate limited, retry later",
}


def deep_link(video_id: str, seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={int(seconds)}s"


def _link_md(source_ids: list[str], resolver) -> str:
    hit = resolver(source_ids or [])
    if hit is None:
        return ""
    seconds, url = hit
    return f" [{fmt_mmss(seconds)}]({url})"


def _attribution(speaker: str, meta, source_ids: list[str], resolver) -> str:
    """`— Speaker, [Video title](deep link)`; falls back to the channel name, which for
    talks and lectures is usually the speaker anyway."""
    who = speaker or meta.channel or "unknown"
    hit = resolver(source_ids or [])
    if hit is None:
        return f"— {who}, *{meta.title}*"
    seconds, url = hit
    return f"— {who}, [{meta.title}]({url}) at {fmt_mmss(seconds)}"


def render_quote_md(quote: dict, meta, resolver) -> list[str]:
    attribution = _attribution(quote.get("speaker") or "", meta, quote.get("source_ids") or [], resolver)
    lines = [f"> \u201c{quote['quote']}\u201d", f"> {attribution}"]
    if quote.get("why"):
        lines.append(f"> _{quote['why']}_")
    lines.append("")
    return lines


def render_video_md(result: VideoResult, resolver) -> str:
    meta = result.meta
    summary = result.summary or {}
    verdict = summary.get("watch_verdict") or {}
    lines = [
        f"# {summary.get('title') or meta.title}",
        "",
        f"- **Video:** [{meta.title}]({meta.watch_url})",
        f"- **Channel:** {meta.channel or 'unknown'}",
        f"- **Duration:** {fmt_mmss(meta.duration) if meta.duration else 'unknown'}"
        + (f" · **Views:** {meta.view_count:,}" if meta.view_count else ""),
        f"- **Transcript source:** {result.transcript_source or 'unknown'}",
        f"- **Worth watching:** {verdict.get('score_1_5', '?')}/5 — {verdict.get('reason', '')}",
        "",
    ]
    if summary.get("tl_dr"):
        lines += ["## TL;DR", "", summary["tl_dr"], ""]

    for section in summary.get("sections") or []:
        lines += [f"## {section['heading']}", ""]
        for bullet in section.get("bullets") or []:
            lines.append(f"- {bullet['text']}{_link_md(bullet.get('source_ids'), resolver)}")
        lines.append("")

    claims = summary.get("claims") or []
    if claims:
        lines += ["## Notable claims", ""]
        for claim in claims:
            stance = claim.get("stance", "asserted")
            lines.append(f"- ({stance}) {claim['claim']}{_link_md(claim.get('source_ids'), resolver)}")
        lines.append("")

    quotes = summary.get("quotes") or []
    if quotes:
        lines += ["## Quotable lines", "", "_Near-verbatim, checked against the transcript; each links to the moment it is said._", ""]
        for quote in quotes:
            lines += render_quote_md(quote, meta, resolver)

    insights = result.comment_insights or []
    if insights:
        from .comment_insights import comment_link

        lines += ["## Community insights", ""]
        for item in insights:
            badge = " · uploader" if item.get("is_uploader") else ""
            likes = f"👍 {item['like_count']:,}" if item.get("like_count") else "👍 0"
            link = comment_link(meta.video_id, item["comment_id"])
            lines.append(
                f"- **({item['type']}, {likes})** {item['insight']} — [{item.get('author', 'unknown')}{badge}]({link})"
            )
        lines.append("")

    themes = summary.get("themes") or []
    if themes:
        lines += ["## Themes", "", ", ".join(themes), ""]
    return "\n".join(lines)


def render_report_md(
    report: dict[str, Any],
    results: list[VideoResult],
    resolver,
    query: str,
    synthesized: bool,
) -> str:
    by_index = {r.index: r for r in results}
    lines = [f"# YouTube research: {query}", ""]

    if synthesized and report.get("overview"):
        lines += ["## Overview", "", report["overview"], ""]

    if synthesized and report.get("watch_recommendations"):
        lines += ["## What to actually watch", ""]
        for rec in report["watch_recommendations"]:
            result = by_index.get(rec["video_index"])
            if not result:
                continue
            title = f"[{result.meta.title}]({result.meta.watch_url})"
            lines.append(f"{rec['rank']}. {title} — {rec.get('verdict', '')}")
            if rec.get("why"):
                lines.append(f"   - {rec['why']}")
        lines.append("")

    if synthesized and report.get("common_themes"):
        lines += ["## Common themes", ""]
        for theme in report["common_themes"]:
            lines.append(f"- **{theme['theme']}** — {theme.get('explanation', '')}{_link_md(theme.get('source_ids'), resolver)}")
        lines.append("")

    if synthesized and report.get("disagreements"):
        lines += ["## Where the videos disagree", ""]
        for item in report["disagreements"]:
            lines.append(f"- **{item['topic']}**")
            for pos in item["positions"]:
                result = by_index.get(pos["video_index"])
                label = result.meta.title if result else f"video {pos['video_index']}"
                lines.append(f"  - *{label}*: {pos['position']}{_link_md(pos.get('source_ids'), resolver)}")
        lines.append("")

    if synthesized and report.get("community_signal"):
        from .comment_insights import comment_link

        lines += ["## What the comments add", ""]
        for item in report["community_signal"]:
            refs_md = " · ".join(
                f"[{ref['author']} 👍{ref['like_count']:,}]({comment_link(ref['video_id'], ref['comment_id'])})"
                for ref in item["refs"]
            )
            lines.append(f"- {item['observation']} — {refs_md}")
        lines.append("")

    if synthesized and report.get("best_quotes"):
        note = (
            "_Transcript-verified lines picked across the videos for someone writing about this topic; "
            "the full per-video pool is in each video's summary._"
        )
        lines += ["## Lines worth quoting", "", note, ""]
        for quote in report["best_quotes"]:
            result = by_index.get(quote["video_index"])
            if not result:
                continue
            lines += render_quote_md(quote, result.meta, resolver)

    if not synthesized:
        lines += ["_Fewer than two videos were summarized, so no cross-video synthesis was generated._", ""]

    lines += ["## All videos", ""]
    lines.append("| # | Video | Channel | Status | Transcript | Verdict |")
    lines.append("|---|-------|---------|--------|------------|---------|")
    for result in results:
        verdict = (result.summary or {}).get("watch_verdict") or {}
        verdict_text = f"{verdict.get('score_1_5')}/5" if verdict.get("score_1_5") else "—"
        status = STATUS_LABELS.get(result.status, str(result.status or "?"))
        if result.error and result.status not in (VideoStatus.OK, VideoStatus.OK_WHISPER):
            status += f" ({result.error[:80]})"
        title = result.meta.title.replace("|", "-")
        lines.append(
            f"| {result.index} | [{title}]({result.meta.watch_url}) | {result.meta.channel.replace('|', '-')} "
            f"| {status} | {result.transcript_source or '—'} | {verdict_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_transcript_md(result: VideoResult) -> str:
    """Plain [MM:SS] prefixes only - a per-segment URL would repeat the video id
    hundreds of times and bloat the file for LLM ingestion; the deep-link recipe
    in the header is all a reader (human or model) needs."""
    meta = result.meta
    lines = [
        f"# Transcript: {meta.title}",
        "",
        f"- **Video:** [{meta.title}]({meta.watch_url})",
        f"- **Channel:** {meta.channel or 'unknown'}",
        f"- **Source:** {result.transcript_source or 'unknown'}",
        "- **Deep links:** append `&t=<seconds>s` to the video URL to jump to a timestamp",
        "",
    ]
    for segment in result.segments:
        text = f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text
        if segment.start is not None:
            lines.append(f"[{fmt_mmss(segment.start)}] {text}")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


def render_status_table(results: list[VideoResult]) -> str:
    rows = []
    for result in results:
        status = STATUS_LABELS.get(result.status, str(result.status or "?"))
        title = result.meta.title[:60]
        rows.append(f"  {result.index:>2}. [{status:<28}] {title}")
    return "\n".join(rows)
