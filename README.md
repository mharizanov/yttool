# yttool

YouTube research CLI: give it a topic, specific videos, or a channel — it finds the most relevant videos, fetches transcripts and comment threads, and produces LLM-written knowledge extracts: per-video summaries, a cross-video synthesis report, and an interactive mindmap, all deep-linked back to the exact moments on YouTube.

```sh
yttool "urban beekeeping basics" --top 10        # topic research (LLM-planned search)
yttool https://youtu.be/VIDEO_ID OTHER_VIDEO_ID  # specific video(s) by URL or ID
yttool @channelhandle --top 10                   # a channel's latest N uploads
yttool VIDEO_URL --diarize --output-lang en      # speaker-attributed, in English
yttool "hive inspection" --channel @channelhandle # keywords within ONE channel
yttool "topic" --since year --min-duration 20m   # time window + length filters
```

## Why this exists

**The problem.** A large share of practical knowledge — conference talks, expert interviews, practitioner reviews, firsthand experience — lives only in YouTube videos, where it is slow to consume and impossible to skim, search, or cite. Researching a topic properly means watching hours of video of very uneven quality, and even then you can't easily compare what different videos claim or jump back to the moment a claim was made. yttool turns that into a readable, verifiable knowledge extract: it finds the relevant videos, reads them (and their comment sections) for you, tells you what they collectively say, where they disagree, which ones are actually worth your time — and every claim links to the exact second it was said.

**Does anything like it exist?** Honestly: the pieces do, the whole doesn't (as of Oct 2026):

