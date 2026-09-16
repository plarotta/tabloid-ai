# tabloid-ai

Automated pipeline that turns the week's notable arXiv AI/ML papers into a short
video series, **ML Papers of the Day**. `SPEC.md` is the full design;
`DECISIONS.md` records every choice made along the way.

**Status: all ten stages built. Stages 1-9 validated end-to-end against live
APIs across three complete episodes; Stage 10 (upload) runs in dry-run until a
YouTube compliance audit clears.** A single command turns a live arXiv window
into an upload-ready episode: **`episode.mp4` (1920x1080 H.264), three standalone
segments, a thumbnail, and metadata with chapter timestamps** — for about **$2**
on a normal window.

The fourth episode (2026-08-19) runs **4:20 across 31 clips at 9.0-9.8s a slide**,
against episode one's 4:51 at 12.1s and episode three's 5:16 at 13.9s — the
length and pace work of D34/D35, measured on a finished file. It cost **$2.97**
over a 2,180-paper catch-up window, most of it Stage 2.

Episodes are numbered: YouTube titles read `ML Papers of the Day Ep. N: <title>`,
with `N` tracked in `state.json` beside the window marker and advanced by the same
packaged run (D38).

| Stage | Status | Validated against |
|---|---|---|
| 1 fetch | done | live arXiv API, 1,385-2,180 papers; window marker follows the index (D31) |
| 2 shortlist | done | **live**, 2,171 scored after exclusions, $1.635 |
| 3 enrich | done | **live**, 15/15 papers, all LaTeX, zero failures |
| 4 rank | done | **live**, 15 real candidates, 3 subfields, $0.099 |
| 5 extract | done | **live**, 3 digests, every number traceable, $0.224 |
| 6 script | done | **live**, 3 segments + wrapper + 3 bridges, scene cap and word budget held, $0.126-0.356 |
| 7 voice | done | **live**, 24-31 clips, ElevenLabs multilingual_v2, $0.373-0.480 |
| 8 render | done | **live**, 7-8 parts, two-level crossfade, 5ms A/V drift |
| 9 package | done | **live**, episode + segments + thumbnail + chapters (−0.09s) |
| 10 upload | dry-run | request body validated against the real bundle; **live path blocked on a YouTube API compliance audit** |

A full run is 1,385-2,180 papers → 15 shortlisted → 15 enriched → 3 finalists →
3 digests → ~31 narrated scenes → 8 rendered parts → one episode. About 10-20
minutes end to end, most of it arXiv rate limiting and ffmpeg. Stage 2 scores
every paper in the window, so both the runtime and the cost track how long it has
been since the last run.

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
| `ci.yml` | every push + PR | **none** | ruff, 249 tests, and the Linux render check |
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

It advances after any run that **packaged an episode over a real fetch window** —
including a `--from script` replay, which is how an episode gets finished after an
outage. A `--paper` rebuild is excluded, and the marker only ever moves forward, so
re-rendering an old run cannot drag coverage backwards. It moves to the **end of
the fetch window**, not to when the run finished, because the ~10 minutes in
between would otherwise become a permanent hole in coverage.

Getting that wrong is expensive rather than merely untidy: the 2026-08-19 episode
went unrecorded under the old rule, and the next window opened at **14 days and
3,000 papers** — the `max_papers` cap — instead of 8 days and ~2,400 (D38).

In Actions it persists through the cache; a cache miss falls back to the 4-day
window, which overlaps rather than breaks (D25).

## How it is organised

Every stage writes to `runs/<date>/<stage>/` and reads only from disk. Stages
never hand Python objects to each other. That is what makes `--from <stage>`
re-runs possible and what keeps development from re-spending on upstream stages.

