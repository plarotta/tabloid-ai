# tabloid-ai

Automated pipeline that turns the week's notable arXiv AI/ML papers into a 3-5
minute video. `SPEC.md` is the full design; `DECISIONS.md` records every choice
made along the way.

**Status: all nine stages built and validated end-to-end.** A single command
turns a live arXiv window into an upload-ready episode: **`episode.mp4` (4.9 min,
1920x1080 H.264), three standalone segments, a thumbnail, and metadata with
chapter timestamps** — for **$1.96**.

| Stage | Status | Validated against |
|---|---|---|
| 1 fetch | done | live arXiv API, 1,225 papers |
| 2 shortlist | done | **live**, 1,225 real papers, $0.921 |
| 3 enrich | done | 21 real papers end-to-end, 9 real e-prints parsed |
| 4 rank | done | **live**, 15 real candidates, $0.102 |
| 5 extract | done | **live**, 3 digests, 12/12 results traceable, $0.146 |
| 6 script | done | **live**, 3 segments + wrapper, 284s, $0.090 |
| 7 voice | done | **live**, 27 clips, ElevenLabs Bella, $0.469 |
| 8 render | done | **live**, 5 parts stitched, crossfades, <30ms A/V drift |
| 9 package | done | **live**, episode + segments + thumbnail + chapters |

A full run (1,225 papers → 15 shortlisted → 15 enriched → 3 finalists → 3 digests
→ a 4.7-minute episode script) costs **$1.26** and takes about 6 minutes.

## Setup

```bash
uv venv --python 3.12
uv pip install -e '.[all]'      # or '.[anthropic]' for just the default config
cp .env.example .env            # then fill in a key
```

## Usage

```bash
pipeline run                       # full pipeline for today
pipeline run --from enrich         # replay from a stage, reusing cached artifacts
pipeline run --run 2026-08-07      # target an existing run directory
pipeline run --paper 2608.04424    # restrict per-paper stages to one paper
pipeline shortlist --run 2026-08-07  # view the shortlist
pipeline rank --run 2026-08-07       # view finalists + substitutes
pipeline cost --run 2026-08-07       # view the cost report

# The upload-ready bundle lands in runs/<date>/output/:
#   episode.mp4  segment_<id>.mp4 x3  thumbnail.png  metadata.json  cost_report.json
```

## How it is organised

Every stage writes to `runs/<date>/<stage>/` and reads only from disk. Stages
never hand Python objects to each other. That is what makes `--from <stage>`
re-runs possible and what keeps development from re-spending on upstream stages.

```
pipeline/
  arxiv.py       Atom API client (rate limiting, paging, de-duplication)
  eprint.py      e-print download + safe unpacking (tar/gz/pdf)
  latex.py       figure, caption and section extraction from LaTeX source
  figures.py     figure normalisation to raster; PDF-embedded-image fallback
  signals.py     free external signals (GitHub, Hugging Face)
  config.py      config.yaml loading, validated up front
  schemas.py     pydantic models for every inter-stage artifact
  stage.py       Stage interface: run / is_complete / load
  paths.py       run directory layout, atomic JSON+JSONL writes
  prompts.py     loads versioned prompts from prompts/<stage>/v<N>.md
  state.py       last-successful-run marker, for the next fetch window
  llm/           provider-agnostic client + cost tracking
  tts/           Stage 7: interface + macOS/OpenAI adapters (D19)
  render/        Stage 8: Pillow slide composition + thumbnail (D20)
  stages/        one module per stage
prompts/         all prompts as versioned files, never inline strings
```

### Stage 3 (enrich) is free

It makes no LLM calls — only arXiv and Hugging Face requests. That is deliberate:
it is the fiddliest stage in the pipeline, and being able to re-run it against
real papers at zero cost is what makes it tractable to get right. See
`DECISIONS.md` D11 for the LaTeX parsing rules and why each one exists.

## Cost

