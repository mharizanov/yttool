"""yttool: search YouTube for a topic, fetch transcripts of the top hits, and
produce LLM summaries, a cross-video report, and an interactive mindmap."""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import __version__
from .cache import Cache
from .llm import build_llm_config, create_client, log
from .mindmap import (
    collect_tooltips,
    inject_deep_links,
    mindmap_to_markdown,
    write_html,
)
from .models import VideoResult, VideoStatus
from .pacing import THROTTLE
from .prompts_io import load_prompts
from .report import (
    render_report_md,
    render_status_table,
    render_transcript_md,
    render_video_md,
)
from .runslug import channel_slug, rename_runs, run_dir_name, slugify, videos_slug
from .search import (
    SINCE_BUCKETS,
    SearchError,
    SearchFilters,
    channel_latest,
    extract_channel_url,
    extract_video_id,
    fetch_video_meta,
    search,
    smart_search,
)
from .summarize import summarize_video
from .synthesize import (
    build_resolution_table,
    make_link_resolver,
    synthesize,
    video_prefix,
)
from .transcripts import (
    NoCaptions,
    TranscriptBlocked,
    acquire_transcript,
    load_cached_asr,
    store_asr_transcript,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yttool",
        description="Search YouTube for a topic, fetch transcripts of the top hits, and produce LLM summaries.",
    )
    parser.add_argument(
        "query",
        nargs="*",
        metavar="TOPIC_OR_URL",
        help="search topic (e.g. \"urban beekeeping basics\"), one or more video URLs/IDs, "
        "or a channel (@handle, channel URL, or UC... id) for its latest --top uploads",
    )
    parser.add_argument("--top", type=int, default=10, help="number of videos to process (default: 10)")
    parser.add_argument("--lang", default="en", help="preferred transcript language (default: en)")
    parser.add_argument(
        "--output-lang",
        default=None,
        metavar="LANG",
        help="force all generated text (summaries, report, mindmap, insights) into this language "
        "regardless of the videos' language, e.g. 'en' or 'Bulgarian' (default: follow the transcript)",
    )
    parser.add_argument("--outdir", type=Path, default=Path("runs"), help="output root directory (default: ./runs)")
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "yttool", help="cache directory")
    # LLM (inherited from an earlier transcription tool)
    parser.add_argument("--model", default=None, help="model id or provider/model id")
    parser.add_argument("--provider", default=None, help="provider name from config (default: inferred from model)")
    parser.add_argument("--config", type=Path, default=None, help="LLM config path (default: $YTTOOL_CONFIG or ./config.json)")
    parser.add_argument("--azure", action="store_true", help="force AzureOpenAI client mode")
    parser.add_argument("--api-mode", choices=["auto", "responses", "chat"], default="auto", help="LLM API surface")
    parser.add_argument("--chunk-chars", type=int, default=12000, help="approximate characters per LLM chunk")
    parser.add_argument("--max-output-tokens", type=int, default=10000, help="max model output tokens per call")
    parser.add_argument("--prompt-dir", type=Path, default=None, help="override the built-in prompt directory")
    # ASR fallback
    parser.add_argument("--no-whisper", action="store_true", help="skip the ASR fallback for caption-less videos entirely")
    parser.add_argument(
        "--transcriber",
        choices=["auto", "whisper", "gemini"],
        default="auto",
        help="ASR backend for caption-less videos: gemini (free API, fast) or local whisper; "
        "auto uses gemini when a GEMINI_API_KEY is configured (default: auto)",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="identify speakers: skip captions and transcribe every video's audio with Gemini using "
        "speaker labels (real names when inferable from context). Needs the gemini transcriber.",
    )
    parser.add_argument("--whisper-model", default="small", help="faster-whisper model (default: small)")
    parser.add_argument("--gemini-model", default="gemini-flash-latest", help="Gemini model for audio-upload transcription")
    parser.add_argument(
        "--gemini-url-model",
        default="gemini-flash-lite-latest",
        help="Gemini model for the URL-ingestion tier (free-tier quotas are per model, "
        "so a different model than --gemini-model keeps separate buckets)",
    )
    parser.add_argument("--no-gemini-url", action="store_true", help="disable the Gemini URL-ingestion transcript tier")
    parser.add_argument("--keep-audio", action="store_true", help="keep downloaded audio files in the cache")
    # Refresh / partial runs
    parser.add_argument("--force-refresh", action="store_true", help="ignore all caches")
    parser.add_argument("--refresh-search", action="store_true", help="re-run only the search")
    parser.add_argument("--dry-run", action="store_true", help="print search hits and exit (with --rename-runs: only list the renames)")
    parser.add_argument("--transcripts-only", action="store_true", help="stop after fetching transcripts")
    # Comments
    parser.add_argument("--comments", type=int, default=30, metavar="N", help="top comments to save per video (default: 30)")
    parser.add_argument("--no-comments", action="store_true", help="skip fetching comment threads")
    # Search filters
    parser.add_argument(
        "--channel",
        default=None,
        metavar="CHANNEL",
        help="restrict a keyword search to one channel (@handle, channel URL, or UC... id)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="WHEN",
        help="only videos uploaded since: hour|day|week|month|year (free, native) "
        "or an ISO date like 2026-05-01 (precise; costs capped per-candidate metadata fetches)",
    )
    parser.add_argument(
        "--min-duration",
        default=None,
        metavar="LEN",
        help="minimum video length, e.g. 90s, 10m, 1h30m; bare number = minutes",
    )
    parser.add_argument(
        "--max-duration",
        default=None,
        metavar="LEN",
        help="maximum video length, e.g. 20m or 1h; bare number = minutes",
    )
    # Search
    parser.add_argument(
        "--no-smart-search",
        action="store_true",
        help="skip LLM query planning/selection and search the topic literally (works without an API key)",
    )
    # Networking
    parser.add_argument(
        "--player-client",
        default="default",
        help="yt-dlp youtube player_client for subs/audio (default: default; note the tv client "
        "serves only DRM formats to anonymous requests as of 2026)",
    )
    parser.add_argument("--concurrency", type=int, default=2, help="parallel transcript fetches (max 3)")
    parser.add_argument("--llm-concurrency", type=int, default=4, help="parallel per-video LLM summarizations")
    parser.add_argument(
        "--rename-runs", action="store_true",
        help="maintenance: rename existing run directories under --outdir to the current "
        "content-based slug scheme (recomputed from each run.json), then exit",
    )
    parser.add_argument("--version", action="version", version=f"yttool {__version__}")
    return parser


