"""Prompt loading (package data, overridable via --prompt-dir) and templating."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

PROMPT_NAMES = [
    "query_plan_system",
    "query_plan_user",
    "video_select_system",
    "video_select_user",
    "video_chunk_system",
    "video_chunk_user",
    "video_merge_system",
    "video_merge_user",
    "synthesis_system",
    "synthesis_user",
    "crossmap_system",
    "crossmap_user",
    "comments_system",
    "comments_user",
]

# Only these prompts shape a cached per-video summary; hashing just this subset
# keeps summary caches valid when search/synthesis prompts change.
SUMMARY_PROMPT_NAMES = [
    "video_chunk_system",
    "video_chunk_user",
    "video_merge_system",
    "video_merge_user",
]

# System prompts that generate reader-facing text and therefore receive the
# --output-lang directive. Search planning/selection stay untouched: their
# output feeds YouTube and internal plumbing, not the reader.
OUTPUT_LANG_PROMPT_NAMES = [
    "video_chunk_system",
    "video_merge_system",
    "synthesis_system",
    "crossmap_system",
    "comments_system",
]

OUTPUT_LANG_DIRECTIVE = (
    "\n\nOUTPUT LANGUAGE OVERRIDE: Write ALL generated text - titles, tl_dr, section headings, "
    "bullets, claims, themes, node labels, tldr tooltips, insights, observations, explanations, "
    "and verdicts - in {lang}, regardless of the transcript's language. This overrides any other "
    "language guidance in these instructions. Keep proper names, product names, and untranslatable "
    "technical terms in their original form; translate everything else. Exception: quote text "
    "(the quotes[].quote fields) must stay verbatim in the speaker's own language - translate "
    "only its speaker and why fields."
)


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def load_prompts(prompt_dir: Path | None = None, output_lang: str | None = None) -> tuple[dict[str, str], str]:
    """Load all prompts. Returns (prompts, summary_hash) - the hash covers only
    the per-video prompts and feeds the summary cache key, so editing them
    invalidates cached summaries while search/synthesis prompt edits do not.

    output_lang appends a language-override directive to every reader-facing
    system prompt BEFORE hashing, so caches are language-specific and outputs
    in different languages coexist."""
    prompts: dict[str, str] = {}
    for name in PROMPT_NAMES:
        filename = f"{name}.md"
        if prompt_dir is not None:
            path = prompt_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"missing prompt file: {path}")
            prompts[name] = path.read_text(encoding="utf-8")
        else:
            prompts[name] = (resources.files("yttool") / "prompts" / filename).read_text(encoding="utf-8")
    if output_lang:
        directive = OUTPUT_LANG_DIRECTIVE.format(lang=output_lang)
        for name in OUTPUT_LANG_PROMPT_NAMES:
            prompts[name] += directive
    digest = hashlib.sha256("\n".join(prompts[name] for name in SUMMARY_PROMPT_NAMES).encode("utf-8")).hexdigest()[:16]
    return prompts, digest