```
pipeline/
  arxiv.py       Atom API client (rate limiting, paging, de-duplication)
  oai.py         OAI-PMH bulk harvester, the fallback when the Atom
                 endpoint refuses us; `fetch.source` picks one (D45)
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
  render/        Stage 8: Pillow slide composition, captions, thumbnail (D20, D33)
  youtube.py     Stage 10: YouTube Data API v3 client, upload scope only (D24)
  stages/        one module per stage
prompts/         all prompts as versioned files, never inline strings
                 (one directory per stage, plus condense/ - the pass that sends
                 an over-long segment back for a shorter draft, D34)
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

Cost of one clean pass over a real 1,225-paper window, and over the 2,180-paper
catch-up window of 2026-08-19:

| Stage | Calls | 1,225 papers | Calls | 2,180 papers | Target |
|---|---|---|---|---|---|
| shortlist | 62 | $0.921 | 109 | $1.635 | $1.00 |
| enrich | 0 | $0.000 | 0 | $0.000 | — (no LLM calls) |
| rank | 1 | $0.102 | 1 | $0.108 | $2.00 |
| extract | 3 | $0.146 | 3 | $0.239 | $6.00 |
| script | 4 | $0.090 | 7 | $0.126 | $3.00 |
| voice | 27 | $0.469 | 24 | $0.373 | $1.00 |
| **per episode** | **97** | **$1.728** | **144** | **$2.481** | ceiling $4.00 |

Only Stage 2 moves with the window: it scores every paper fetched, so a run costs
roughly what the gap since the last one costs. That is why the ceiling is $4.00
rather than $3.00 — a 7.4-day catch-up window left no headroom under the old one
(D34). The script column includes the condense pass, which is a second call
against any part that comes back over its word budget.

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
pytest        # 249 tests, no network, no API spend; ffmpeg optional
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

- **Title backdrops have never generated an image.** `render.title_backdrops` is
  on and a key is now configured, but the OpenAI account has no credits, so the
  first real request came back `insufficient_quota` and every title card falls
  back to the flat design. The compositing was validated against a stand-in; the
  generator has made exactly one live request and it was refused. Add credit and
  it costs ~$0.25 an episode, cached so re-renders are free (D39).
- **The runtime projection is good to about ±5%.** It reads the finished length
  off the word count at Stage 6 rather than waiting for Stage 8. The read rate
  moves with how long the words are — 2.33 to 2.45 spoken words a second across
  four runs — so treat "4:45" as "somewhere between 4:31 and 4:59" (D34).
- **Stage 2's $1.00 target is not a fixed number.** It cost $1.03 on a 3.6-day
  window and $1.63 on a 7.4-day one, because it scores every paper in the window
  (D15, D34). The target wants to scale with the window, or be dropped.
- **The Atom search endpoint blocked this address for over a day.** It began
  answering every request with 429 on 2026-09-13 and had not relented 24 hours
  later, while the website, the RSS feeds and OAI-PMH all answered normally.
  Stage 1 now has a second interface and `fetch.source` chooses; the config
  ships pointed at `oai`. The block was probably self-inflicted, because each
  failed run fires four retries at a server that already refused (D45).
- **The harvest scans far more than it keeps.** OAI-PMH filters on when a
  record was last modified, not when it was submitted, and its sets are coarse,
  so the first live run scanned 10,160 records to keep 2,342. It is free and
  takes a few minutes; it costs nothing at Stage 2, which only sees survivors.
- **A run during an arXiv index stall fails loudly** rather than skipping papers
  (D31). That is the right trade, but a scheduled run can fail for reasons that
  have nothing to do with this code — as it did on 2026-08-13.
- **Subfield diversity is enforced on model-supplied labels**, so near-synonymous
  tags can defeat it (D16). A closed tag vocabulary in the prompt is the fix.
- **EPS/PS figures cannot be rasterised** — needs ghostscript, which is not
  installed. PDF and raster figures work (D12).
- **Captions and Ken Burns are built but switched off.** `render.captions` and
  `render.motion` work end to end and were validated against a real episode —
  same frame count, same duration, and with both off the render reproduces the
  pre-change episode to the byte. They are off because the *look* was rejected
  on review, not the mechanism (D33). Turning them on unchanged reproduces what
  was rejected; the caption's container and the ~16% band it takes out of every
  slide are what need rethinking first. Motion also needs
  `crossfade_seconds > 0` — the zoom is a per-slide filter and a hard-cut render
  has one input for the whole part — and costs 2.5x file size and 1.9x render
  time.
- **A dead API account was treated as a flaky one.** Fixed, but worth knowing it
  happened: on 2026-08-19 an exhausted credit balance came back as a 400, got
  retried three times, and then fell through the "a wrapper failure is not worth
  failing an episode over" path (D18) — so the run narrated and rendered an
  episode with no title before Stage 10 stopped it. Account-level failures are
  now a distinct error that is never retried and never degraded around, and the
  script stage fails at its boundary instead (D35). The same broad handlers still
  exist in the other LLM stages.
- **The visual system flipped back.** Near-black ground and a single accent, as
  in episode one (D35), reversing the paper-white ground and per-paper accents of
  D27. Both palettes are recorded; the swap is six constants in
  `render/slides.py`. The ground is since lifted slightly off black, and a sixth
  palette role — `BASE`, for the value a comparison is measured against — was
  added but is not yet drawn by any static slide type (D36).
- **Animated callouts have never rendered on Linux.** They work on macOS and
  ship in the 2026-08-19 episode, but manim draws through cairo and pango and the
  scheduled workflow has never run with the `manim` extra installed. If those
  libraries are missing on the runner, every callout falls back to a static slide
  — correctly and silently, which is the failure mode to watch for rather than a
  crash (D37). The same caveat as the render path itself.
- **A `note` can describe a different ratio than its bars draw.** `two_bar`
  renders `note` under a brace spanning the gap between the two bars, so a true
  fact about some *other* ratio reads as a claim about the one on screen. It
  happened on the first live run ("dynamics receive 65x more labels" under bars
  of 79.4 and 21.4). No schema can catch it; `script/v7` states the rule, and
  that is the whole defence (D37).
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
