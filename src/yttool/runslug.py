"""Human-readable run-directory names.

A run directory is ``<slug>-<YYYYmmdd-HHMMSS>``. The slug should say what the
run is *about* so the ``runs/`` listing is skimmable: search topics use the
query text, channel runs the channel name, and direct-video runs the video
title(s) instead of opaque video IDs. ``slug_from_run_meta`` recomputes the
slug from a run's ``run.json`` so old directories can be renamed to the
current scheme.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

SLUG_MAX = 40          # topic / channel / single-title slugs
MULTI_PART_MAX = 24    # each title in a multi-video slug
MULTI_TOTAL_MAX = 80   # whole multi-video slug
MULTI_SHOWN = 3        # titles listed before "+N-more"

RUN_DIR_RE = re.compile(r"^(?P<slug>.+)-(?P<stamp>\d{8}-\d{6})$")
_HANDLE_RE = re.compile(r"@([A-Za-z0-9._-]+)")


def slugify(text: str, max_len: int = SLUG_MAX) -> str:
    """Lowercase, keep letters/digits of any script (so Cyrillic titles survive),
    collapse everything else to single dashes, truncate on a word boundary."""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[\u2019'`]", "", text)  # "Didn't" -> "didnt", not "didn-t"
    slug = re.sub(r"[^\w]+|_+", "-", text).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        if slug[max_len] != "-" and "-" in cut:  # mid-word: drop the partial word
            cut = cut.rsplit("-", 1)[0]
        slug = cut
    return slug.strip("-") or "query"


def _common_word_prefix(slugs: list[str]) -> str:
    words = [s.split("-") for s in slugs]
    prefix: list[str] = []
    for column in zip(*words):
        if len(set(column)) != 1:
            break
        prefix.append(column[0])
    return "-".join(prefix)


def videos_slug(titles: list[str]) -> str:
    """``video-<title>`` for one video; ``videos-<t1>+<t2>[+N-more]`` for several.
    Series with a shared title prefix ("X - Part 1", "X - Part 2") collapse
    to ``videos-<X>-<part-1>+<part-2>`` so the distinguishing tails are kept."""
    titles = [t for t in titles if t and t.strip()]
    if not titles:
        return "videos"
    if len(titles) == 1:
        return "video-" + slugify(titles[0])

    shown = titles[:MULTI_SHOWN]
    slugs = [slugify(t, max_len=200) for t in shown]
    prefix = _common_word_prefix(slugs)
    if len(prefix) >= 12 and all(len(s) > len(prefix) + 1 for s in slugs):
        tails = [slugify(s[len(prefix) + 1:], max_len=MULTI_PART_MAX) for s in slugs]
        body = slugify(prefix, max_len=SLUG_MAX) + "-" + "+".join(tails)
    else:
        body = "+".join(slugify(s, max_len=MULTI_PART_MAX) for s in slugs)
    if len(titles) > MULTI_SHOWN:
        body += f"+{len(titles) - MULTI_SHOWN}-more"
    if len(body) > MULTI_TOTAL_MAX:
        body = body[:MULTI_TOTAL_MAX].rstrip("-+")
    return "videos-" + body


def channel_slug(channel_name: str, fallback: str = "") -> str:
    name = (channel_name or "").strip()
    if not name:
        match = _HANDLE_RE.search(fallback)
        name = match.group(1) if match else fallback
    return "channel-" + slugify(name)


def slug_from_run_meta(meta: dict) -> str:
    """Recompute the slug a run *would* get today from its ``run.json``."""
    videos = meta.get("videos") or []
    found_by = {v.get("found_by") for v in videos}
    query = meta.get("query") or ""
    if videos and found_by == {"direct"}:
        return videos_slug([v.get("title") or "" for v in videos])
    if videos and found_by == {"channel"}:
        names = [v.get("channel") for v in videos if v.get("channel")]
        return channel_slug(names[0] if names else "", fallback=query.removeprefix("latest videos from ").strip())
    return slugify(query)


def rename_runs(outdir: Path, dry_run: bool = False) -> list[tuple[Path, Path]]:
    """Rename every ``<slug>-<stamp>`` directory under ``outdir`` whose slug
    differs from the current scheme. Returns the (old, new) pairs applied
    (or that would be applied with ``dry_run``). Collisions are skipped."""
    renames: list[tuple[Path, Path]] = []
    if not outdir.is_dir():
        return renames
    for run_dir in sorted(outdir.iterdir()):
        match = RUN_DIR_RE.match(run_dir.name)
        if not run_dir.is_dir() or not match:
            continue
        run_json = run_dir / "run.json"
        if not run_json.is_file():
            continue
        try:
            meta = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        new_slug = slug_from_run_meta(meta)
        if new_slug == match.group("slug"):
            continue
        target = run_dir.with_name(f"{new_slug}-{match.group('stamp')}")
        if target.exists():
            continue
        renames.append((run_dir, target))
        if not dry_run:
            run_dir.rename(target)
    return renames


def run_dir_name(slug: str, when: datetime | None = None) -> str:
    return f"{slug}-{(when or datetime.now()).strftime('%Y%m%d-%H%M%S')}"