- [Fabric](https://github.com/danielmiessler/Fabric) (~43k★) and [steipete/summarize](https://github.com/steipete/summarize) (~6k★) are excellent, well-maintained *single-URL* summarizers — you give them a video, they summarize it. Neither searches, batches by topic, synthesizes across videos, mines comments, or anchors claims to timestamps.
- [NotebookLM](https://notebooklm.google.com) (now Gemini Notebook) is good at answering questions about videos you add to a notebook, but its research features search the web and Google Drive, not YouTube; it ignores comments, and it cannot import a video without captions.
- The few projects matching the full search→batch→synthesize loop are single-digit-star, effectively unmaintained hobby repos; [yt-fts](https://github.com/NotJoeMartinez/yt-fts) (transcript full-text search, 1.8k★) was declared abandoned by its maintainer.
- Nothing found does the parts this tool leans on hardest: LLM-planned multi-query search for compound topics, upvote-weighted comment mining, speaker diarization with real names, cross-video disagreement analysis, or code-verified timestamp/comment citations (LLMs asked for timestamps directly will fabricate them).

So: if you just want one video summarized, Fabric is a fine choice. If you want *research* — a defensible answer assembled from many videos plus their communities — that's the gap this tool fills.

**Use cases.**

- **Topic research** — `yttool "sourdough starter maintenance"` → what the expert channels agree on, where they conflict, what people report in comments that the videos omit
- **Purchase / decision due diligence** — `yttool "robot lawn mower long-term owner experiences, not launch coverage" --top 20` → individual opinions synthesized into a ranked watch list and a themes/disagreements report
- **Deep-reading one long video** — `yttool <podcast-url> --diarize` → speaker-attributed transcript and summary of a 2-hour interview, with who-claimed-what
- **Following a creator** — `yttool @channel --top 10` → digest of a channel's latest uploads
- **Cross-language research** — `--output-lang en` reads Bulgarian/Russian/any-language videos and reports in your language; Gemini ASR handles caption-less ones
- **Feeding another LLM** — the `transcripts/` files are deliberately lean (no per-line URLs) for ingestion into downstream pipelines

## Requirements & install

- Python ≥ 3.11, macOS/Linux (no ffmpeg needed — audio is handled via yt-dlp + PyAV)
- A **residential internet connection** (YouTube blocks cloud/VPN IPs — see *Rate-limit protection*)
- An **OpenAI-compatible API key** for summarization, and optionally a free **Gemini API key** for ASR/diarization

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pip install -e '.[gemini]'    # optional: Gemini ASR + diarization (recommended)
.venv/bin/pip install -e '.[whisper]'   # optional: fully-local ASR fallback (~200 MB, CPU)
.venv/bin/pip install -e '.[dev]'       # optional: pytest + ruff
```

Keep `yt-dlp` fresh — a stale build is the #1 cause of download errors:
`pip install -U --pre "yt-dlp[default]"`

## Configuration

Config file search order: `--config PATH` → `$YTTOOL_CONFIG` → `./config.json` → `~/.config/yttool/config.json`. See `config.json.example`. Values like `"$OPENAI_API_KEY"` are resolved from the environment.

```json
{
  "model": "openai/gpt-5.6-luna",
  "apiKey": "$OPENAI_API_KEY",
  "geminiApiKey": "$GEMINI_API_KEY"
}
```

- **Summarization LLM**: any OpenAI-compatible endpoint via the `openai` SDK. Without a config file, `OPENAI_API_KEY` (plus optional `OPENAI_BASE_URL`, `YTTOOL_MODEL`) suffices. Azure: `--azure` or a model id like `azure/<deployment>`. Model ids use `provider/model` form; override per run with `--model`.
- **Gemini** (ASR + diarization): free key from [aistudio.google.com](https://aistudio.google.com) in `GEMINI_API_KEY`/`GOOGLE_API_KEY` or `geminiApiKey` in config. Free-tier limits (1,500 req/day) far exceed normal use; an hour of audio fits one request.

## What a run does

1. **Smart search** (topic mode): the LLM plans 3–5 practical search queries — decomposing compound topics, using vocabulary that appears in real video titles — runs them all via `yt-dlp ytsearch` (no YouTube API key or quota), merges/dedupes ~40–60 candidates, and a second LLM call selects the most relevant `--top` N from metadata, preferring substantive content over shorts/ads and recording a per-pick reason in `run.json`. Phrase intent directly in the topic (e.g. *"personal experiences, NOT news coverage"*) — both planner and selector honor it. `--no-smart-search` searches the topic literally (works without any API key). Direct-video and channel modes skip search entirely.
2. **Transcripts** — four tiers per video, first success wins:
   - `youtube-transcript-api` (fast, no subprocess)
   - yt-dlp subtitle download (VTT with rolling-caption dedup; requests exact language codes plus the original-language `*-orig` auto track — never auto-translated tracks, which trigger hard rate limits)
   - **Gemini URL ingestion** — the YouTube URL is passed straight to the Gemini API and Google fetches the video server-side, so this tier consumes *zero* of our YouTube request budget and keeps working even while our IP is rate-limited. Uses its own model (`--gemini-url-model`, default flash-lite) because free-tier quotas are per model; quota exhaustion disables the tier for the run. `--no-gemini-url` to opt out.
   - **Audio-download ASR** as last resort: `--transcriber gemini` (upload to the API) or `whisper` (local faster-whisper, offline); `auto` (default) prefers Gemini when a key is configured
   - Both caption tiers fall back to the video's original language when `--lang` has no track. Caption entries are coalesced into ~250-char segments for better timestamp anchors.
3. **Comments** — top `--comments` N threads per video (default 30, sorted by top). When the subtitle tier runs anyway, comments piggyback on the same yt-dlp invocation.
4. **Per-video summaries** — chunked map-reduce per video: thematic sections, notable claims with stance (asserted/opinion/speculative), themes, **quotable lines**, a 1–5 watch verdict, and mindmap nodes. Quotable lines are the sentences a writer would lift: memorable, self-contained, carrying a real claim. They are extracted near-verbatim at the chunk level (where the raw transcript is visible), then **checked by code against the transcript** around the cited segment — a reworded or invented line is dropped, and the anchor is moved to the segment that matched best, so every quote links to the moment it is spoken. Attribution uses the speaker label when diarized, else the channel name. Plus **Community insights**: an LLM pass distills only knowledge-bearing comments (corrections, firsthand experience, counterpoints, uploader clarifications, tips), ranked with upvotes as the community's relevance vote, each linked to the comment itself (`&lc=` permalink). Authors/likes come from captured data, never from the LLM.
5. **Cross-video synthesis** (2+ summarized videos): overview, ranked *what to actually watch*, common themes, genuine disagreements between videos, **Lines worth quoting** — up to 10 of the verified per-video quotes curated across the run for someone writing about the topic (the LLM only selects by quote id; it cannot write or alter quote text) — and **What the comments add** — cross-video patterns in viewer comments (recurring experiences, echoed corrections, consistent pushback), each referencing validated comment permalinks.
6. **Mindmap** — one combined Markmap organized by concept (not by video); single-video runs build it from that video's own nodes. Nodes carry timestamp deep links and a hover **TL;DR balloon** with depth-matched abstraction (broad at the trunk, specific at the leaves). The toolbar's dark-theme toggle themes the whole page.

**Quote integrity**: the LLM is asked for near-verbatim lines (filler dropped, punctuation added, nothing reworded) and code verifies each one: at least 70% of its content words must occur in the cited transcript segment or its neighbours. Quotes stay in the speaker's language even under `--output-lang`; only the attached "why" note is translated.

**Timestamp integrity**: the LLM never computes timestamps. Transcript lines carry stable source IDs (`[S0007 00:01:23] …`); the LLM cites IDs, and code resolves them into `youtube.com/watch?v=ID&t=NNs` links (cross-video citations use `V02.S0007` prefixes). Comment citations are likewise validated against captured comment IDs — invented references are dropped.

## Speaker identification

`--diarize` skips captions (they carry no speaker data) and transcribes every video with Gemini — via URL ingestion when available, else audio upload — using speaker labels — **real names when inferable from the conversation** (introductions, being addressed), else `Speaker 1/2`. Transcripts become `[MM:SS] Name: text`, and summaries attribute claims to speakers. Needs the Gemini transcriber; costs an audio download per video, so best for interviews/podcasts where who-said-what matters. Diarized and plain transcripts cache separately.

## Output language

`--output-lang LANG` (e.g. `en`, `Bulgarian`) forces **all** generated text — summaries, report, mindmap labels and TL;DRs, community insights — into that language regardless of the videos' languages. Proper names and technical terms are kept as-is. Caches are language-specific, so variants coexist. Default: follow the transcript's language.

## Output layout

Each run writes `runs/<slug>-<timestamp>/`, where the slug says what the run is about: the search topic, `channel-<name>` for a channel digest, or the video title(s) for direct-video runs — `video-<title>` for one, `videos-<title-1>+<title-2>[+N-more]` for several (a series with a shared title prefix collapses to `videos-<prefix>-<part-1>+<part-2>`). Non-Latin titles keep their script. `yttool --rename-runs [--dry-run]` renames older run directories to the current scheme, recomputing each slug from its `run.json`.


| File | Contents |
|------|----------|
| `report.md` | cross-video synthesis (incl. Lines worth quoting) + status table of every video (including failures and why) |
| `mindmap.html` / `mindmap.md` | interactive mindmap: clickable timestamps, hover TL;DRs, dark theme |
| `videos/NN-<id>.md` | per-video summary with deep links, Quotable lines + Community insights |
| `transcripts/NN-<id>.md` | full transcript, `[MM:SS]` per segment (single header URL + `&t=` recipe — no per-line URLs, keeping files lean for LLM ingestion) |
| `comments/NN-<id>.md` | top comment threads, replies nested, with author/likes/uploader badges |
| `run.json` | machine-readable: statuses, transcript sources, selection reasons, errors |

## Caching

Everything cacheable lives in `~/.cache/yttool/` (`--cache-dir` to move):

- search plans/selections (24 h), channel listings (24 h), video metadata (7 d)
- transcripts: permanent per video+language (diarized separately); "no captions" misses expire in 6 h; transient rate-limit blocks are never cached
- per-video summaries: keyed by model + prompts + transcript content + output language — re-runs never re-hit YouTube or re-pay the LLM for anything unchanged; editing prompts or switching models/languages invalidates exactly what it should
- comment threads and comment insights, similarly keyed

`--force-refresh` bypasses all caches; `--refresh-search` re-runs only the search.

## Rate-limit protection

YouTube throttles per endpoint per IP over a rolling window. The tool is built to stay under it:

- a **global pacer** enforces a ~4–5 s minimum gap between *all* YouTube requests, across every thread and subprocess
- **block-aware short-circuits**: 429/403 responses stop client rotation (rotation can't fix IP throttling — it only helps video-specific issues like the tv client's anonymous-DRM behavior); a blocked transcript API is skipped for the rest of the run
- a **circuit breaker**: after 3 consecutive blocked videos, remaining work is marked `deferred`, the run finishes cleanly with what succeeded, and it tells you to re-run after a cooldown (everything done is cached, so the retry only fetches what's missing)

If you still see `blocked` statuses: you're on a VPN/cloud IP, or a heavy day tripped the rolling window — wait 30–60 min. Expect a few videos per batch to fail regardless (members-only, age-restricted, comments disabled); the batch never aborts, and every video ends with an explicit status in `report.md`.

## Flag reference

| Flag | Default | Meaning |
|------|---------|---------|
| `--top N` | 10 | number of videos to process |
| `--channel` | — | restrict a keyword search to one channel (@handle / URL / UC... id) |
| `--since` | — | `hour\|day\|week\|month\|year` (native, free) or `YYYY-MM-DD` (precise; capped per-candidate metadata fetches) |
| `--min-duration` / `--max-duration` | — | length range: `90s`, `10m`, `1h30m`, bare number = minutes (native short/long buckets pre-narrow, post-filter is exact) |
| `--lang` | `en` | preferred transcript language (falls back to the video's original) |
| `--output-lang LANG` | follow transcript | force output language |
| `--diarize` | off | speaker-attributed Gemini transcription (skips captions) |
| `--transcriber` | `auto` | ASR backend: `gemini` / `whisper` / `auto` |
| `--gemini-model` | `gemini-flash-latest` | Gemini model for ASR |
| `--whisper-model` | `small` | faster-whisper model (cpu/int8) |
| `--no-whisper` | off | skip the ASR tier entirely |
| `--comments N` / `--no-comments` | 30 | comment threads per video / skip |
| `--model`, `--provider`, `--azure`, `--api-mode` | from config | summarization LLM selection |
| `--chunk-chars` | 12000 | transcript chars per LLM chunk |
| `--max-output-tokens` | 10000 | per-call output budget (merge/synthesis get ≥16k automatically) |
| `--llm-concurrency` | 4 | parallel per-video summarizations |
| `--concurrency` | 2 (max 3) | parallel transcript fetches |
| `--player-client` | `default` | yt-dlp YouTube client (auto-falls back to `android_vr`, `default`; note `tv` serves only DRM to anonymous requests) |
| `--dry-run` | — | print selected videos and stop |
| `--transcripts-only` | — | stop after transcripts (still writes the run dir) |
| `--force-refresh` / `--refresh-search` | — | cache bypass (all / search only) |
| `--outdir`, `--cache-dir` | `./runs`, `~/.cache/yttool` | locations |
| `--prompt-dir` | built-in | override the prompt files |
| `--keep-audio` | off | keep downloaded audio in cache |
| `--no-smart-search` | off | literal search, no LLM planning (keyless) |

## Development

```sh
.venv/bin/pytest        # fully offline: VTT fixtures, mocked LLM, no network
.venv/bin/ruff check src tests
```

Prompts live in `src/yttool/prompts/*.md` and can be overridden with `--prompt-dir`; per-video prompt edits auto-invalidate summary caches via content hashing. The transcript→LLM core (config layer, chunking, source-ID anchoring, JSON salvage, Markmap output) originated in an earlier transcription tool of mine and was hardened here with retries, caching, concurrency, and rate-limit protection — improvements since backported.