Every billed call goes through `MeteredClient`, which prices it against
`pricing.yaml` and appends it to `runs/<id>/calls.jsonl` immediately. A run that
crashes still leaves an accurate partial trail. Exceeding `budget.ceiling_usd`
aborts the run.

Measured cost of a full run on a real 1,225-paper window:

| Stage | Calls | Cost | Target |
|---|---|---|---|
| shortlist | 62 | $0.921 | $1.00 |
| enrich | 0 | $0.000 | — (no LLM calls) |
| rank | 1 | $0.102 | $2.00 |
| extract | 3 | $0.146 | $6.00 |
| script | 4 | $0.090 | $3.00 |
| voice | 27 | $0.469 | $1.00 |
| **total** | **~100** | **$1.96** | ceiling $3.00 |

Stages 3, 8 and 9 make no billed calls at all: enrichment is downloads and
parsing; rendering and packaging are Pillow + ffmpeg. Narration is the only
non-LLM cost (D22); swapping `tts.provider` to `macos` makes it free.

`cost_report.json` is rebuilt from the append-only `calls.jsonl` on every run, so
it reflects the whole run rather than the last invocation (D23).

> **D6's $0.77 shortlist projection was ~20% low — the measured figure is
> $0.921** (see `DECISIONS.md` D15). Output tokens, not abstracts, were
> underestimated. That leaves only 8% headroom against the $1.00 target, so a
> heavier window would breach it.

`pricing.yaml` is verified for the two models in use (`claude-haiku-4-5` $1/$5,
`claude-sonnet-4-5` $3/$15). `budget.ceiling_usd` is **2.0**, and is enforced
**per run, not per session**.

## Tests

```bash
pytest        # 159 tests, no network, no API spend, no ffmpeg needed
ruff check .
```

arXiv parsing is tested against a real captured API response
(`tests/fixtures/arxiv_page.xml`). LaTeX parsing is tested against
`tests/fixtures/eprints/*.tar.gz` — the real `.tex` files from three published
papers chosen for how differently they are built (non-standard root filename, a
commented-out `\input` that must not be followed, and a paper that reaches all
its content only through two-argument `\subimport`). Figure binaries are replaced
with stubs to keep the fixtures small.

Stages 2, 4, 5 and 6 are tested with a fake LLM covering the failure modes that
matter: unparseable responses, hallucinated arXiv IDs, duplicate entries,
fabricated numbers, invented figure paths, and total provider failure.

### Reviewing a script

Stage 6 writes markdown next to the JSON, because pacing and wording are judged
by reading prose:

```
runs/<date>/script/episode.md              # title, description, cold open, all segments
runs/<date>/script/segment_<arxiv_id>.md   # one segment, scene by scene
```

## Known gaps

- **Stage 2 headroom is thin** — $0.921 measured against a $1.00 target (D15).
  A heavier window would breach it.
- **Subfield diversity is enforced on model-supplied labels**, so near-synonymous
  tags can defeat it (D16). A closed tag vocabulary in the prompt is the fix.
- **EPS/PS figures cannot be rasterised** — needs ghostscript, which is not
  installed. PDF and raster figures work (D12).
- **The music bed is off by default.** No royalty-free track is shipped; the
  synthesised fallback reads as hum once audible. Drop a file in `assets/music/`
  and set `render.background_music: true` (D21).
- **Publishing is manual**, as the spec intends for v1 — the pipeline produces
  upload-ready assets in `runs/<date>/output/` and stops there.
- **Scheduling is not set up** (spec §5 — GitHub Actions cron Tue/Thu). Note the
  render step needs `ffmpeg`, and narration needs `ELEVENLABS_API_KEY`.
- **`state.json` is still not advanced**, so consecutive runs re-cover the same
  window. It should move only once an episode is actually published.
- `state.json` is deliberately not advanced yet — it should only move once the
  pipeline produces a full episode, otherwise the next run would skip papers.