def parse_duration_arg(text: str) -> int:
    """'90s', '10m', '1h30m', or a bare number (minutes) -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text) * 60
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if not match or not any(match.groups()):
        raise SystemExit(f"ERROR: cannot parse duration {text!r} (use e.g. 90s, 10m, 1h30m, or minutes)")
    h, m, s = (int(g or 0) for g in match.groups())
    return h * 3600 + m * 60 + s


def build_search_filters(args: argparse.Namespace) -> SearchFilters | None:
    channel_url = None
    if args.channel:
        channel_url = extract_channel_url(args.channel)
        if not channel_url:
            raise SystemExit(f"ERROR: --channel {args.channel!r} is not a recognizable channel (@handle, URL, or UC... id)")
    since_bucket = since_date = None
    if args.since:
        value = args.since.strip().lower()
        if value in SINCE_BUCKETS:
            since_bucket = value
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            since_date = value.replace("-", "")
        else:
            raise SystemExit(f"ERROR: --since must be hour|day|week|month|year or YYYY-MM-DD, got {args.since!r}")
    min_seconds = parse_duration_arg(args.min_duration) if args.min_duration else None
    max_seconds = parse_duration_arg(args.max_duration) if args.max_duration else None
    if min_seconds is not None and max_seconds is not None and min_seconds > max_seconds:
        raise SystemExit("ERROR: --min-duration exceeds --max-duration")
    filters = SearchFilters(
        channel_url=channel_url, since_bucket=since_bucket, since_date=since_date,
        min_seconds=min_seconds, max_seconds=max_seconds,
    )
    return filters if filters.active else None


def fetch_one_transcript(result: VideoResult, args: argparse.Namespace, cache: Cache, first: bool) -> None:
    """Tiers 1-2 (API, VTT). Sets segments+source or an intermediate error state.

    Pacing between requests is handled globally by pacing.youtube_gate(); the
    THROTTLE circuit breaker stops the whole fetch phase after repeated blocks."""
    if THROTTLE.tripped:
        result.status = VideoStatus.DEFERRED
        result.error = "rate-limit circuit breaker tripped; re-run after a cooldown"
        return
    player_clients = [args.player_client]
    for fallback in ("android_vr", "default"):
        if fallback not in player_clients:
            player_clients.append(fallback)
    try:
        segments, source = acquire_transcript(
            result.meta, args.lang, cache, player_clients, force_refresh=args.force_refresh,
            comments_limit=0 if args.no_comments else args.comments,
        )
        result.segments = segments
        result.transcript_source = source
        result.status = VideoStatus.OK
        THROTTLE.record_success()
    except TranscriptBlocked as exc:
        result.status = VideoStatus.BLOCKED
        result.error = str(exc)
        THROTTLE.record_block()
        if THROTTLE.tripped:
            log("rate-limit circuit breaker TRIPPED: deferring all remaining fetches")
    except NoCaptions as exc:
        result.status = VideoStatus.SKIPPED_NO_TRANSCRIPT
        result.error = str(exc)
    except Exception as exc:
        result.status = VideoStatus.FAILED_DOWNLOAD
        result.error = f"{type(exc).__name__}: {exc}"


def run_asr_fallback(results: list[VideoResult], args: argparse.Namespace, cache: Cache) -> None:
    from .gemini_transcriber import (
        GeminiQuotaExhausted,
        GeminiUnavailable,
        gemini_available,
        transcribe_url_with_gemini,
        transcribe_with_gemini,
    )
    from .whisper_fallback import (
        AudioDownloadError,
        WhisperUnavailable,
        transcribe_video,
    )

    backend = args.transcriber
    if backend == "auto":
        backend = "gemini" if gemini_available(args.config) else "whisper"
    if args.diarize and backend != "gemini":
        raise SystemExit(
            "ERROR: --diarize needs the gemini transcriber. Set GEMINI_API_KEY (free key: "
            "https://aistudio.google.com) or pass --transcriber gemini; local whisper cannot diarize."
        )

    # Tier 3: Gemini URL ingestion - Google fetches the video server-side, so it
    # spends no YouTube request budget and works even while our IP is blocked.
    url_tier = (not args.no_gemini_url) and gemini_available(args.config)

    candidates = [r for r in results if r.status in (VideoStatus.SKIPPED_NO_TRANSCRIPT, VideoStatus.BLOCKED)]
    if candidates:
        tiers = (["gemini-url"] if url_tier else []) + [backend]
        log(f"ASR fallback tiers: {' -> '.join(tiers)}" + (" (diarizing)" if args.diarize else ""))
    for result in candidates:
        if THROTTLE.tripped and not url_tier:
            # URL ingestion doesn't touch YouTube, so the breaker doesn't gate it.
            result.status = VideoStatus.DEFERRED
            result.error = "rate-limit circuit breaker tripped; re-run after a cooldown"
            continue
        segments = None
        source = backend
        if url_tier:
            try:
                segments = transcribe_url_with_gemini(
                    result.meta, lang=args.lang, model_name=args.gemini_url_model,
                    config_path=args.config, diarize=args.diarize,
                )
                source = "gemini-url"
            except GeminiQuotaExhausted as exc:
                log(f"{exc}; disabling the URL tier for this run")
                url_tier = False
            except GeminiUnavailable as exc:
                log(f"{result.meta.video_id}: URL ingestion failed ({exc}); trying the audio path")
            except Exception as exc:
                log(f"{result.meta.video_id}: URL ingestion error ({type(exc).__name__}: {exc}); trying the audio path")
        if segments is None and THROTTLE.tripped:
            result.status = VideoStatus.DEFERRED
            result.error = "rate-limit circuit breaker tripped; re-run after a cooldown"
            continue
        try:
            if segments is None:
                if backend == "gemini":
                    segments = transcribe_with_gemini(
                        result.meta, cache, lang=args.lang, model_name=args.gemini_model,
                        player_client=args.player_client, keep_audio=args.keep_audio, config_path=args.config,
                        diarize=args.diarize,
                    )
                else:
                    segments = transcribe_video(
                        result.meta, cache, lang=args.lang, model_name=args.whisper_model,
                        player_client=args.player_client, keep_audio=args.keep_audio,
                    )
            if not segments:
                raise NoCaptions("ASR produced no speech segments")
            result.segments = segments
            result.transcript_source = source
            result.status = VideoStatus.OK_WHISPER
            result.error = None
            THROTTLE.record_success()
            store_asr_transcript(cache, result.meta.video_id, args.lang, segments, source, diarized=args.diarize)
        except (WhisperUnavailable, GeminiUnavailable) as exc:
            result.status = VideoStatus.SKIPPED_NO_WHISPER
            result.error = str(exc)
            log(str(exc))
            if not url_tier:
                return  # no usable tier remains; stop trying the remaining candidates
        except AudioDownloadError as exc:
            if exc.blocked:
                result.status = VideoStatus.BLOCKED
                THROTTLE.record_block()
                if THROTTLE.tripped:
                    log("rate-limit circuit breaker TRIPPED: deferring remaining ASR fallbacks")
            elif result.status != VideoStatus.BLOCKED:
                result.status = VideoStatus.FAILED_DOWNLOAD
            result.error = str(exc)
            log(f"{result.meta.video_id}: {exc}")
        except Exception as exc:
            if result.status != VideoStatus.BLOCKED:
                result.status = VideoStatus.FAILED_DOWNLOAD
            result.error = f"{type(exc).__name__}: {exc}"
            log(f"{result.meta.video_id}: {backend} fallback failed: {result.error}")


def fetch_all_comments(results: list[VideoResult], args: argparse.Namespace, cache: Cache) -> None:
    from .comments import fetch_comments

    def work(result: VideoResult) -> None:
        try:
            result.comments = fetch_comments(result.meta, cache, limit=args.comments, force_refresh=args.force_refresh)
        except Exception as exc:
            log(f"{result.meta.video_id}: comment fetch failed ({type(exc).__name__}: {exc}); continuing without comments")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(work, results))
    fetched = sum(1 for r in results if r.comments)
    log(f"comments: captured threads for {fetched}/{len(results)} videos")


def summarize_all(results: list[VideoResult], args: argparse.Namespace, cache: Cache) -> None:
    prompts, prompts_hash = load_prompts(args.prompt_dir, output_lang=args.output_lang)
    llm_config = build_llm_config(args.model, args.provider, args.config, args.azure)
    client = create_client(llm_config)
    log(f"using provider={llm_config.provider} model={llm_config.model}")

    ready = [r for r in results if r.has_transcript]

    from .comment_insights import extract_comment_insights

    def work(result: VideoResult) -> None:
        try:
            result.summary = summarize_video(
                client, llm_config, result, prompts, prompts_hash, cache,
                api_mode=args.api_mode, chunk_chars=args.chunk_chars,
                max_output_tokens=args.max_output_tokens, force_refresh=args.force_refresh,
            )
        except SystemExit:
            raise
        except Exception as exc:
            result.status = VideoStatus.FAILED_SUMMARIZE
            result.error = f"{type(exc).__name__}: {exc}"
            log(f"{result.meta.video_id}: summarization failed: {result.error}")
            return
        if result.comments:
            try:
                result.comment_insights = extract_comment_insights(
                    client, llm_config, result, prompts, cache,
                    api_mode=args.api_mode, force_refresh=args.force_refresh,
                )
            except Exception as exc:
                log(f"{result.meta.video_id}: comment-insight extraction failed ({type(exc).__name__}: {exc}); continuing")

    with ThreadPoolExecutor(max_workers=max(1, args.llm_concurrency)) as pool:
        list(pool.map(work, ready))

    summarized = [r for r in ready if r.summarized]
    if len(summarized) >= 2:
        try:
            report, mindmap = synthesize(
                client, llm_config, summarized, args.query, prompts,
                api_mode=args.api_mode, max_output_tokens=max(args.max_output_tokens, 16000),
            )
            args._report, args._mindmap = report, mindmap
        except Exception as exc:
            log(f"cross-video synthesis failed: {type(exc).__name__}: {exc} (per-video summaries are still written)")
            args._report, args._mindmap = None, None
    else:
        log("fewer than 2 summarized videos; skipping cross-video synthesis")
        args._report, args._mindmap = None, None


def write_outputs(results: list[VideoResult], args: argparse.Namespace) -> Path:
    slug = getattr(args, "_slug", None) or slugify(args.query)
    run_dir = args.outdir / run_dir_name(slug)
    videos_dir = run_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    # Full transcripts for every video that has one, summarized or not.
    transcripts_dir = run_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        if result.has_transcript:
            path = transcripts_dir / f"{result.index:02d}-{result.meta.video_id}.md"
            path.write_text(render_transcript_md(result), encoding="utf-8")

    # Comment threads, next to the transcripts.
    from .comments import render_comments_md

    if any(r.comments for r in results):
        comments_dir = run_dir / "comments"
        comments_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            if result.comments:
                path = comments_dir / f"{result.index:02d}-{result.meta.video_id}.md"
                path.write_text(render_comments_md(result, result.comments), encoding="utf-8")

    # Per-video markdown with local (unprefixed) source-id resolution.
    for result in results:
        if not result.summarized:
            continue
        local_table = {
            seg.source_id: (result.meta.video_id, float(seg.start))
            for seg in result.segments
            if seg.source_id and seg.start is not None
        }
        resolver = make_link_resolver(local_table)
        path = videos_dir / f"{result.index:02d}-{result.meta.video_id}.md"
        path.write_text(render_video_md(result, resolver), encoding="utf-8")

    # Cross-video report + mindmap with V-prefixed resolution.
    summarized = [r for r in results if r.summarized]
    global_resolver = make_link_resolver(build_resolution_table(summarized))
    report = getattr(args, "_report", None)
    mindmap = getattr(args, "_mindmap", None)
    synthesized = report is not None

    report_md = render_report_md(report or {}, results, global_resolver, args.query, synthesized)
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")

    mindmap_resolver = global_resolver
    if mindmap is None and len(summarized) == 1:
        # Single-video runs have no cross-video synthesis, but the per-video
        # summary already carries mindmap nodes with plain S-prefixed ids.
        only = summarized[0]
        summary = only.summary or {}
        if summary.get("nodes"):
            mindmap = {
                "title": summary.get("title") or only.meta.title,
                "summary": summary.get("tl_dr") or "",
                "nodes": summary["nodes"],
            }
            mindmap_resolver = make_link_resolver(
                {
                    seg.source_id: (only.meta.video_id, float(seg.start))
                    for seg in only.segments
                    if seg.source_id and seg.start is not None
                }
            )

    if mindmap is not None:
        inject_deep_links(mindmap, mindmap_resolver)
        markdown = mindmap_to_markdown(mindmap)
        (run_dir / "mindmap.md").write_text(markdown, encoding="utf-8")
        write_html(
            markdown, mindmap.get("title") or args.query, run_dir / "mindmap.html",
            tooltips=collect_tooltips(mindmap),
        )

    run_meta = {
        "query": args.query,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top": args.top,
        "lang": args.lang,
        "model": args.model,
        "diarize": args.diarize,
        "filters": {
            "channel": args.channel, "since": args.since,
            "min_duration": args.min_duration, "max_duration": args.max_duration,
        } if (args.channel or args.since or args.min_duration or args.max_duration) else None,
        "videos": [
            {
                "index": r.index,
                "video_id": r.meta.video_id,
                "title": r.meta.title,
                "channel": r.meta.channel,
                "found_by": r.meta.found_by,
                "why_selected": r.meta.why,
                "status": r.status.value if r.status else None,
                "transcript_source": r.transcript_source,
                "segments": len(r.segments),
                "comments": len(r.comments),
                "comment_insights": len(r.comment_insights),
                "quotes": len((r.summary or {}).get("quotes") or []),
                "error": r.error,
            }
            for r in results
        ],
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.rename_runs:
        renames = rename_runs(args.outdir, dry_run=args.dry_run)
        verb = "would rename" if args.dry_run else "renamed"
        for old, new in renames:
            print(f"{verb}: {old.name}  ->  {new.name}")
        print(f"[yttool] {verb} {len(renames)} run director{'y' if len(renames) == 1 else 'ies'} under {args.outdir}")
        return
    if not args.query:
        parser.error("the following arguments are required: TOPIC_OR_URL")
    args.concurrency = max(1, min(3, args.concurrency))
    cache = Cache(args.cache_dir)

    force_search = args.force_refresh or args.refresh_search
    filters = build_search_filters(args)
    tokens = args.query
    args.query = " ".join(tokens)
    video_ids = [extract_video_id(token) for token in tokens]
    channel_url = extract_channel_url(tokens[0]) if len(tokens) == 1 else None
    if filters and filters.channel_url and (all(video_ids) or channel_url):
        raise SystemExit("ERROR: --channel combines with a search topic, not with video IDs or a channel positional.")
    try:
        if all(video_ids):
            if filters:
                log("note: search filters are ignored for explicitly specified videos")
            metas = [fetch_video_meta(vid, cache, force_refresh=force_search) for vid in video_ids]
            args.query = ", ".join(meta.title for meta in metas)
            args._slug = videos_slug([meta.title for meta in metas])
        elif any(video_ids):
            raise SystemExit(
                "ERROR: got a mix of video URLs/IDs and search terms. "
                "Pass either only videos or only a search topic."
            )
        elif channel_url:
            metas = channel_latest(channel_url, args.top, cache, force_refresh=force_search, filters=filters)
            channel_name = metas[0].channel or tokens[0]
            args.query = f"latest videos from {channel_name}"
            args._slug = channel_slug(metas[0].channel, fallback=tokens[0])
        elif args.no_smart_search:
            metas = search(args.query, args.top, args.lang, cache, force_refresh=force_search, filters=filters)
        else:
            prompts, _ = load_prompts(args.prompt_dir, output_lang=args.output_lang)
            llm_config = build_llm_config(args.model, args.provider, args.config, args.azure)
            client = create_client(llm_config)
            metas = smart_search(
                args.query, args.top, args.lang, cache, client, llm_config, prompts,
                api_mode=args.api_mode, force_refresh=force_search, filters=filters,
            )
    except SearchError as exc:
        raise SystemExit(f"ERROR: {exc}")

    results = [VideoResult(meta=meta, index=i) for i, meta in enumerate(metas, 1)]
    log(f"processing {len(results)} videos for {args.query!r}")
    for result in results:
        line = f"  {result.index:>2}. {result.meta.title} ({video_prefix(result.index)}, {result.meta.video_id})"
        if result.meta.why:
            line += f"\n      -> {result.meta.why}"
        log(line)

    if args.dry_run:
        return

    if args.diarize:
        if args.no_whisper:
            raise SystemExit("ERROR: --diarize and --no-whisper are contradictory (diarization IS the ASR path).")
        # Captions carry no speaker information; every video goes through Gemini ASR.
        for result in results:
            cached = None if args.force_refresh else load_cached_asr(cache, result.meta.video_id, args.lang, diarized=True)
            if cached:
                result.segments, result.transcript_source = cached
                result.status = VideoStatus.OK_WHISPER
            else:
                result.status = VideoStatus.SKIPPED_NO_TRANSCRIPT
        run_asr_fallback(results, args, cache)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(fetch_one_transcript, result, args, cache, first=(i == 0))
                for i, result in enumerate(results)
            ]
            for future in futures:
                future.result()

        if not args.no_whisper:
            run_asr_fallback(results, args, cache)

    with_transcript = [r for r in results if r.has_transcript]
    log(f"transcripts: {len(with_transcript)}/{len(results)} videos")

    if not args.no_comments and args.comments > 0:
        fetch_all_comments(results, args, cache)

    if args.transcripts_only:
        run_dir = write_outputs(results, args)
        print(render_status_table(results))
        print(f"\nRun output: {run_dir}/ (transcripts/ + run.json, no summaries)")
        return

    if not with_transcript:
        print(render_status_table(results))
        raise SystemExit("ERROR: no transcripts could be fetched for any video")

    summarize_all(results, args, cache)
    run_dir = write_outputs(results, args)

    print()
    print(render_status_table(results))
    if THROTTLE.tripped:
        deferred = sum(1 for r in results if r.status == VideoStatus.DEFERRED)
        print(
            f"\n!! Rate-limit circuit breaker tripped: {deferred} video(s) deferred to protect the cooldown.\n"
            f"   Re-run the same command in 30-60 minutes - everything completed so far is cached."
        )
    print()
    print(f"Run output: {run_dir}/")
    print("  report.md      cross-video synthesis")
    if (run_dir / "mindmap.html").exists():
        print("  mindmap.html   interactive mindmap (open in a browser)")
    print("  videos/        per-video summaries")
    print("  transcripts/   full transcripts with clickable timestamps")
    if (run_dir / "comments").exists():
        print("  comments/      top comment threads per video")


if __name__ == "__main__":
    main()
