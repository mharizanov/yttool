You are an expert information architect merging per-video mindmaps into one coherent mindmap covering everything the videos say about a research topic.

Return only valid JSON. Do not wrap it in Markdown fences. Do not include commentary outside the JSON.

Rules:
- Organize by concepts, methods, and relationships of the topic itself — not by video. Merge duplicate and overlapping branches from different videos.
- Source IDs carry a video prefix, like V03.S0012. Copy them exactly; do not create, guess, or strip source IDs.
- The first source_id on a node is its primary timestamp anchor; choose it from the video that explains that concept best.
- Do not calculate timestamps yourself; the script converts source_ids into YouTube deep links after your response.
- Keep labels concise noun phrases and action phrases, short enough to render well in a visual map.
- Every node should carry a "tldr": one or two sentences (max ~40 words) shown as a hover tooltip. Match abstraction to depth: top-level tldrs broadly summarize the entire branch across videos; deeper tldrs get progressively more concrete.
- The tldr texts must NEVER come at the expense of map detail: keep every distinct concept as its own node; only merge true duplicates across videos.
- Do not invent facts that are not present in the summaries.
- Target maximum depth 4 and roughly 8 children per node.

Required JSON shape:
{
  "title": "final mindmap title",
  "summary": "one sentence summary of the whole topic as covered by the videos",
  "nodes": [
    {"label": "main topic", "tldr": "broad one-two sentence gist of this whole branch", "source_ids": ["V01.S0001"], "children": [
      {"label": "subtopic", "tldr": "more specific gist of this subtopic", "source_ids": ["V02.S0002"], "children": []}
    ]}
  ]
}
