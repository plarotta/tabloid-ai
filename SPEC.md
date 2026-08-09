# arXiv Paper Video Pipeline — Design & Implementation Spec

> Owner-supplied spec. Source of truth for design.
> Recovered into the repo on 2026-08-07; it previously existed only in a chat transcript.
> **Amended 2026-08-09** by the owner to add Stage 10 (Upload). Everything else is
> as originally supplied; the §1 non-goal on publishing was updated to match.

Status: Greenfield. This document is the source of truth for design and implementation. Owner: Pedro Intended reader: Claude Code (agentic implementation). Read this fully before writing code. Where the spec says DECIDE, make a reasoned choice, document it in `DECISIONS.md`, and proceed. Where it says ASK, stop and ask the owner.
1. Goal
A fully automated pipeline that runs every Tuesday and Thursday and produces a 3–5 minute video covering the top 3 AI/ML papers uploaded to arXiv in the preceding ~3–4 days. Output is a rendered `.mp4` plus per-paper standalone segments, a thumbnail, and a title/description for YouTube.
Primary success criteria, in order:

1. Cost per episode ≤ $15 (target $6–10). Log actual spend per run.
2. Zero-touch operation — a run either completes or fails loudly with a clear error; no silent partial output.
3. Factual fidelity — narration claims must be traceable to the paper. No hallucinated results.
4. Watchable quality: real figures from the paper, clean narration, readable slides.

Explicit non-goals (v1): generative video (Veo/Sora-class), talking-head avatars, multi-language.

Publishing was originally a non-goal ("produce upload-ready assets; publishing stays manual for now"). **Superseded 2026-08-09 by Stage 10** — automated upload is now in scope, gated behind a YouTube API compliance audit. Manual upload via YouTube Studio remains the documented fallback while that audit is outstanding.
2. Architecture overview

```
[1] Fetch      arXiv API → new papers (cs.LG, cs.CL, cs.CV, cs.AI, stat.ML), last 3-4 days
[2] Shortlist  cheap LLM pass on title+abstract → ~15 candidates
[3] Enrich     pull LaTeX source (figures + captions), intro, conclusion; external signals
[4] Rank       frontier LLM ranks enriched candidates → top 3 (no intermediate top-5 stage)
[5] Extract    per paper: structured PaperDigest (claim, method, results, caveats, key figures)
[6] Script     per paper: SceneManifest (narration + visuals + timing), then episode intro/outro
[7] Voice      TTS per scene
[8] Render     programmatic video assembly per segment, then stitch episode
[9] Package    mp4 + segments + thumbnail + title/description + cost report
[10] Upload    YouTube Data API v3, scheduled publish + AI disclosure (audit-gated)

```

Each stage writes its output to disk (`runs/<date>/<stage>/`) so any stage can be re-run in isolation. This is a hard requirement — it is how we debug and how we cap costs during development.
3. Stage specs
Stage 1 — Fetch

* Use the arXiv API (`http://export.arxiv.org/api/query`), not scraping. Respect rate limits (1 request / 3 sec, batch with `max_results`).
* Categories: `cs.LG, cs.CL, cs.CV, cs.AI, stat.ML`. Window: submissions since the last successful run (fallback: 4 days). Dedupe cross-listed papers by arXiv ID.
* Expect 300–700 papers per window. Persist as JSONL: `{arxiv_id, title, abstract, authors, categories, submitted, updated}`.

Stage 2 — Shortlist (cheap committee)

* Model: a cheap/fast model (Haiku-class or Groq-hosted small model — DECIDE based on what API keys are configured; owner already uses Groq llama-3.1-8b-instant + qwen3-32b in a prior digest project and may want to reuse that pattern).
* Score each paper 0–10 on: novelty of claim, likely audience interest, likely visual assets (does the abstract imply strong figures/results?). Batch multiple abstracts per call to cut cost.
* Take top ~15. Log scores for all papers.

Stage 3 — Enrich
For each shortlisted paper:

* Download LaTeX source from arXiv (`/e-print/<id>`); this is a tar.gz for most papers. Extract figure files (pdf/png/eps) and their captions by parsing `\begin{figure}...\caption{}` blocks. Convert eps/pdf figures to png (e.g., `pdftoppm` / ImageMagick). If no source is available, fall back to PDF figure extraction (`pymupdf`), and mark quality lower.
* Extract intro and conclusion text from the source (or PDF fallback).
* External signals (all free): presence on Hugging Face Daily Papers (scrape the page for the date range), linked GitHub repo (regex the abstract/comments), author affiliations.
* Output per paper: `enriched/{arxiv_id}/` with `meta.json`, `sections.json`, `figures/` (files + `captions.json`), `signals.json`.

