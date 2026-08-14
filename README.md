# tabloid-ai

Automated pipeline that turns the week's notable arXiv AI/ML papers into a short
video series, **ML Papers of the Day**. `SPEC.md` is the full design;
`DECISIONS.md` records every choice made along the way.

**Status: all ten stages built. Stages 1-9 validated end-to-end against live
APIs across three episodes; Stage 10 (upload) runs in dry-run until a YouTube
compliance audit clears.** A single command turns a live arXiv window into an
upload-ready episode: **`episode.mp4` (5:16, 1920x1080 H.264), three standalone
segments, a thumbnail, and metadata with chapter timestamps** — for **$1.93**.

| Stage | Status | Validated against |
|---|---|---|
| 1 fetch | done | live arXiv API, 1,385 papers; window marker follows the index (D31) |
| 2 shortlist | done | **live**, 1,382 scored after exclusions, $1.032 |
| 3 enrich | done | **live**, 15/15 papers, all LaTeX, zero failures |
| 4 rank | done | **live**, 15 real candidates, 3 subfields, $0.099 |
| 5 extract | done | **live**, 3 digests, every number traceable, $0.224 |
| 6 script | done | **live**, 3 segments + wrapper + 3 bridges, 6-scene cap held, $0.099 |
| 7 voice | done | **live**, 28 clips, ElevenLabs multilingual_v2, $0.480 |
| 8 render | done | **live**, 8 parts, two-level crossfade, 5ms A/V drift |
| 9 package | done | **live**, episode + segments + thumbnail + chapters (−0.09s) |
| 10 upload | dry-run | request body validated against the real bundle; **live path blocked on a YouTube API compliance audit** |

A full run is 1,385 papers → 15 shortlisted → 15 enriched → 3 finalists →
3 digests → 28 narrated scenes → 8 rendered parts → one episode. About 10 minutes
end to end, most of it arXiv rate limiting and ffmpeg.

