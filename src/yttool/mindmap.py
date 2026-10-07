"""Mindmap normalization and Markmap HTML output, vendored from an earlier transcription tool.

Changes from the original:
  - source-id regex widened to accept the cross-video form V01.S0007
  - deterministic timestamps become clickable YouTube deep links in the
    Markmap markdown instead of backtick-quoted times
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .chunking import clean_text, fmt_mmss

SOURCE_ID_RE = re.compile(r"(?:V\d{2,}\.)?S\d{4,}")

# resolver(source_ids) -> (start_seconds, url) or None
LinkResolver = Callable[[list[str]], "tuple[float, str] | None"]


def clean_label(value: Any, max_len: int = 180) -> str:
    text = clean_text(str(value or ""))
    text = text.replace("|", "-")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "..."
    return text


def normalize_source_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [str(item) for item in value]
    else:
        return []
    source_ids: list[str] = []
    for item in values:
        for match in SOURCE_ID_RE.finditer(item):
            source_id = match.group(0)
            if source_id not in source_ids:
                source_ids.append(source_id)
    return source_ids


def normalize_nodes(nodes: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(nodes, list):
        return normalized
    for node in nodes:
        tldr = ""
        if isinstance(node, str):
            label = clean_label(node)
            children = []
            source_ids: list[str] = []
        elif isinstance(node, dict):
            label = clean_label(node.get("label") or node.get("title") or node.get("name") or "")
            children = normalize_nodes(node.get("children") or [])
            source_ids = normalize_source_ids(node.get("source_ids") or node.get("source_id") or [])
            tldr = clean_text(str(node.get("tldr") or ""))[:300]
        else:
            continue
        if label:
            item: dict[str, Any] = {"label": label, "children": children}
            if source_ids:
                item["source_ids"] = source_ids
            if tldr:
                item["tldr"] = tldr
            normalized.append(item)
    return normalized


def normalize_mindmap(data: dict[str, Any], default_title: str = "Mindmap") -> dict[str, Any]:
    title = clean_label(data.get("title") or default_title)
    summary = clean_label(data.get("summary") or "")
    nodes = data.get("nodes") or data.get("children") or []
    return {"title": title, "summary": summary, "nodes": normalize_nodes(nodes)}


def inject_deep_links(mindmap: dict[str, Any], resolver: LinkResolver) -> None:
    """Resolve each node's source_ids to a (seconds, url) deep link, stored on
    the node. Links are always produced by code, never by the LLM."""

    def apply_node(node: dict[str, Any]) -> tuple[float, str] | None:
        child_links: list[tuple[float, str]] = []
        for child in node.get("children") or []:
            link = apply_node(child)
            if link is not None:
                child_links.append(link)

        link = resolver(node.get("source_ids") or [])
        if link is None and child_links:
            link = min(child_links, key=lambda item: item[0])
        if link is not None:
            node["link"] = {"seconds": link[0], "url": link[1]}
        else:
            node.pop("link", None)
        return link

    for node in mindmap.get("nodes") or []:
        apply_node(node)


def collect_tooltips(mindmap: dict[str, Any]) -> dict[str, str]:
    """Map node label -> tldr for the hover-tooltip layer (first label wins)."""
    tips: dict[str, str] = {}

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            label = node.get("label")
            if label and node.get("tldr") and label not in tips:
                tips[label] = node["tldr"]
            walk(node.get("children") or [])

    walk(mindmap.get("nodes") or [])
    return tips


def markdown_text(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if text.startswith(("-", "*", "+", "#")):
        text = "\\" + text
    return text


def append_markdown_node(lines: list[str], node: dict[str, Any], depth: int) -> None:
    indent = "  " * depth
    label = markdown_text(node.get("label") or "Untitled")
    link = node.get("link")
    if isinstance(link, dict) and link.get("url"):
        label = f"{label} [{fmt_mmss(link.get('seconds'))}]({link['url']})"
    lines.append(f"{indent}- {label}")
    for child in node.get("children") or []:
        append_markdown_node(lines, child, depth + 1)


def mindmap_to_markdown(mindmap: dict[str, Any]) -> str:
    lines = [f"# {markdown_text(mindmap.get('title') or 'Mindmap')}", ""]
    summary = clean_label(mindmap.get("summary") or "")
    if summary:
        lines.append(f"- Summary: {markdown_text(summary)}")
    for node in mindmap.get("nodes") or []:
        append_markdown_node(lines, node, 0)
    lines.append("")
    return "\n".join(lines)


def write_html(markdown: str, title: str, path: Path, tooltips: dict[str, str] | None = None) -> None:
    safe_markdown = markdown.replace("</script>", "<\\/script>")
    tips_json = json.dumps(tooltips or {}, ensure_ascii=False).replace("</", "<\\/")
    template = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(title)}</title>
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; font-family: Segoe UI, Arial, sans-serif;
                  background: #ffffff; color: #111827; }}
    header {{ box-sizing: border-box; padding: 12px 16px; border-bottom: 1px solid #ddd; }}
    header h1 {{ margin: 0; font-size: 18px; }}
    header p {{ margin: 4px 0 0; color: #555; font-size: 13px; }}
    /* The markmap toolbar's dark toggle sets .markmap-dark on <html> but only
       restyles the SVG; follow it for the page chrome too. */
    html.markmap-dark, html.markmap-dark body {{ background: #111827; color: #e5e7eb; }}
    html.markmap-dark header {{ border-bottom-color: #374151; }}
    html.markmap-dark header p {{ color: #9ca3af; }}
    .markmap {{ position: relative; width: 100%; height: calc(100vh - 70px); }}
    .markmap > svg {{ width: 100%; height: 100%; }}
    #node-tip {{
      position: fixed; display: none; z-index: 10; max-width: 340px;
      background: #1f2937; color: #f9fafb; padding: 8px 12px; border-radius: 8px;
      font-size: 13px; line-height: 1.45; box-shadow: 0 4px 14px rgba(0,0,0,.25);
      pointer-events: none;
    }}
  </style>
  <script>
    window.markmap = {{ autoLoader: {{ toolbar: true }} }};
  </script>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>Interactive mindmap generated from YouTube transcripts. Timestamps link to the exact moment on YouTube. Hover a node for its TL;DR.</p>
  </header>
  <div class=\"markmap\">
    <script type=\"text/template\">
{safe_markdown}
    </script>
  </div>
  <div id=\"node-tip\"></div>
  <script src=\"https://cdn.jsdelivr.net/npm/markmap-autoloader@latest\"></script>
  <script>
    (function () {{
      var TIPS = {tips_json};
      // longest-first so a label that is a prefix of another cannot shadow it
      var LABELS = Object.keys(TIPS).sort(function (a, b) {{ return b.length - a.length; }});
      var tipEl = document.getElementById('node-tip');

      function tipFor(el) {{
        var text = (el.textContent || '').trim();
        for (var i = 0; i < LABELS.length; i++) {{
          if (text.indexOf(LABELS[i]) === 0) return TIPS[LABELS[i]];
        }}
        return null;
      }}

      function wire() {{
        document.querySelectorAll('.markmap foreignObject > div, .markmap foreignObject > span').forEach(function (el) {{
          if (el.dataset.tipWired) return;
          el.dataset.tipWired = '1';
          var tip = tipFor(el);
          if (!tip) return;
          el.addEventListener('mouseenter', function () {{
            tipEl.textContent = tip;
            tipEl.style.display = 'block';
          }});
          el.addEventListener('mousemove', function (ev) {{
            var x = Math.min(ev.clientX + 14, window.innerWidth - 360);
            var y = Math.min(ev.clientY + 16, window.innerHeight - 90);
            tipEl.style.left = x + 'px';
            tipEl.style.top = y + 'px';
          }});
          el.addEventListener('mouseleave', function () {{
            tipEl.style.display = 'none';
          }});
        }});
      }}

      window.addEventListener('load', function () {{
        setTimeout(wire, 600);
        var host = document.querySelector('.markmap');
        if (host) new MutationObserver(wire).observe(host, {{ childList: true, subtree: true }});
      }});
    }})();
  </script>
</body>
</html>
"""
    path.write_text(template, encoding="utf-8")