Stage 4 — Rank (frontier committee)

* Model: frontier-class (Sonnet-class default — DECIDE). Input per paper: abstract + intro + conclusion + figure captions + signals. Do NOT send full paper text at this stage.
* Rank for a video audience: prefer papers with a crisp claim, strong visual assets, and broad interest. Diversity constraint: avoid 3 papers from the same narrow subfield in one episode.
* Output: top 3 with a one-paragraph justification each (goes into the episode description later).

Stage 5 — Extract (PaperDigest)
For each of the 3 winners, one frontier-model pass over the full paper text producing this schema (validate with pydantic; retry once on validation failure, then ASK):

```json
{
  "arxiv_id": "...",
  "one_sentence_claim": "...",
  "problem_context": "2-3 sentences: what gap this fills",
  "method_summary": "3-5 sentences, plain language",
  "headline_results": [
    {"statement": "...", "source": "Table 2 / Figure 3", "numbers": "..."}
  ],
  "key_figures": [
    {"file": "figures/fig3.png", "caption": "...", "why_show": "..."}
  ],
  "honest_caveats": ["..."],
  "why_it_matters": "1-2 sentences"
}

```

* Fidelity rule: every entry in `headline_results` must cite a specific table/figure/section. The extraction prompt must instruct the model to only report numbers it can quote from the text.
* "Minimal lossless representation" of the whole paper is explicitly NOT the goal — this targeted schema is.

Stage 6 — Script (SceneManifest)
Per paper segment (target 60–90 seconds), generate:

```json
{
  "arxiv_id": "...",
  "scenes": [
    {
      "id": "s1",
      "narration": "spoken text, conversational register, no LaTeX, numbers spelled naturally",
      "visual": {
        "type": "title_card | figure | bullet_slide | result_callout",
        "figure_file": "optional",
        "title": "optional",
        "bullets": ["optional"],
        "highlight": "optional annotation text"
      },
      "est_seconds": 12
    }
  ]
}

```

* Narration constraints: ~150 words/minute budget; hook first sentence; end each segment with the caveat or "why it matters". No hype words ("groundbreaking", "revolutionary").
* Also generate: episode cold-open (10–15s teasing all 3 papers), transitions, outro, YouTube title (≤70 chars), description with arXiv links, and 3 thumbnail text options.
* Segments must be renderable standalone (for Shorts repurposing later): each opens with its own title card.

Stage 7 — Voice (TTS)

* Default: OpenAI TTS (`tts-1` or `gpt-4o-mini-tts`), ~$0.015/min — DECIDE model/voice; make provider pluggable behind an interface so ElevenLabs can be swapped in later.
* One audio file per scene. Measure actual durations; feed real durations back into the render timeline (do not trust `est_seconds`).

Stage 8 — Render

* DECIDE between Remotion (React, recommended if Node is acceptable) and a Python route (moviepy compositing rendered HTML/matplotlib slides). Criteria: template-ability, text layout quality, dev speed. Manim only if math animations become a priority later — overkill for v1.
* Visual system: dark background, single accent color, large type, paper figures displayed on light cards with source attribution ("Figure 3, <arxiv_id>") burned in. Keep it minimal — figures are the star.
* 1920×1080, 30fps, H.264. Sync scene boundaries to audio durations. Simple crossfade transitions. Optional light background music bed (royalty-free, ship one default track in-repo) mixed at low volume — DECIDE.
* Render per-paper segments, then concatenate: cold-open → seg1 → seg2 → seg3 → outro.

Stage 9 — Package
`runs/<date>/output/`: `episode.mp4`, `segment_<id>.mp4` ×3, `thumbnail.png` (generate programmatically from template + best figure; no image-gen API needed in v1), `metadata.json` (title, description, chapter timestamps), `cost_report.json`.
Stage 10 — Upload (YouTube)