Scheduling is built (Tue/Thu on GitHub Actions) but **switched off** — see
[Scheduling](#scheduling) for the one command that starts it.

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
pipeline youtube-auth                # one-time OAuth; prints a refresh token

# The upload-ready bundle lands in runs/<date>/output/:
#   episode.mp4  segment_<id>.mp4 x3  thumbnail.png  metadata.json  cost_report.json
#   upload.json  <- stage 10: what was (or would be) sent to YouTube
```

### Publishing (stage 10)

`upload.enabled` is **false** in `config.yaml`, so the stage runs as a dry-run: it
validates the bundle against YouTube's limits, computes the scheduled publish
time, and writes the exact `videos.insert` body to `output/upload.json` without
calling the API. Review that file, then upload `episode.mp4` by hand in YouTube
Studio using `metadata.json`.

Going live needs three things, in order: a Google Cloud project with the YouTube
Data API enabled and a Desktop-app OAuth client; `pipeline youtube-auth` once, to
mint the refresh token; and **the YouTube API compliance audit to pass** — until
it does, uploads from the project are locked to `private` and `publishAt` is
ignored. Then `uv pip install -e '.[youtube]'` and flip `upload.enabled: true`.

Every upload sets `containsSyntheticMedia: true`. That is not configurable.

## Scheduling

Two workflows, split by whether they can spend money:

| Workflow | Trigger | Secrets | What it does |
|---|---|---|---|
| `ci.yml` | every push + PR | **none** | ruff, 186 tests, and the Linux render check |
| `episode.yml` | Tue/Thu 13:00 UTC, or manual | API keys | one full episode, uploaded as an artifact |

**The schedule is off.** Each run spends ~$1.73, so it does nothing until you opt
in:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set ELEVENLABS_API_KEY
gh variable set EPISODE_ENABLED --body true   # <- starts the Tue/Thu cron
```

Manual runs work as soon as the secrets exist, without the variable:
`gh workflow run episode.yml -f from_stage=fetch`.

The episode lands as a workflow artifact (`episode-<n>`, kept 90 days) — that is
the handoff, since Stage 10 is still in dry-run. A failed run uploads JSON
diagnostics instead. Both workflows install `ffmpeg` and `fonts-dejavu-core`
explicitly; the renderer silently falls back to an unreadable bitmap font without
a real one, which is what `tests/test_portability.py` exists to catch (D25).

Two things worth knowing: GitHub disables scheduled workflows in public repos
after 60 days of no repo activity, and 13:00 UTC is 09:00 ET — about three hours
of slack before Stage 10's noon publish slot, which absorbs Actions' cron drift.

### The fetch window

`state.json` records where the last successful run stopped, so the next one does
not re-cover the same papers — without it, a Tue/Thu cadence against the 4-day
fallback window would let one paper headline two consecutive episodes.

It advances only after a run that earned it: a full `fetch`→`package` pass, never
a `--from <stage>` replay or a `--paper` rebuild. It moves to the **end of the
fetch window**, not to when the run finished, because the ~10 minutes in between
would otherwise become a permanent hole in coverage. In Actions it persists
through the cache; a cache miss falls back to the 4-day window, which overlaps
rather than breaks (D25).

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
  youtube.py     Stage 10: YouTube Data API v3 client, upload scope only (D24)
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

Cost of one clean pass over a real 1,225-paper window:

| Stage | Calls | Cost | Target |
|---|---|---|---|
| shortlist | 62 | $0.921 | $1.00 |
| enrich | 0 | $0.000 | — (no LLM calls) |
| rank | 1 | $0.102 | $2.00 |
| extract | 3 | $0.146 | $6.00 |
| script | 4 | $0.090 | $3.00 |
| voice | 27 | $0.469 | $1.00 |
| **per episode** | **97** | **$1.728** | ceiling $3.00 |

`runs/2026-08-07/` itself reports **$1.96 over 158 calls** — higher because
extract and script were each re-run once and narration three times (once per TTS
engine tried). The report is cumulative per run by design (D23), so it records
development iteration as well as the shipped pass.

Stages 3, 8 and 9 make no billed calls at all: enrichment is downloads and
parsing; rendering and packaging are Pillow + ffmpeg. Narration is the only
non-LLM cost (D22); swapping `tts.provider` to `macos` makes it free.

`cost_report.json` is rebuilt from the append-only `calls.jsonl` on every run, so
it reflects the whole run rather than the last invocation (D23).

> **D6's $0.77 shortlist projection was ~20% low — the measured figure is
> $0.921** (see `DECISIONS.md` D15). Output tokens, not abstracts, were
> underestimated. That leaves only 8% headroom against the $1.00 target, so a
> heavier window would breach it.

`pricing.yaml` is verified for the two LLM models in use (`claude-haiku-4-5`
$1/$5, `claude-sonnet-4-5` $3/$15). The **audio** prices are not verified — they
came from the initial scaffold, and `pricing.yaml` says so.

`budget.ceiling_usd` is **3.0**, enforced **per run and cumulative across
invocations**: resuming with `--from` does not grant a fresh budget.

## Tests

```bash
pytest        # 186 tests, no network, no API spend; ffmpeg optional
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

Stage 10 is faked at two seams — a fake `YouTubeClient` for the stage's policy
and a fake `googleapiclient` service for the resumable-upload retry loop — so the
suite cannot publish a video, and needs none of the `[youtube]` extras installed.

`tests/test_portability.py` is the exception to "no ffmpeg needed": it runs the
real renderer against the real ffmpeg and skips when there is none. That is the
check that the Linux runner can produce a readable episode rather than one set in
Pillow's fallback bitmap font (D25).

### Reviewing a script

Stage 6 writes markdown next to the JSON, because pacing and wording are judged
by reading prose:

```
runs/<date>/script/episode.md              # title, description, cold open, bridges, all segments
runs/<date>/script/segment_<arxiv_id>.md   # one segment, scene by scene
```

`episode.md` interleaves the bridges with the segments in the order they play,
because the seam between two parts is the thing being reviewed. The per-segment
files deliberately leave them out — a bridge belongs to the episode cut, not to
the standalone segment, which has to be publishable on its own (D26).

## Known gaps

- **The episode runs long.** 5:16 against a 4:30 target (D29). Segments are close
  — 250s against 225s — but the wrapper grew to 66s once the cold open had to
  name the series and the shared thread (D30). Trimming means the wrapper, not
  the papers.
- **Stage 2 headroom is gone, not thin** — $1.03 measured against a $1.00 target
  on the last two runs (D15). The target needs raising or the batch size
  revisiting.
- **A run during an arXiv index stall fails loudly** rather than skipping papers
  (D31). That is the right trade, but a scheduled run can fail for reasons that
  have nothing to do with this code — as it did on 2026-08-13.
- **Subfield diversity is enforced on model-supplied labels**, so near-synonymous
  tags can defeat it (D16). A closed tag vocabulary in the prompt is the fix.
- **EPS/PS figures cannot be rasterised** — needs ghostscript, which is not
  installed. PDF and raster figures work (D12).
- **The music bed is off by default.** No royalty-free track is shipped; the
  synthesised fallback reads as hum once audible. Drop a file in `assets/music/`
  and set `render.background_music: true` (D21).
- **Publishing is still manual, and the blocker is not code.** Stage 10 is built
  and runs in dry-run, but going live needs a **YouTube API compliance audit**
  (~2-4 weeks, owner action). Until it passes, uploads from an unverified project
  are locked to `private` and scheduled publishing is ignored. Upload
  `episode.mp4` by hand via YouTube Studio using `metadata.json`. The live path
  has never run against the real API — only against a faked one (D24).
- **The schedule is built but inert.** `episode.yml` will not run on its cron
  until `EPISODE_ENABLED` is set and the two API secrets exist — deliberately, so
  merging it does not start billing (D25).
- **The Linux render path has never produced a real episode.** CI proves the
  fonts, ffmpeg and libx264 are there and that a slide encodes; the full
  1920x1080 five-part stitch has only ever run on macOS. The first scheduled run
  is the real test.