* Use YouTube Data API v3 `videos.insert` with the **`youtube.upload` scope only** — the narrowest scope that does the job, which also makes the compliance audit easier. Google API Python client, resumable upload, exponential backoff on 5xx.
* **Policy constraint that gates this stage:** videos uploaded from unverified API projects (created after 2020-07-28) are **locked to `private`**. The Google Cloud project must pass a YouTube API compliance audit before public or scheduled publishing works. **This is owner action, not code:** create the project and OAuth consent screen, and submit the audit **during Phase 1** — lead time is ~2–4 weeks and it runs parallel to development.
* Upload flow: upload as `privacyStatus: private` with `publishAt` set to the configured publish time (e.g. 12:00 ET same day) → set the thumbnail via `thumbnails.set` → verify processing by polling `videos.list` until processed.
* Set `status.containsSyntheticMedia: true`. This is YouTube's AI-content disclosure and is **non-negotiable for this channel**.
* OAuth: a one-time interactive flow obtains a refresh token; store that refresh token as a secret (env var / GitHub Actions secret). The code must handle token refresh **non-interactively** thereafter.
* Dry-run mode (`upload.enabled: false` in config — the default until the audit clears): the stage validates metadata and logs what it *would* upload. The fallback path is manual upload of `episode.mp4` via YouTube Studio using `metadata.json`.
* Idempotency: record the returned video ID in `runs/<date>/output/upload.json`. A re-run must not double-upload.

4. Cost accounting (hard requirement)

* Wrap every LLM/TTS call in a client that logs: stage, model, input/output tokens, computed cost from a `pricing.yaml` checked into the repo.
* Emit `cost_report.json` per run with per-stage subtotals. Fail the run with a warning if projected episode cost exceeds a configurable ceiling (default $20).
* Budget targets per run: Stage 2 ≤ $1, Stage 4 ≤ $2, Stage 5 ≤ $6, Stage 6 ≤ $3, TTS ≤ $1.

5. Operational requirements

* Config: single `config.yaml` (categories, window, model choices per stage, budget ceiling, render settings). All API keys via env vars; document in `.env.example`. Never hardcode keys.
* Scheduling: GitHub Actions cron (Tue/Thu) is the default — DECIDE, but note render step needs ffmpeg + possibly Node in the runner. If render is too heavy for Actions, split: Stages 1–7 in Actions, render locally via a `make render RUN=<date>` target, and ASK before committing to a paid runner.
* Idempotency: `pipeline run --from <stage> --run <date>` re-runs from any stage using cached upstream artifacts. `--paper <id>` re-runs a single paper's extract/script/voice/render.
* Failure policy: if a paper fails extraction/enrichment, substitute the next-ranked paper (up to rank 6) rather than shipping 2 papers; log the substitution.
* Testing: unit tests for LaTeX figure/caption parsing (fixture tarballs from 3–4 real papers of varying messiness), schema validation, cost accounting math, and timeline assembly. One end-to-end "golden run" test on a fixed cached paper set with mocked LLM responses.

6. Implementation phases
Build in this order; each phase ends with something runnable.

* Phase 1 — Skeleton + Fetch/Shortlist: repo layout, config, cost-tracking client, Stages 1–2 working end-to-end. Output: ranked shortlist JSON for a real date window.
* Phase 2 — Enrich + Rank: LaTeX source download, figure/caption extraction (this is the fiddliest part — budget real effort and tests here), Stage 4. Output: top-3 with justifications and extracted figures on disk.
* Phase 3 — Extract + Script: PaperDigest and SceneManifest generation with schema validation. Output: reviewable scripts as markdown alongside the JSON.
* Phase 4 — Voice + Render one segment: TTS + renderer for a single paper segment. This is the go/no-go quality checkpoint — ASK for owner review of the first rendered segment before proceeding.
* Phase 5 — Full episode assembly + packaging + scheduling.
* Phase 6 — Upload: Stage 10 behind `upload.enabled`, dry-run first, live once the compliance audit clears. **The audit is the long pole and does not depend on any code** — submit it at Phase 1 so it is not the thing holding up the first publish.

7. Repo conventions

* Python 3.11+, `uv` or `pip-tools` for deps (DECIDE); `ruff` + type hints; pydantic for all inter-stage schemas.
* Layout: `pipeline/` (stages as modules with a shared `Stage` interface), `render/`, `prompts/` (all prompts as versioned files, never inline strings), `tests/`, `runs/` (gitignored), `DECISIONS.md`.
* Every prompt change or model swap gets a line in `DECISIONS.md` with rationale.

8. Open questions for the owner (ASK when relevant, don't block Phase 1)

1. Which LLM providers/keys are available for this project (Anthropic / OpenAI / Groq)? Determines Stage 2 and 4–6 model choices.
2. Voice preference: single narrator voice OK for v1? Any accent/gender preference?
3. Branding: channel/series name for the title card, or placeholder?
4. Music bed: yes/no for v1?