# Decisions

Every DECIDE point from the spec, every model swap, and every prompt change gets
a dated entry here with its rationale. Newest last.

---

## D1 - Dependency management: `uv`, Python pinned to 3.12

**Date:** 2026-08-07 · **Spec:** §7 (`uv` or `pip-tools`, DECIDE)

`uv` for speed and its ability to manage the interpreter itself, which turned out
to matter: the machine's system Python is **3.14.2**, and several dependencies
this project will need in Phase 2 (notably `pymupdf` for the PDF figure-extraction
fallback) do not reliably ship 3.14 wheels yet. `uv venv --python 3.12` pins a
known-good interpreter without touching the system install.

`pyproject.toml` constrains `requires-python = ">=3.11,<3.13"` so this cannot
silently drift back onto 3.14.

**Reversibility:** high. `pyproject.toml` is standard; swapping to pip-tools is
mechanical.

---

## D2 - LLM access: provider-agnostic abstraction

**Date:** 2026-08-07 · **Spec:** §3 Stage 2/4 (DECIDE), §8 Q1 (ASK) · **Owner decision**

The environment had **no usable API keys** — `ANTHROPIC_API_KEY` was exported but
empty, and no OpenAI or Groq key was present. Since the key situation could not
be inferred, this was put to the owner, who chose a provider-agnostic abstraction
over committing to one vendor.

Implemented as `LLMClient` (`pipeline/llm/base.py`) with adapters for Anthropic,
OpenAI and Groq in `providers.py`. Model choice is per stage in `config.yaml`;
SDK imports are lazy so only the providers actually in use need installing.

Defaults, following the spec's guidance:

| Stage | Provider | Model |
|---|---|---|
| shortlist (2) | anthropic | `claude-haiku-4-5` |
| rank (4) | anthropic | `claude-sonnet-4-5` |
| extract (5) | anthropic | `claude-sonnet-4-5` |
| script (6) | anthropic | `claude-sonnet-4-5` |

**Cost of the choice:** three adapters to maintain instead of one, and provider
usage-reporting differences must be normalised in each adapter.

---

## D3 - Stage 7 TTS: interface only in Phase 1

**Date:** 2026-08-07 · **Spec:** §3 Stage 7 (DECIDE), §8 Q2 (ASK) · **Owner decision**

The spec's default is OpenAI `tts-1`, but **no OpenAI key is configured**, so the
default was not available as written. Anthropic offers no TTS API, so the chosen
LLM provider does not cover this stage.

Owner elected to defer the engine choice to Phase 4, where the spec already places
the go/no-go quality checkpoint. Phase 1 ships `pipeline/tts/base.py` — the
`TTSClient` interface and `SpeechResult` — with `build_tts_client()` raising
`NotImplementedError` carrying a pointer to this entry.

The interface requires `synthesize()` to return a **measured** duration, encoding
the spec's rule that Stage 8 builds its timeline from real audio lengths and never
from the script's `est_seconds`.

**Open:** the actual engine. Blocks Phase 4, not Phases 1–3.

**Resolved by D19, then D22:** ElevenLabs `eleven_turbo_v2_5`, voice Bella, after
both macOS engines were rejected in review. The interface-first approach paid
off — swapping engines was a config change, not a refactor.

---

## D4 - arXiv: hand-rolled Atom client over HTTPS

**Date:** 2026-08-07 · **Spec:** §3 Stage 1

No wrapper library. The query surface is small and we need direct control over
rate limiting and retries on a free API.

Two things established by probing the live API rather than assuming:

- **The `http://` endpoint 301-redirects.** The spec gives `http://export.arxiv.org`;
  requesting it without follow-redirects returns an empty body and a confusing XML
  parse error. `pipeline/arxiv.py` uses `https://` directly.
- **`submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]` filters server-side**, and an
  `OR` of categories comes back already de-duplicated. We de-dupe by arXiv ID
  anyway, because paging can overlap when new papers land mid-crawl.

Version suffixes are stripped (`2608.06377v1` → `2608.06377`) so an ID is stable
across revisions.

---

## D5 - Window size is ~2-3x the spec's estimate

**Date:** 2026-08-07 · **Spec:** §3 Stage 1 ("expect 300-700 papers")

Measured against the live API:

- 4-day, 5-category window: **1,745 papers**
- 3-day window actually fetched on 2026-08-07: **1,225 papers**

Not a decision so much as a correction to a planning assumption, but it drives
the Stage 2 cost envelope (see D6) and is why `fetch.max_papers` (default 3000)
exists as a circuit breaker against a runaway window.

---

## D6 - Shortlist batching: 20 abstracts per call

**Date:** 2026-08-07 · **Spec:** §3 Stage 2, §4 (Stage 2 ≤ $1)

Measured on the real 1,225-paper window:

| Batch size | Calls | Haiku | Groq llama-3.1-8b |
|---|---|---|---|
| 1 | 1225 | $1.29 | $0.055 |
| 20 | 62 | **$0.77** | $0.029 |
| 40 | 31 | $0.76 | $0.029 |

Batching amortises the ~500-token system prompt, worth ~40%. Beyond ~20 the curve
is flat, because the abstracts themselves (~490k input tokens) dominate and do not
shrink with batch size. 20 is chosen over 40 because larger batches degrade
per-paper scoring attention and increase the blast radius of one truncated JSON
response.

**Superseded by D15: the measured figure is $0.921, not $0.77** — output tokens
were underestimated, leaving 8% headroom rather than 23%. The reasoning below
stands; the number does not.

**This is thin.** $0.77 against a $1.00 target is 23% headroom, and the 1,745-paper
window measured in D5 would breach it. Mitigations, in order of preference if it
becomes a problem: point this stage at a cheap hosted model (Groq is ~26x cheaper
here and the owner already uses it elsewhere), tighten the 1,500-char abstract
truncation, or drop papers whose primary category is outside the target set
(~10% of the window is cross-listed noise like `cs.CR`, `cs.RO`).

---

## D7 - Stage failure isolation in shortlist

**Date:** 2026-08-07 · **Spec:** §1 (fail loudly, no silent partial output)

Tension: the spec demands loud failure, but one malformed JSON response out of 62
batches should not discard the other ~1,200 papers.

Resolution — fail loudly at the level that matters:

- A single failed batch logs a warning and drops **only its own** papers.
- If scoring coverage falls below 80%, a prominent warning names the shortfall.
- If **every** batch fails, or no paper clears `min_score`, the stage raises
  `StageError` and the run stops.

Scores for all papers (not just the 15 survivors) are persisted to
`shortlist.json` so screening quality can be audited after the fact.

---

## D8 - Cost tracking: single choke point, loud about gaps

**Date:** 2026-08-07 · **Spec:** §4

Nothing calls a provider SDK directly; everything goes through `MeteredClient`,
which is the only place usage is priced and recorded.

Two properties worth stating explicitly:

- **Append-only.** Each call is flushed to `runs/<id>/calls.jsonl` as it happens,
  so a crashed run still leaves an accurate partial cost trail.
- **A model missing from `pricing.yaml` is priced at $0 but flagged `unpriced`**,
  and the flag is surfaced in the report and CLI output. A cost report that
  silently undercounts is worse than no cost report.

Failed calls are **not** recorded — most providers do not bill them, and counting
them would overstate spend.

`pricing.yaml` carries a `verified_on` field and is currently marked
`unverified-initial-scaffold`. **The prices in it are from memory and have not
been checked against live pricing pages.** They must be verified before any cost
report is used for a real decision.

---

## D9 - Indentation: tabs

**Date:** 2026-08-07

Following the sole recorded preference in the owner's notes ("indents with tabs,
not spaces", low confidence). `ruff format` is configured with
`indent-style = "tab"` so this is enforced rather than left to drift.

**Reversibility:** trivial — flip the setting and run `ruff format .`.

---

## D10 - Spec committed to the repo

**Date:** 2026-08-07

The spec was the declared source of truth but existed only in a chat transcript;
`README.md` referenced "the spec" with nothing to point at. It is now
`SPEC.md`, copied verbatim. A source of truth that can be lost with a closed
terminal is not one.

---

## D11 - LaTeX parsing: flatten first, match braces, never regex structure

**Date:** 2026-08-07 · **Spec:** §3 Stage 3, §5 (testing)

The spec calls this the fiddliest part and it is. Four properties were measured
against three real e-prints (Qwen3, MEM1, Gemini 2.5), not assumed:

- **Multi-file is the norm.** All three split sections across `\input{}` files.
  A parser reading only the root file finds almost nothing, so the include graph
  is flattened before any parsing. Cycle- and depth-guarded.
- **The root is not reliably `main.tex`.** Qwen3's is `colm2024_conference.tex`.
  The root is found by looking for `\documentclass` + `\begin{document}`.
- **`\subimport{dir}{file}` exists and takes two arguments.** The Gemini report
  pulls in *every* section that way. Before this was handled the paper parsed as
  0 figures / 0 intro / 6 sections; after, 17 figures / 3.1k-char intro / 76
  sections. Single-argument-only include matching silently empties such papers.
- **Captions nest braces heavily.** `\caption{\textbf{... \textbf{bold} and
  \underline{...}}}` is real. A `\{[^}]*\}` regex truncates at the first inner
  `}`. All brace groups are matched by depth counting.

Two smaller rules that each fixed an observed defect:

- `\textcolor{red}{existing}` must drop only its *first* argument; the naive
  strip produced `redexisting` in a caption.
- LaTeX escapes are unescaped last (`lp\_ship12l` → `lp_ship12l`), after brace
  removal, or `\{` would be unescaped into a brace that then gets deleted.

**Scoping figure extraction to `\begin{figure}` blocks** is what keeps inline
title-block logos (`\includegraphics[height=24pt]{logo/qwen-logo.pdf}`, six of
them in Qwen3) out of the figure inventory. No filename heuristics needed.

Comment stripping is load-bearing rather than cosmetic: Qwen3 ships
`%\input{content/experiments.tex}`. Honouring the comment yields 2 figures,
which is what the submitted paper contains; ignoring it would yield 4 and cite
figures that are not in the paper.

**Testing:** `tests/fixtures/eprints/*.tar.gz` hold the real `.tex` files from
those three papers with figure binaries replaced by stubs (41-87 KB each, vs
2-9 MB for the originals). Real messiness, small repo.

---

## D12 - Figure rasterisation: PDF yes, EPS no

**Date:** 2026-08-07 · **Spec:** §3 Stage 3 (`pdftoppm` / ImageMagick)

What is actually installed on this machine decided this:

| Tool | Status |
|---|---|
| `pdftoppm` (poppler) | present |
| `pymupdf` | added as a dependency |
| ghostscript (`gs`) | **absent** |
| ImageMagick | **absent** |

So PDF → PNG works (`pymupdf` first, `pdftoppm` as fallback) and **EPS/PS cannot
be rasterised**. Rather than fail the paper, an unconvertible figure is kept with
`converted=False`: its caption is still useful to Stages 4-5, and
`usable_figures` excludes it so Stage 6 cannot select it as a visual. No sample
paper used EPS, so this is not currently costing us figures.

Raster figures are **copied, not re-encoded**, and keep their real extension. An
earlier version wrote every figure as `figNN.png` regardless of content, which
put JPEG bytes behind a `.png` name for 9 of 58 figures in the smoke run.

The PDF-extraction fallback (no LaTeX source available) needs three filters —
minimum pixels, minimum short side, and maximum aspect ratio. Without them a
real paper yielded mostly decorative slivers (median 1057×249).

---

## D13 - External signals: Hugging Face JSON API, not scraping

**Date:** 2026-08-07 · **Spec:** §3 Stage 3 ("scrape the page for the date range")

`https://huggingface.co/api/papers/<arxiv_id>` is an official JSON endpoint that
answers per paper. It beats scraping the daily page on three counts: no HTML
parsing to break, no date-range bookkeeping, and it returns `upvotes`, which is a
stronger signal than mere presence.

A 404 is a definitive "not featured". Any other failure sets `hf_lookup_ok=False`
and the rank prompt is told the signal is **unknown**, not negative — absent
evidence is not evidence of absence, and a paper should not be penalised for a
network blip.

No signal is load-bearing: every lookup degrades rather than failing the paper.

---

## D14 - Stage 4: one comparative call, constraints enforced in code

**Date:** 2026-08-07 · **Spec:** §3 Stage 4

**One call, not batches.** ~15 candidates fit in a single pass, and ranking is
inherently comparative — batched orderings cannot be merged without a second
pass. Input is abstract + intro + conclusion + figure captions + signals, never
full text; that bound is what keeps the stage inside its $2 target.

The prompt states the two hard constraints, but they are **also enforced in
code**, because a ranking that silently violates them produces a bad episode:

- **Subfield diversity** — at most 2 finalists per subfield. A paper deferred on
  diversity becomes the *first* substitute rather than being discarded; it was
  ranked higher and lost only on a structural rule.
- **No usable figure ⇒ cannot be a finalist** (`rank.require_figures`). The
  format is built around showing real figures.

Array order is trusted over the model's own `rank` field — models order more
reliably than they number, and the two sometimes disagree.

Unparseable JSON is converted to `StageError`, not left as `LLMError`: the CLI
catches `StageError` and prints a clear message, where an `LLMError` would reach
the user as a traceback. "Fail loudly" in the spec means legibly, not noisily.

~~**Not yet validated against a live model** — no API key is configured.~~
**Superseded by D16:** ranked live on 15 real candidates for $0.102. Logic
remains covered by 15 fake-LLM tests (hallucinated IDs, duplicates, truncated
output, total provider failure); D16 records what the live run showed.

---

## D15 - Cost: pricing verified, ceiling lowered, D6 corrected by measurement

**Date:** 2026-08-07 · **Spec:** §4

An API key was configured, so Stages 2 and 4 ran live for the first time. Three
things changed as a result.

**`pricing.yaml` is now verified**, not scaffold. The two models the pipeline
actually uses were checked against the model catalog and both were already
correct, so the tracker was never understating spend:

| Model | In / Out per 1M | Status |
|---|---|---|
| `claude-haiku-4-5-20251001` (shortlist) | $1.00 / $5.00 | confirmed |
| `claude-sonnet-4-5-20250929` (rank) | $3.00 / $15.00 | confirmed |

**`budget.ceiling_usd` lowered 20.0 → 2.0.** The ceiling is the only mechanical
brake on spend, and $20 exceeded the operator's whole-session budget. No stage is
projected above ~$0.95, so $2 leaves headroom without risking the cap. Note the
ceiling is **per run, not per session** — several runs can each spend up to it.

**D6's $0.77 shortlist projection was ~20% low. Measured: $0.921** on the same
1,225-paper window (62 calls, 440,796 in / 96,025 out). The projection
underestimated *output* tokens: the per-paper `rationale` field costs ~96k output
tokens across the window, which at $5/1M is $0.48 — over half the stage's bill.

A 3-batch probe run first measured $0.0149/batch and extrapolated to $0.92,
matching the full run's $0.9209 almost exactly. Probing a slice before committing
to a full window is the cheap way to price any future prompt change.

**This leaves the stage under its $1.00 target by 8%, not 23%.** The D6
mitigations are now closer to load-bearing: a heavier window (the 1,745-paper day
measured in D5) would breach the target outright. Shortening the `rationale`
instruction in `prompts/shortlist/v1.md` is now the cheapest lever, since output
tokens rather than abstracts are the marginal cost.

---

## D16 - Stages 2 and 4 validated live

**Date:** 2026-08-07

Full pipeline run on the real 1,225-paper window: 1,225 → 15 shortlisted → 15
enriched (14 with usable figures) → 3 finalists + 3 substitutes, for **$1.023**.

Two things the fake-LLM tests could not establish:

- **Fidelity holds.** The top justification cited "80% accuracy, 36 billion fewer
  tokens, 33% sparsity"; the paper's own figure caption reads "reaches 0.8 mean
  accuracy using 36B fewer natural language training tokens" and "At 33%
  sparsity". Numbers traced back exactly, with no hype words.
- **`require_figures` fired for real.** One shortlisted paper enriched to zero
  usable figures and was correctly kept out of the finalists while remaining
  available as a substitute.

**Known limitation found in the live run:** the diversity constraint compares the
model's *own* `subfield` tags, so near-synonymous labels defeat it — this episode
selected both `robot safety` and `generative safety`, which a viewer would read
as one subfield. Enforcing on free-text labels is inherently soft. If it matters,
the fix is a closed vocabulary of subfield tags in the prompt rather than more
code.

---

## D17 - Stage 5: the fidelity rule needs two checks, not one

**Date:** 2026-08-07 · **Spec:** §1 (factual fidelity), §3 Stage 5

The spec's fidelity rule is that every reported number is traceable to the paper.
The prompt asks for a citation on every `headline_result`, and the stage drops
any result that arrives without one.

**That check is not sufficient, and a live run proved it.** The first real
extraction produced a result citing `"Section 5, Table 1"` — a real section and a
real table — and then reported five percentages of which **three
(`88.5`, `93.8`, `96.5`) appear nowhere in the paper**, confirmed against the
raw LaTeX source. Citing a real location and inventing its contents passes a
citation check completely.

So the rule is now enforced in two parts:

1. **Citation present** — a result with an empty `source` is dropped.
2. **Numbers verifiable** — every distinctive number in `numbers` must appear in
   the paper text, or the result is dropped and the drop is logged loudly.

Matching is by substring, which is what makes it usable rather than brittle: a
legitimately rounded `"80"` matches the paper's `"80.4"`, while a fabricated
`"88.5"` matches nothing. Only numbers with a decimal point or 3+ digits are
checked — small integers ("3 models", "5 tasks") appear everywhere and would
only add noise.

**Verification is skipped when the full text is unavailable** (PDF-fallback
papers, where only intro+conclusion exist). A fragment cannot disprove a number,
and dropping legitimate results because we failed to fetch the source would be
the wrong error. The stage warns instead. Same principle as the Hugging Face
lookup in D13: absent evidence is not evidence of absence.

After the fix, re-running the same three papers gave **12 of 12 results fully
traceable**. `extract.verify_numbers` turns the check off if it ever proves too
aggressive.

**This required a Stage 3 change.** Stage 5 needs the whole paper, but enrichment
deleted the e-print source after parsing, so the text could not be recovered. Enrich
now writes `enrich/<id>/fulltext.txt` (free — no LLM call). It is capped at
`extract.max_fulltext_chars` (90k) when sent, because papers run from ~17k to
~97k characters here and the tail is usually appendices.

---

## D18 - Stage 6: per-segment calls, wrapper failure is not fatal

**Date:** 2026-08-07 · **Spec:** §3 Stage 6

**One call per paper, plus one for the episode wrapper.** Segments are
independent, and a per-paper call keeps each prompt focused on a single digest
rather than asking one call to hold three papers at once. The wrapper is a
separate prompt (`prompts/episode/v1.md`) because it needs all three digests
together and produces a different shape.

**A failed wrapper degrades rather than fails the run.** The segments are the
substance; a title and description can be written by hand, so a wrapper failure
returns empty metadata and logs it. A failed *segment*, by contrast, is a real
loss and is reported.

Two things enforced in code rather than trusted to the prompt, mirroring D14:

- `figure_file` must name a figure Stage 3 produced. An invented path would
  render as a missing image, so it is downgraded to a bullet slide.
- Narration is scanned for LaTeX and markdown, which a TTS voice reads aloud
  literally. Detected leakage is warned about rather than silently shipped.

`est_seconds` is the model's own estimate and is explicitly **not** authoritative
— Stage 8 must build its timeline from measured audio (spec Stage 7). It exists
to pace the script.

Markdown scripts are written next to the JSON (`segment_<id>.md`, `episode.md`),
because judging pacing and wording means reading prose, not JSON.

**Measured on the real episode:** 3 segments, 284s total (4.7 min, inside the
spec's 3-5 minute target), $0.090 against a $3.00 target. Narration came back
correctly spoken — "twenty-seven percent", "GPT-four-oh", "Gemini two point five
Flash" — with no LaTeX leakage and a real figure on the result scene.

---

## D19 - Stage 7 TTS: macOS `say`, with the spec's OpenAI default wired behind it

**Date:** 2026-08-08 · **Spec:** §3 Stage 7 (DECIDE) · **Resolves D3**

The spec's default is OpenAI `tts-1`. **`OPENAI_API_KEY` is still empty**, so that
default remains unavailable, and Anthropic — the one provider with a key — offers
no TTS API. Rather than block Phase 4 on a credential, Stage 7 ships with macOS
`say` as the working engine.

| Engine | Cost | Status |
|---|---|---|
| `macos` (`say` + `afinfo`) | $0.00 | **default**, works today, offline |
| `openai` (`tts-1`) | ~$15/1M chars | wired, raises a clear error until a key exists |

`say` is not as good as a hosted neural voice, and the owner should hear it
before committing (see the go/no-go note below). But it satisfies the one hard
requirement of this stage: it produces real audio whose **measured** duration
drives the render timeline. Swapping to OpenAI is a config change
(`tts.provider`), not a refactor — which is what D3's interface-first approach
bought.

`measure_duration()` uses `afinfo`, falling back to `ffprobe`, and **raises rather
than estimating**. A wrong duration silently desynchronises the whole episode,
which is worse than a loud failure.

**Measured on the real episode:** 22 scenes, 4,688 characters, 267s of audio,
**$0.00**. Scripted estimate was 263s against 244s measured for the segments —
the script prompt's 150 wpm budget runs about 7% slow versus this voice. Drift is
logged per run so the prompt can be tuned against it.

**Cost note:** `macos/say` is listed in `pricing.yaml` at $0.00 rather than left
absent. Absent means "price unknown" and would show in the `unpriced` count;
this engine is known-free, which is a different statement.

---

## D20 - Stage 8 render: Pillow + ffmpeg directly, not Remotion or moviepy

**Date:** 2026-08-08 · **Spec:** §3 Stage 8 (DECIDE)

Three options were on the table. Chosen: **Pillow for slide composition, ffmpeg
invoked directly for assembly.**

| Option | Why not |
|---|---|
| Remotion | Best text layout, but adds an entire Node/React toolchain beside a pure-Python codebase, plus a ~500MB dependency tree, to draw what is deliberately a very simple visual system |
| moviepy | Stays in Python but is a heavy wrapper over ffmpeg with its own failure modes; we need direct ffmpeg control anyway |
| **Pillow + ffmpeg** | One small dependency, direct control, and slides become pure functions that are testable as images without rendering video |

**Sync comes from construction, not correction.** Audio and video are built from
the *same* list of measured durations: the audio track concatenates the per-scene
clips, and the video track is a concat-demuxer list holding each slide for exactly
that scene's measured length. Drift cannot accumulate. Measured on the first
segment: video 77.200s vs audio 77.273s — **73ms, about two frames at 30fps.**

Two details that are load-bearing rather than cosmetic:

- **Figures sit on a light card.** Paper figures are drawn for white paper; on a
  dark background their black axes and text vanish. Transparent PNGs are
  flattened onto the card, not onto black, for the same reason.
- **The concat demuxer drops the final image** unless the last file is repeated
  without a duration. Without that the last scene renders as a frozen frame of
  the wrong slide.

Attribution ("Figure 1, arXiv:2608.05715") is burned into the card, and the arXiv
id sits in the footer of every slide, so provenance is on screen throughout.

**ffmpeg was installed** (`brew install ffmpeg`, 8.1.2 with libx264/aac),
clearing the blocker carried since Phase 1. Its absence is now reported as a
clear `StageError` rather than a traceback.

**Deferred:** crossfade transitions (spec says "simple crossfade"; the first cut
uses hard cuts) and the music bed. Neither blocks the go/no-go review.

---

## D21 - Phase 4 review pass: voice ceiling, visual chrome, crossfades, music

**Date:** 2026-08-08 · **Owner review of the first rendered segment**

Owner verdict on the go/no-go segment: narration "a bit robotic, too monotone";
also asked for a visual pass and the deferred polish.

### Voice: the local engine is capped, and this machine has only legacy voices

`say -v '?'` lists **no Premium, Enhanced or Siri voices installed**. The en_US
set is Albert, Fred, Junior, Kathy, Ralph, Samantha plus novelty voices (Boing,
Bubbles, Zarvox). Samantha — what shipped in the review cut — is the best of
them. **No amount of tuning fixes this locally**; the monotone is the engine.

Three ways out, none of which this session could take unilaterally:

| Path | Cost | Blocker |
|---|---|---|
| macOS Premium voices (e.g. Ava, Zoe) | free | GUI download: System Settings → Accessibility → Spoken Content → System Voice → Manage Voices |
| OpenAI `gpt-4o-mini-tts` | ~$0.07/episode | `OPENAI_API_KEY` is empty |
| ElevenLabs | ~$0.47/episode | `ELEVENLABS_API_KEY` not set |

All three are now one config line away. `elevenlabs` was added as an adapter
(the alternative the spec names), the OpenAI default moved from `tts-1` to
`gpt-4o-mini-tts` (newer and more natural — the whole point of leaving the local
engine), and `tts.rate` was added, defaulting to 165 wpm because `say`'s ~175
reads as rushed.

### Visual system

Every slide now shares one chrome — paper-title eyebrow top-left, arXiv id
bottom-left, segment progress bar bottom-right — so the frame reads as a system
rather than four unrelated layouts. Also: optical centring on title cards, a
centred rule anchoring the result callout, an optional caption line under figure
cards, and `SlideContext.scaled()` so every 1080p-designed measurement survives a
different output size (previously absolute offsets landed in the wrong places at
any other resolution).

### Crossfades — and the bug they introduced

0.4s cross-dissolves, on by default. Each slide is held for its measured duration
**plus** the fade, and each xfade consumes exactly that overlap, so runtime is
preserved.

The first implementation got the arithmetic wrong: it accumulated `d - fade` per
step, subtracting the fade once *per transition* rather than once against the
cumulative timeline. On a 7-scene segment that lost 6 x 0.4 = **2.0 seconds**, and
because ffmpeg was invoked with `-shortest`, it silently truncated the last two
seconds of narration rather than erroring. Measured: 75.27s output against 77.27s
of audio. Fixed to `offset = cumulative - fade`; re-measured at 77.267s video
against 77.230s audio (37ms, about one frame). Two regression tests pin the
offsets and the duration identity.

### Music bed: supported, defaulted off

**Off by default**, which is a change from the first cut. The renderer prefers a
real track at `assets/music/` (looped, trimmed, faded, levelled) and falls back
to a synthesised low triad.

The fallback is the problem: measured at **-56.5 dB mean against narration at
-15.7 dB**, it is inaudible; raised to where it registers, a sine drone reads as
hum rather than ambience. Shipping that on by default would make the episode
worse. No royalty-free track could be sourced here, so the honest arrangement is
full support, a documented drop-in path (`assets/music/README.md`), and the flag
off until a real track exists. Default level raised to -18 dB so it is actually
audible when enabled.

---

## D22 - Stage 7 voice: ElevenLabs "Bella". Local TTS was rejected twice

**Date:** 2026-08-08 · **Spec:** §3 Stage 7 (DECIDE), §8 Q2 (ASK) · **Owner decision** · **Supersedes D19**

Two local engines were tried and rejected by the owner as robotic:

1. `Samantha` (compact) — the only decent voice installed at the time.
2. `Ava (Premium)` — after the owner downloaded Apple's neural voices. Still
   rejected, which settled it: **Premium is the top macOS tier**, so `say` had
   nothing better to offer and no rate or pause tuning fixes timbre.

Owner chose **ElevenLabs `eleven_turbo_v2_5`**, voice **Bella**
(`EXAVITQu4vr4xnSDxMaL`), picked from six library voices sampled on the same
narration line (`runs/_voice_samples/`). Measured: **$0.4688 per episode** for
4,688 characters, matching the estimate exactly.

Two things worth recording:

- **The key lacks `voices_read`.** Synthesis works; enumerating the account's
  voices returns 401. That is why the candidates are public library IDs rather
  than the owner's own saved voices.
- **Bella's pacing validated the script prompt.** Measured 267s against a 263s
  scripted estimate (**+1%**), versus -7% for Samantha. The 150 wpm budget in
  `prompts/script/v1.md` was calibrated closer to a neural voice than to a
  compact one all along, so Stage 8 now absorbs almost no drift.

The `macos` adapter stays as a free offline fallback, and `openai` remains wired.

---

## D23 - Stage 9 packaging, and a cost report that was lying on every resume

**Date:** 2026-08-08 · **Spec:** §3 Stage 9, §4

`output/` now holds the upload-ready bundle: `episode.mp4`, three standalone
`segment_<id>.mp4`, `thumbnail.png`, `metadata.json`, `cost_report.json`.

**Chapters come from measured audio**, the same durations Stage 8 built the
timeline from, so they land on the cuts rather than near them. The list is
appended to the description in YouTube's `M:SS Label` form, and the stage warns
if the first chapter is not at 0:00 — YouTube silently ignores the whole list
otherwise.

The **thumbnail is composed from the same Pillow primitives as the slides** (spec
Stage 9: no image model in v1) — the model's first thumbnail text option, the
top-ranked paper's best figure on a light card, 1280x720, 182 KB against
YouTube's 2 MB limit.

**The bug this stage exposed.** Packaging reported `total_cost_usd: 0.0000` while
`calls.jsonl` held 158 calls worth $1.9615. `CostTracker` only ever held records
from the *current process*, so `write_report()` rebuilt `cost_report.json` from
whatever ran this invocation and discarded the rest.

That was not a packaging problem. **Every `--from <stage>` resume had been
writing a partial report** — the extract+script rerun wrote $0.23 over a run that
had actually spent $1.49. `calls.jsonl` was always correct, exactly as D8
promised; the report derived from it was not. Worse, the ceiling was being
checked against a fraction of true spend, so a resumed run effectively got a
fresh budget each time.

Fixed by seeding the tracker from the append-only log on construction. Three
regression tests cover it: a resume carries prior spend, the ceiling counts it,
and a truncated final line from a killed run is skipped rather than fatal.

The corrected report for this run:

| Stage | Calls | Cost |
|---|---|---|
| shortlist | 62 | $0.9209 |
| rank | 1 | $0.1020 |
| extract | 6 | $0.2943 |
| script | 8 | $0.1755 |
| voice | 81 | $0.4688 |
| **total** | **158** | **$1.9615** |

Extract, script and voice show repeated passes because each was re-run during
development — voice three times, once per engine tried. Only the ElevenLabs pass
was billed; the two local passes were free. That is accurate history, not
double-counting.

---

## D24 - Stage 10 upload: dry-run is the product, the live path is the appendix

Stage 10 is the first stage that cannot be finished by writing code. Uploads from
an unverified Google Cloud project are locked to private until the project passes
a YouTube API compliance audit — 2–4 weeks of someone else's calendar. So the
stage was built for the state it will actually be in for the next month: off.

**Dry-run is not a stub.** With `upload.enabled: false` (the checked-in default)
the stage resolves the real bundle, applies every YouTube-side limit, computes
the real `publishAt`, and writes the exact `videos.insert` body it would send to
`output/upload.json`. Enabling uploads adds the network call and nothing else.
Run against the 2026-08-07 episode it produced a reviewable body in one pass:
11.5 MB `episode.mp4`, private, `containsSyntheticMedia: true`, category 28.

**Scope is `youtube.upload` and nothing else.** The spec asks for the narrowest
scope, and a narrow scope is an easier audit. It covers `videos.insert` and
`thumbnails.set` but is *not* documented to authorise `videos.list` — which the
spec also asks for, to poll until processed. Rather than widening the scope for a
verification step, a 403 on the poll records `processing_status: "unverified"`
and warns. The upload already succeeded by then; failing there would be a lie
about what happened.

**Write ordering is the real design.** `insert` → **write `upload.json`** →
thumbnail → poll → rewrite. The video ID hits disk the instant it exists, before
anything else can throw, because the failure this stage must never produce is a
published video whose ID was lost — that is precisely what makes the next run
publish the episode a second time. Everything after `insert` degrades to a
warning: a missing thumbnail or an unconfirmed poll is not worth failing a run
that already put a video on YouTube. A test kills the process between `insert`
and the poll and asserts the ID survived.

**The audit's symptom is detected, not just documented.** An unverified project
accepts the upload and silently drops `publishAt`. If the video comes back with
no scheduled publish time, the stage says so in as many words, because otherwise
a green run means a video that will never publish.

**Late runs still publish that day.** If a run finishes after the 12:00 ET slot,
scheduling in the past is not an option and skipping the day is worse, so it
schedules `late_publish_grace_minutes` (default 15) out and warns. The 2026-08-07
bundle is two days stale, so the first real dry-run exercised exactly this path.

**Dependencies stay optional.** `google-api-python-client` and friends live in a
`[youtube]` extra, imported lazily. The dry-run path and all 37 new tests import
none of them; the client's retry and resume logic is tested against a fake
`googleapiclient` injected into `sys.modules`, and the stage's policy against a
fake client. The suite cannot publish a video even by accident.

Also added: `pipeline youtube-auth`, the one-time interactive OAuth flow that
prints the refresh token every later run consumes non-interactively.

---

## D25 - Scheduling: everything in GitHub Actions, and the window marker starts moving

The spec left this a DECIDE with a fallback: run it all in Actions, or split
stages 1–7 into Actions and render locally via `make render`, and **ASK before
committing to a paid runner**.

**The ASK is not triggered — the repo is public, so Actions minutes are free and
unlimited.** That removes the only real argument for the split. The other one
was that render might be too heavy: a full run is ~10 minutes wall clock, most of
it arXiv rate limiting, against a 6-hour job limit. And the split has a cost the
spec does not mention — it needs a human at a particular laptop twice a week,
which is the opposite of the zero-touch goal in §1. So: **one workflow, all ten
stages, on `ubuntu-latest`.**

**Linux portability was the actual risk, and it was not hypothetical.** The
renderer picks a font from a candidate list and falls back to Pillow's built-in
bitmap face when it finds nothing — without raising. On a runner with no DejaVu
that produces a complete, uploadable episode set in tiny unreadable type. The
existing suite could not catch it: it fakes ffmpeg and never asserts which font
it got. `tests/test_portability.py` now checks that the resolved font is a real
scalable face, that a title card draws actual text, that ffmpeg encodes a slide
into a measurable clip, and that libx264 is compiled in. Both workflows install
`ffmpeg` and `fonts-dejavu-core` explicitly rather than trusting what the runner
image ships this month, and CI asserts both are present so those tests cannot
quietly skip.

**Two workflows, split by whether they can spend money.** `ci.yml` runs on every
push with **no secrets at all**, so it cannot bill anything by accident.
`episode.yml` holds the keys.

**The schedule is opt-in.** Each run spends ~$1.73, and a cron that starts
billing the moment it merges is not something to switch on for someone. The
scheduled job is gated behind a repo variable — `gh variable set EPISODE_ENABLED
--body true` — while `workflow_dispatch` always works. Same shape as
`upload.enabled` in D24: ship it off, let the owner turn it on. 13:00 UTC (09:00
ET) leaves ~3 hours before Stage 10's noon publish slot, which Actions' cron
drift comfortably fits inside.

**Artifact upload is not a nicety.** Stage 10 is still in dry-run, so a run that
does not hand the bundle to a human produces nothing anyone can publish. The
episode bundle is uploaded on success (90 days); on failure a much smaller set of
JSON diagnostics goes up instead of gigabytes of extracted figures.

### The window marker now advances — reversing the earlier position

`state.json` was previously pinned with the note "should move only once an
episode is actually published." That was right when publishing looked imminent.
It is wrong now: publishing is blocked on a compliance audit with no date, and a
Tue/Thu schedule against a fixed 4-day fallback window means **Thursday's run
re-covers Monday and Tuesday** — the same paper can headline two consecutive
episodes. Waiting for the audit guarantees that bug on every scheduled run, so
the marker now advances on a **packaged** episode rather than a published one.

Two details that matter more than they look:

- **It moves to the fetch window's `end`, not to "now".** A run takes ~10
  minutes; papers submitted during it fall between the two, and anything skipped
  is never covered again. Stage 1 records the window it actually fetched to
  `fetch/window.json` for exactly this.
- **Only a run that earned it moves the marker.** `--from script` reuses a cached
  fetch and has covered no new window; `--paper` is a single-paper rebuild. Both
  leave it alone, as does any run that never reached a packaged episode.

Persistence in CI is `actions/cache` with a rolling key, saved only on success. A
cache miss degrades to the 4-day fallback — some overlap, not breakage — which is
why this is a cache rather than a commit back to the repo.

---

## Craft backlog — owner review of the first episode, 2026-08-10

Six notes from watching the 2026-08-07 episode back. None are bugs; the pipeline
does what it was asked to. They are all the difference between "a pipeline that
produces a video" and "a video worth watching", which is criterion 4 in §1 and the
one least served so far. Where a note could be checked against the real script
output, it was — the findings are below the note.

**1. Thumbnails are not good enough.** The Claude-designs / Pillow-draws route
(explored 2026-08-10) produced better hooks — `"Sticky note attacks"` became
`"Robots obey sticky notes"` — and fixed two real placement bugs, but the owner's
verdict on the result is that it still is not good. The code sits uncommitted in
`pipeline/render/thumbs.py` with `prompts/thumbnail/v1.md`, wired into nothing;
Stage 9 still ships the original. The untried path is an actual image model
(Gemini 2.5 Flash Image, or `gpt-image-1`) doing image-to-image over the real
figure, which is blocked only on a key. Worth keeping from the exploration
regardless of direction: the focal-point crop and the 45%-retention rule that
falls back to fitting a figure rather than cropping it into fragments.

**2. The intro and outro are thin.** Verified: the cold open is three flat
sentences, one per paper, and the outro is two scenes ending on "Thanks for
watching." There is no series identity, no framing of why these three papers, and
no reason to subscribe. Lives in `prompts/episode/v1.md`. Note this collides with
the open question in §8 Q3 — the channel has no name, so there is nothing for an
intro to introduce.

**3. Nothing bridges the cold open into the first paper.** The cold open's last
line ends and segment one's title card begins, hard cut. The episode prompt is
documented as producing "transitions" but nothing consumes them. Needs a decision
about whether the bridge is narration, a visual device, or both — Stage 6 and
Stage 8 respectively. **Done — D26. The answer was both.**

**4. Segments end on a caveat, not a conclusion.** This one has a clear root
cause. `prompts/script/v1.md` says to close "with the caveat **or** 'why it
matters'", and the model took the caveat every time: all three segments end on a
`bullet_slide` of limitations, then cut straight to the next paper. So each paper
finishes on its weakest note. The fix is to stop offering the choice — require the
caveat *and then* a one-line close. **Done — `prompts/script/v2.md`, taken
together with note 3: this is what the bridge leaves *from*.**

**5. Narration is monotonous.** One ElevenLabs voice at one pace with no prosody
variation across ~5 minutes. Two independent levers: Stage 7 (per-scene voice
settings, or a second voice for the wrapper) and Stage 6 (sentence-length rhythm,
which is currently uniform because the prompt budgets words per scene).

**6. It looks like one PowerPoint deck.** The sharper version of this: the *slide
types* are actually well distributed across the episode — 8 title cards, 7
figures, 7 bullet slides, 5 result callouts — so the monotony is not in the mix.
It is that all 27 scenes share one palette, one accent, identical chrome, and no
motion whatsoever. Candidate directions, roughly in order of effort: a per-paper
accent so each segment reads as its own chapter; slow pans or scale on figure
slides so the frame is not frozen; a distinct treatment for the wrapper scenes so
the episode has punctuation. `pipeline/render/slides.py` and Stage 8.

---

## D26 - Transitions: a bridge is narration *and* a cut, so it is both

Backlog note 3 asked whether the bridge between parts should be narration, a
visual device, or both. It is both, because there are two distinct hard cuts at
every seam and fixing either one leaves the other standing:

- a **narrative** cut — a thought finishes and an unrelated one starts, with
  nothing connecting them;
- a **visual** cut — one frame is replaced by the next in a thirtieth of a
  second, three times per episode.

The spec has asked for transitions since Stage 6 was written (§3, "Also generate:
episode cold-open, transitions, outro"). Nothing produced them, so nothing
consumed them and the omission was invisible.

### A transition is an episode-level part, not part of the segment

The tempting implementation is to prepend the bridge line to the next segment's
first scene. That breaks the standalone requirement: spec Stage 6 says each
segment must be publishable on its own for Shorts repurposing, and a segment that
opens with "that attack needed a camera pointed at the world" cannot be. The same
argument rules out appending it to the previous segment.

So a transition is its own part, exactly like the cold open and the outro — its
own scene, its own audio clip, its own mp4 — stitched into the episode and absent
from `segment_<id>.mp4`. It also means `--paper` re-runs skip them for free.

One per paper **including the first**, which is the seam the review actually
complained about. There is deliberately none between the last paper and the
outro: closing the loop is the outro's whole job, and a bridge into it would say
the same thing twice.

The bridge into a paper plays *before* it and belongs to that paper's chapter, so
clicking a chapter lands on the sentence that sets the paper up rather than on
its title card.

### The seam cross-fade costs a re-encode, and that is the whole tradeoff

Within a segment, slides have cross-dissolved since D21. Between parts, the
episode was assembled with a stream-copy concat — free, and correctly so, since
every part comes out of this pipeline with identical codec settings. But a stream
copy and a filtergraph are mutually exclusive, and dissolving a seam needs a
filtergraph. `render.episode_crossfade_seconds` is therefore a real cost knob:
0.6s by default, 0 restores the copy. Seam fades are longer than the 0.4s
within-segment fade so a part boundary reads as a section break rather than as
one more scene change.

**The padding direction is the subtle part.** The episode's audio is a plain
concat of the parts, so nothing may move on the audio timeline; the video has to
arrive at each seam at exactly the un-faded elapsed time. D21's within-segment
path pads each clip at the *end*, which is right there because its inputs are
still images — a frozen frame shifted by 0.4s is the same frozen frame. Reusing
that here would slide every part 0.6s early against its own narration, because
these inputs are real video. The fix is to pad each part after the first at the
**start**, with `fade` seconds of its own frozen first frame: the xfade consumes
exactly the pad, so part *i*'s real content still begins at `sum(durations[:i])`.
Verified against ffmpeg with coloured parts — the dissolve is centred on the
boundary and the next part's content starts on it, to the frame.

That invariant is what lets Stage 9's chapter marks stay exact, and it is why
`_xfade_filter` is shared by both scales rather than duplicated: the arithmetic
is identical, only what gets padded differs. It now consumes `[v0]..[vn]` labels
that callers supply, replacing a string-replacement pass over the filtergraph.

Audio is concatenated, never `acrossfade`d. A 0.6s audio dissolve at a seam would
eat the last syllable before it.

Failure is contained at every level: a seam cross-fade that ffmpeg rejects falls
back to the hard-cut concat rather than losing the episode, a part shorter than
two fades disables the effect, and a transition that fails to script or narrate
leaves that one seam as the hard cut it is today.

### A bad bridge must not cost the title

Transitions come back from the same call as the title, description and thumbnail
options. Validating them as part of `EpisodeMetadata` would mean one malformed
bridge — an `est_seconds` of 0 is enough — degrades the whole wrapper to empty.
They are parsed individually instead, then filtered to one per paper and sorted
into play order, since neither is something the prompt can guarantee.

### The card

The visual half is a distinct slide type, and the only one that does not sit on
`BG` — a chapter marker, the next paper's angle in two to five words, and a rule
running most of the frame. On the standard background it would read as one more
title card, which is precisely the "one long PowerPoint deck" of backlog note 6;
at full accent strength it is a flashbang held for five seconds. It sits at 22%
of the accent over the background.

The card text is composed in code from the transition's `label` rather than
described by the model. The model writes the line that is *spoken*; there is
nothing for it to decide about the frame, and one fewer field is one fewer thing
to validate.

### Cost and runtime

One transition adds ~5s of narration to Stage 7 and ~13 words to the Stage 6
wrapper call, so three of them are a rounding error against the ~$1.30 episode.
The seam cross-fade re-encodes the finished episode once — the only material
addition, and the reason it is switchable.

---

## D27 - The visual system goes light, and colour becomes per-paper

Owner review of 2026-08-12: "still quite dull, too dark, and too monochromatic".
Both halves of that are departures from the spec's Stage 8 wording ("dark
background, single accent colour"), so both are recorded here rather than
quietly changed.

**Ground: near-black → paper white (`#F7F6F3`).** The dark ground was chosen
before there was an episode to watch; across five minutes it read as heavy. The
concrete argument for flipping is the figures: paper figures are drawn for white
paper, and the light card they sat on existed purely to rescue them from a ground
they clashed with. On a paper ground the card stops doing rescue work and becomes
an edge marker — which is why it now carries a hairline rule instead of relying
on contrast. Four directions were rendered against real slides before choosing.

**One accent → one accent per paper.** This is the part that actually answers
"monochromatic". Backlog note 6 established that the *slide mix* was already well
distributed; the monotony was 27 scenes sharing one hue. Papers now take rust,
teal and indigo by running order, and a bridge wears the accent of the paper it
introduces, so the next chapter's colour arrives a beat before the chapter does.

The cold open and outro stay a neutral graphite. They are the frame around the
papers rather than papers themselves, and giving the wrapper a colour of its own
would imply a fourth chapter.

Two details worth keeping:

- **The letterbox colour was hardcoded** to the old near-black in the ffmpeg
  scale filter. Any non-16:9 render would have framed every slide in a colour the
  visual system no longer contains; it now derives from the palette.
- **The accent index comes from the running order, not the render loop**, so a
  `--paper` rebuild produces the same colour the episode gave that segment. Keyed
  off the loop it would have coloured every single-paper rebuild rust.

## D28 - Narration: the settings that were never sent, and where pauses live

"Still sounds quite robotic, maybe even more so." Two causes, both real:

**`voice_settings` were never sent.** The ElevenLabs client posted only `text`
and `model_id`, so every line ran at the voice's default `stability`, which is
high — consistent and flat, which is exactly the complaint across a five-minute
read. They are now configurable and set to `stability: 0.40`.

**`eleven_turbo_v2_5` is the latency-optimised model.** Nothing in a batch
pipeline needs low latency. Now `eleven_multilingual_v2`.

### Pauses live in the audio file, not the timeline

The owner asked for more pausing. Three mechanisms were possible and the choice
matters:

- `<break>` tags in the narration. **Measured on this account, not assumed:**
  they work — 1.25s of speech becomes 7.34s with two three-second breaks — but
  only above roughly half a second. A 0.6s break changed a real line by 46ms,
  because the pause a full stop already produces absorbs it. Ellipses and em
  dashes do nothing at all.
- A gap inserted in the render timeline. Rejected: Stage 8 derives both of its
  tracks from what Stage 7 measured, so a gap added there has to be added twice
  and kept in sync forever.
- **Silence appended to each clip in Stage 7. Chosen.** The clip is measured
  after padding, so the gap flows through the timeline, the chapter marks and the
  runtime with no arithmetic anywhere else. It is deterministic, unlike anything
  that depends on how a given model interprets a tag. And the beat lands exactly
  where the slide changes, so it is a visual rest as well as an audible one.

### A duration bug this uncovered, and the chapter drift it explains

`measure_duration` tried `afinfo` before `ffprobe`. `afinfo` reports an
*estimated* duration and runs about 0.25% long on MP3 — 30ms per clip, invisible.
Across the 29 clips of the 2026-08-12 episode it summed to **1.09s of timeline
that did not exist in the files**, which is precisely the drift that pushed the
late chapter marks past their true positions (flagged as "known drift" after that
run, and wrongly attributed to AAC quantisation alone). `ffprobe` reads the
container and is now preferred, with `afinfo` kept as the fallback for a machine
without ffmpeg.

## D29 - Length: a cap in code, because a budget in a prompt is a suggestion

The 2026-08-12 episode ran 6:22 against a 3-5 minute spec. `script/v2` asked for
5-8 scenes and a 75-second target; it got 8, 7 and 7 scenes at ~110s each. The
prompt had stated the budget correctly since v1 and been ignored three times.

So the scene count is now enforced in Stage 6 rather than requested. **How** it
is enforced is the decision: trimming from the end takes the close, which is the
one scene a segment must not lose (D26 note 4), and trimming from the front takes
the title card the standalone cut requires. The cap keeps the head up to the
limit and **always preserves the last two scenes** — caveat, then close — so an
over-long segment loses its middle and keeps its shape.

Word count is reported, never cut. There is no safe way to shorten a sentence in
code, so exceeding the budget by more than 25% logs an error naming the prompt as
the thing to fix.

The owner chose ~4:30 over a more aggressive 3:30, keeping segments substantive.

## D30 - The series has a name, and the cold open has a job

Resolves §8 Q3, open since the spec was written: the channel had no name, so
title cards carried the paper's hook and nothing identified the series.

It is **ML Papers of the Day** (owner, 2026-08-13).

The cold open now runs in three beats — name the series, name the thread the
three papers share, then tease each one — rather than three hooks in a row with
no framing. The outro returns to that thread. The prompt is explicit that if the
honest answer is "same field, nothing more", it should say something true and
small rather than invent a theme.

**Register: light persona.** Chosen from three options against the owner's
reference channels. Structural borrowing — open on a gap in what the viewer
believes rather than on the finding, second person, deliberately varied sentence
length — plus a consistent presenter warmth and the series name. Explicitly **no**
catchphrases and no host character; the prompt names and bans the obvious
borrowed signatures, because pastiche was the main risk in leaning any further.

The varied-sentence-length rule does double duty: it is also where the pauses
come from inside a scene, since punctuation is what a synthesised read breathes on.

## D31 - The window marker follows arXiv's index, not the clock

The 2026-08-13 run failed at Stage 1 with "arXiv returned no papers". The window
was empty, and the reason was not that no papers existed.

**arXiv's search index lags real time.** Probed live across six windows: the
newest indexed submission was `2026-08-12T17:58Z` in *every* one of them,
including a seven-day lookback — the index had not advanced in roughly thirty
hours. Meanwhile D25 had moved the marker to the previous run's requested window
end, `2026-08-13T02:27Z`, because that is when the run happened.

So the marker sat **8.5 hours past the newest paper that existed**, and the next
window ran from there to now: a range containing nothing.

The empty fetch is the harmless symptom. The real defect is silent: every paper
submitted between 17:58 and 02:27, once the index caught up, would have been
skipped permanently — the marker was already past them. That is exactly the
failure D25 set out to prevent, and it went one level deeper than D25 looked.
D25's reasoning ("papers submitted *during the run* fall between end and now")
was right and insufficient; the gap between the index and the clock is much
larger than the gap between the fetch and the end of the run.

**The marker now advances to `max(submitted)` over the papers actually
returned.** That is the only frontier the run has evidence for: everything up to
it has demonstrably been covered, and everything after it has not been seen, so
the next window starts exactly where the evidence stops. The requested end is
still recorded alongside it as `requested_end`, for diagnosing lag.

`state.json` was rewound 8.5 hours to `2026-08-12T17:58:07Z`, and
`runs/2026-08-12/fetch/window.json` backfilled to match what the code now writes.

Worth noting for the scheduled runs: this makes a run during an index stall fail
loudly with an empty window rather than quietly skipping a day of papers. That is
the right trade, but it means a Tue/Thu cron can fail for reasons that have
nothing to do with this code.

## D32 - A ledger of covered papers, because the marker is not a guarantee

D31 made the window marker correct. It still is not *sufficient*, and the
distinction matters: the marker prevents overlap only while windows tile
perfectly. The moment one is rewound — after an index stall, a failed run, a
manual replay — nothing stops a paper that already carried a segment from being
ranked into another episode. The 2026-08-13 index stall forced exactly that
situation: the only way to make an episode was to re-open a window whose best
three papers had already aired.

So `state.json` gains `covered_papers`: every arXiv ID that has had a segment in
a packaged episode. It is written at the same moment the marker advances, by the
same rule — a run that earned the marker earned the ledger entry — so the two
cannot disagree about what a run covered.

**Filtered at Stage 2, not Stage 4.** Ranking is where a repeat would actually do
damage, but shortlisting is where it is cheapest to prevent: a paper that cannot
become a finalist is not worth paying a model to score. Exclusion at the entry to
the funnel also means the whole downstream pipeline is unaware the mechanism
exists.

Bounded at 400 entries. This file is a cache entry, not an archive; at three
papers an episode that is well over a year, and a paper old enough to fall off
the end is not one a viewer would recognise as a repeat. A cache miss degrades to
"no history" — some risk of repetition, not breakage — which is the same
degradation the marker already has.

Emptying the window entirely is a hard failure rather than a silent one: if every
paper in a window has been covered, Stage 2 says so and names the two ways out
(widen the window, or set `shortlist.exclude_covered: false`). Shortlisting
nothing would otherwise surface three stages later as an unrelated error.

Backfilled from the two existing episodes, and the marker deliberately rewound to
`2026-08-09T02:27Z` to re-open that window for a third.

## D33 - Captions and motion: built, measured, and switched off

Two changes to Stage 8, taken together because they answer the same objection:
static slides with no on-screen text are how a channel reads as machine-made and
gets swiped.

**Both are off (owner, 2026-08-14).** They were built on by default, validated
against the 2026-08-13 episode, and rejected on sight. The mechanism works and
the numbers below hold; what was wrong was the result on screen. The code stays
behind two config flags rather than being torn out, because the objection they
answer has not gone away — the next attempt should reuse the timeline machinery
and rethink the *look*, which means the caption's container and the band it
takes out of the slide, not how the cues are timed.

Recorded in full because the reasoning is what the next attempt needs.

**Why captions at all.** Most mobile viewing is muted, so an episode without
them plays as a wordless slideshow for most of its audience.

The timing is *derived, not measured*. `captions.cues_for` splits a scene's
narration into ≤36-character phrases and gives each a share of that scene's
**measured** duration proportional to its length in characters. ElevenLabs will
return character-level alignment from `/with-timestamps`, and it is the better
signal - but the macOS and OpenAI adapters cannot, and a caption track that only
lines up on one engine is worse than one that is a fraction of a second loose on
all three. The approximation is bounded: cues tile each scene exactly, so every
scene boundary re-syncs against measured audio and error cannot accumulate down
a segment. Moving to real alignment later changes `cues_for` and nothing else.

**One line, and the slides give up a band for it.** Two-line captions would need
a fifth of the frame reserved on every slide, and D27 made figures the star.
`SlideContext.caption_band` reserves ~16% instead; the chrome lifts out of it and
the figure card shortens, which is why the band is a property of the context
rather than something the caption renderer decides on its own.

**Composited last, after the dissolve and the zoom.** Overlaid earlier, a
caption would fade out with the slide under it at every scene change and drift
across the frame with the Ken Burns move. It is a separate concat of transparent
PNGs - gaps filled with a clear frame, because the concat demuxer has no notion
of a hole and would hold the previous caption over a silence.

**Ken Burns is per-slide, so it needs the cross-dissolve path.** The zoom hangs
off each still's own input; the hard-cut path feeds one concat stream for the
whole part and has nowhere to put it. With `crossfade_seconds: 0` the stage logs
that it is rendering still rather than silently ignoring the setting.

Amplitude is per visual type: figures 7%, everything else 3%, transition cards
zero - a bridge is a punctuation beat and its tinted ground already marks the
change. The still is oversampled by the amplitude before the zoom, so the
tightest crop is still 1:1 rather than an upscale of a natively-sized slide.
`d=1` keeps one output frame per input frame, which is what leaves every xfade
offset - and therefore the A/V sync - exactly as it was.

**Measured on the 2026-08-13 episode, re-rendered from cached audio** (Stage 8
onward is free, so this cost nothing to validate):

| | frames | duration | size | wall |
|---|---|---|---|---|
| both off | 9,468 | 315.605s | 12,586,544 B | 39s |
| captions + motion | 9,468 | 315.605s | 31,067,842 B | 72s |

Same frame count and the same duration to the microsecond, so the timeline
survived both changes; with the flags off the render reproduces the pre-change
episode to the byte, which is the regression check that matters. 152 cues across
the episode, no narration text dropped.

The costs are real but not monetary: **2.5x the file size and 1.9x the wall
time** on 8 cores, because a slow zoom makes every frame distinct and the
encoder can no longer coast through a static slide.

**What is still open.** The cue splitting, the timing and the compositing order
are settled and cost nothing to re-enable. What is not settled is anything a
viewer actually sees: the caption's container (the bordered paper-white pill
reads as a UI element rather than as type on a frame), how much of the slide the
band is allowed to take, and whether constant slow movement suits material this
dense. Re-enabling without answering those reproduces exactly what was rejected.


## D34 - Length and pace are two knobs, and D29 was turning the wrong one

The owner's note on the 2026-08-13 episode was that it had drifted long and slow,
and asked for episode one's length and pace back while keeping the cold open,
the bridges and the outro that had been added since. Those turned out to be two
faults with two different causes, and D29 had conflated them.

**The measurements first**, across the three shipped episodes:

| | total | segments | wrapper | scenes/segment | s/scene |
|---|---|---|---|---|---|
| ep 1 (08-07) | 4:51 | 266.6s | 24.9s | 7, 7, 8 | 12.1 |
| ep 2 (08-12) | 6:22 | 339.0s | 44.4s | 8, 7, 7 | 15.4 |
| ep 3 (08-13) | 5:16 | 249.6s | 65.9s | 6, 6, 6 | 13.9 |

Episode three's **segments were already the shortest of the three**. The runtime
had moved into the wrapper, which nearly tripled once the cold open had to name
the series and the shared thread (D30) and the bridges arrived (D26).

And the six-scene cap D29 introduced to fix episode two's length **saved no time
at all** — the words stayed and simply arrived in fewer, longer scenes, which is
the "slow" the owner was hearing. Episode one ran seven and eight scenes a
segment and was the shortest episode of the three.

So: **words are length, scenes are pace.** Config now sets them separately —
7-8 scenes a segment against 6, and the runtime taken out of the wrapper.

**The narration arrives at 30-33 words a scene whatever the prompt asks for.**
This is the finding that cost the most to learn. Measured across v1, v3 and v4,
with the stated per-scene budget ranging from 23 to 31 words, the delivered
figure never left that band — so the scene count, not the budget line, had been
setting the length all along. Raising the cap to 8 with the budget unchanged
produced a **6:27** draft, worse than anything shipped.

Two things fixed it, and the order matters:

1. **State the per-scene budget as a structure, not a number.** "One sentence per
   scene; a second only if it is under eight words" is a rule the model can check
   itself against. "About twenty-three words" is not. This alone took the draft
   from 6:27 to 4:23.
2. **A condense pass** (`prompts/condense/`, `ScriptStage._condense`) that sends
   an over-budget part back asking *only* for a shorter draft. D29 ruled this out
   on the grounds that no code can safely shorten a sentence — true, but a model
   can, and it does that job well when it is the only job in front of it. The
   rewrite is accepted only if it returns every scene it was given; visuals are
   never re-sent, because the slides are already chosen. It repeats while it is
   making progress, up to `condense_max_passes`, because one pass moves about ten
   percent: the worst real segment went 279 → 242 → 225 against a budget of 175.

With the structural rule in place the pass now rarely fires, which is the right
outcome — it is a backstop, and about a cent when it runs.

**The wrapper is measured at Stage 6 now**, in words, against its own budget.
Episode three's wrapper ran 66s against 46s and nothing said so until a 5:16
episode came out of Stage 8, two stages and a voice bill later.
`cold_open_seconds` also went 18 → 21, because 18 was never enough for the three
beats D30 asks for and the honest fix for a budget nothing can meet is the
budget.

**Validated before spending.** Stage 6 was re-driven five times against the
cached 2026-08-13 digests for $0.62 total, predicting the finished runtime from
word count — the narration reads at about **2.4 spoken words/sec** plus
`tts.scene_gap_seconds` per clip.

**And then the projection was corrected by the run that used it.** Calibrated on
two episodes the rate looked like a constant 2.45 w/s; the 2026-08-19 narration
came back at **2.33**, because the rate moves with how long the words are
(15.25-16.62 characters/sec across four runs, 6.4-6.8 characters/word). So the
projection is good to about **±5%**, not ±2%, and `MEASURED_WORDS_PER_SECOND` is
now the middle of the range rather than the fast end.

That 5% is the difference between two decisions. `target_segment_seconds` had
been put back to 75 on the strength of the optimistic constant. The 2026-08-19
segments then hit that budget almost exactly — 215, 207 and 148 words against
187 each — and still ran **253s**, which is no shorter than the segments of the
episode being fixed. The budget is denominated in words at 150 wpm and the voice
reads at 140-147, so a segment that hits it is already 5% long before the scene
gaps. It is back to **70**, which puts the finished episode at 4:46 against
episode one's 4:51.

**The ceiling went 3.0 → 4.0** at the same time, for an unrelated reason that
surfaced here: Stage 2 scores every paper in the window, so a run costs roughly
what the gap since the last one costs. This window was 7.4 days and 2,180 papers,
about $2.50, which left nothing for a retry (owner, 2026-08-19).



## D35 - Episode one's register and palette, restored on review

The owner watched the 2026-08-19 draft and asked for three things back from the
first episode: its **script**, its **pace**, and its **look** — keeping only the
opening that names the series. Each is a reversal of a decision made on an
earlier review, so each is recorded here rather than quietly applied.

**Register: back to v1 (`prompts/script/v5`).** D30 chose a "light persona"
against the owner's reference channels — open on a crack in what the viewer
believes, second person, deliberately varied sentence length. Across three
episodes that read as mannered rather than clear, and the draft it produced
opened a segment on a forty-word sentence carrying three statistics. v5 is v1's
narration rules verbatim: state the finding, no second person, no host warmth,
report and let it land.

Two things are deliberately *not* reverted with it, because they are structure
rather than register and neither was what the review objected to: the
caveat-then-close ordering (D26 note 4 — a segment that stops at its own
limitation leaves the paper on its weakest note) and the whole length-and-pace
budget from D34, which measured well and is the reason the segments are short.

**Palette: back to near-black and one accent.** D27 flipped the ground to paper
white and gave each paper its own hue, answering "too dark, too monochromatic"
on episode two. Episode three answered back. The swap is confined to six
constants in `render/slides.py`; D27 records the paper values, so flipping again
is an edit to those lines rather than a rewrite. `PAPER_ACCENTS` survives as a
list of one, because Stage 8 indexes it by running order and collapsing it to a
constant would delete the seam a future per-paper palette hangs on.

**The wrapper is the intro and nothing else.** The cold open keeps its D30 job —
name the series, name the thread, one hook per paper. The outro is gone.

**And the bridges are overruled.** D26 spent a written sentence on each seam,
finding the real relationship between two papers, and banned numbering them:
"never 'our second paper'". The owner asked for exactly that banned form — "a
slight pause and a segue, like 'the second paper is about...'". So a bridge is
now a plain signpost of at most fifteen words, and `tts.bridge_pause_seconds`
(1.0s, against a 0.35s scene gap) does the work the sentence used to. The
reasoning in D26 is not withdrawn and the machinery is untouched; it was
overruled on taste, which is the owner's call. Reverting is a prompt change.

**What this costs in runtime**, projected: 616 words over 31 clips, or **4:29 to
4:37** against episode one's 4:51 — the wrapper is 20s lighter than D34 left it
because the outro went.

**Measured on the finished episode** (2026-08-19, cut once credit was restored):

| | total | scenes/segment | s/slide | wrapper |
|---|---|---|---|---|
| ep 1 | 4:51 | 7, 7, 8 | 12.1 | 24.9s |
| ep 3 | 5:16 | 6, 6, 6 | 13.9 | 65.9s |
| **ep 4** | **4:20** | **8, 8, 8** | **9.0-9.8** | **37.8s** |

31 clips, 7 parts, 6 cross-dissolves. The bridges came in at 4.4-5.6s each — a
signpost and its pause — against the 7s the written ones cost. The condense pass
fired once, on the cold open, for two words.

It lands 31s under episode one rather than beside it, which is the one number
that did not come out where D34 aimed. The wrapper is the reason: dropping the
outro and shortening the bridges took ~28s out of it, on top of the segment
budget. If that reads as too short, `script.target_segment_seconds` back to 75
is the knob — worth about +15s — and it is the one that was already measured.

## The 2026-08-19 run: what a mid-run credit failure leaves behind

Worth recording because the failure mode is not one the design anticipated.

Stage 6 writes three segments and then one wrapper call. The segments succeeded;
the wrapper call hit `400 - Your credit balance is too low`, retried three times,
and returned the degraded `EpisodeMetadata` that D18 put there for exactly this
case: a wrapper failure is not worth failing an episode over. That is right when
the wrapper is one bad JSON parse. It is wrong when the cause is an account-level
failure that will hit every subsequent call, because the pipeline then spent
$0.37 narrating and rendered a 4:13 episode with **no intro, no bridges and no
title** before Stage 10 refused to publish it.

Three things worked as intended and are worth keeping:

- **Stage 10 caught it.** "Title is empty. Stage 6 produces it" — the bundle was
  validated rather than uploaded, and the run exited non-zero.
- **The window marker did not move.** `_advance_window` requires a completed
  package stage *and* a `--from fetch` invocation; the upload failure meant
  neither the marker nor the covered-papers ledger advanced, so the window is
  still owed and the three papers are still eligible.
- **Everything upstream is on disk.** Fetch through extract cost $1.98 and does
  not need repeating; finishing needs `--from script`.

**What should change.** A 400 naming the credit balance is not a transient error
and should not be retried three times, nor swallowed by the degraded-wrapper
path. Stage 6 should distinguish "this call failed" from "this account cannot
make calls" and stop the run at the stage boundary, before Stage 7 spends real
money narrating a script that has no title. Not yet implemented.



## D36 - A palette role for data, and a ground that is not quite black

A manim spike (below) animated one `result_callout` and immediately exposed a
hole in the palette. The slide compared two quantities — 200 tokens per parameter
against Chinchilla's 20 — and the baseline bar had to be drawn in `FAINT`,
because that is the only colour between the ground and the accent. `FAINT` is the
colour of rules and inactive chrome, so a real measurement rendered as furniture
and all but vanished.

The static slides never needed a sixth role: nothing on them is *compared*, so
everything is either emphasis or chrome. A comparison needs a colour that means
"this is the value being measured against", and neither an accent nor a rule will
do it.

**`BASE` = (201, 139, 63), `BASE_TEXT` = (232, 199, 154).** Warm, against the cool
accent. Five variants were rendered against the real scene and judged on the
frame, the way D27 chose the last palette: the baseline in the rules colour (the
bug), a neutral slate, the accent darkened, a warm counter-colour, and the neutral
on a lifted ground. The owner took the warm one (2026-08-20).

This is a second hue on screen, which D35 had just removed — but at a different
level. D35 removed per-paper *chapter* colour, where three papers each wore their
own accent and the episode had no single identity. `BASE` is confined to data
inside one slide and never marks a section, so the episode still reads as one
colour with a comparison drawn in two.

**Ground: (15, 17, 21) → (22, 26, 33).** Episode one's near-black, lifted. Large
flat fields of it read hard once bars, braces and rules sit on top, and the
animated slides put a lot of all three on screen. Checked across every slide type
by re-rendering the 2026-08-19 episode from cached audio, which costs nothing:
the figure's light card and the bridge's tinted ground both separate from the
ground more cleanly than they did against pure black.

`BASE` is defined in `render/slides.py` but not yet drawn by any static slide
type — it exists because the palette decision was made here, and the animated
callout that needs it is not built yet.

## D37 - Animated callouts: the model fills a template, it never writes code

Built on D36's spike. A `result_callout` may now carry a `Comparison` - a
template name and its parameters - and Stage 8 renders it with manim instead of
Pillow. Half of all segment scenes carry no real paper figure, and a callout is
the weakest of them: a phrase frozen for nine seconds while the narration makes a
comparison the slide never shows.

**The model picks between three templates and fills them.** `two_bar` (two
quantities at true relative length), `split` (one bar divided, for a proportion)
and `count_up` (one number counted, with its unit). It does not emit animation
code. A scene that compiles, reads well, matches its narration *and* is not
subtly wrong is hard to generate and impossible to check by eye at scale; four
numbers and two labels can be validated in full, and are.

**Everything about this is designed to fail back to the slide it replaced.**
There are five ways to decline and all of them keep the scene:

  - `render.animated_callouts` off, or the `manim` extra not installed
  - no `comparison` on the visual — a bare finding is *better* as static text
  - the part is not cross-dissolving, because the hard-cut path concatenates
    images and has nowhere to put an mp4 (the same constraint motion has, D33)
  - the comparison failed validation, or its numbers are not in the digest
  - manim failed, or ran past `animate_timeout_seconds`

**Two things were learned by running it, not by designing it.**

*A malformed comparison cost a whole segment.* On the first live run of prompt
v6 the model returned a `two_bar` with no `value_b`; because the comparison was
validated inside the `SceneManifest`, the manifest failed and the episode lost a
paper. Comparisons are now lifted out of the payload and validated one at a
time, exactly as transitions already are for exactly the same reason (D18) - an
optional extra on one scene must cost that extra and nothing else.

*Strict typing cost an animation for nothing.* A model returned `note: 65`
instead of `"65"`. The value goes onto a slide as text either way, so the string
fields coerce rather than reject.

**The numbers are checked harder here than anywhere else.** A bar at a tenth the
length of another *is* the claim - the animation is the most credible thing on
screen - so every value must appear in the digest the segment was written from.
This is stricter than `extract.unverifiable_numbers`, which ignores integers
under three digits because they are everywhere in a full paper; a digest is a few
hundred words and a two-digit baseline is precisely what gets invented. It fired
on the first real run, on a claimed `0.5` that appeared nowhere in the digest.

**What the templates cannot catch.** On that same run the model paired a
`two_bar` of 79 against 21 with the note "65-to-1 supervision ratio" - both
numbers real, the note describing a different ratio than the bars draw. Nothing
in the schema can see that. The remaining exposure is semantic, and it is the
argument for keeping the template set small and the `note` field a phrase.

**Cost:** no extra API calls - the parameters come back in the same Stage 6 call.
~3s of CPU per animated scene, against a ~40s episode render.

## The manim spike: what it measured

`prompts/`-driven animation is not built. One scene was hand-written against
ManimCommunity to find out whether it is worth building, using a real callout
from the 2026-08-19 episode (`2608.17286/s3`, measured 9.55s).

| | measured |
|---|---|
| render | **2.85s** for a 9.55s clip at 1920x1080/30 |
| duration accuracy | 9.567s against 9.55s — **17ms, half a frame** |
| install | **266MB**, pure Python |
| LaTeX | **not required** |

The duration number is the one that matters. Stage 8 already knows every scene's
*measured* audio length before it composes anything, so a manim clip can be given
an exact `run_time` and the sync-by-construction property of D20 survives. At
~3s a scene, animating every callout in an episode adds ~14s to a ~40s render.

**`DecimalNumber` is a trap.** The obvious way to animate a counting number
renders through `MathTex` and silently requires a full TeX install. Rebuilding a
Pango `Text` each frame costs nothing at this size and keeps the dependency to
what manim already needs. Any template library should ban the MathTex family
outright.

D20 rejected Remotion partly for a ~500MB dependency tree beside a Python
codebase. Manim is roughly half that and stays in Python, so that objection does
not transfer.

**What is not answered.** The scene was hand-written; in the pipeline a model
would have to produce it, which is the real risk and the reason to give it
parameterised templates rather than let it emit Python. And it is the best case:
of the five callouts in that episode, four have animatable structure and one
("First reliable compute-optimal guidance") has no number in it at all, so the
fallback to a static callout is a routine path rather than a safety net.


---

## D38 - Edit the argument, and let the picture develop

The next review asked for a substantial improvement in engagement and flow.
The supplied 4:42 episode and the existing prompt/rendering code pointed to a
specific problem: the format explains interesting findings but spends much of
its time holding a whole figure or a short phrase while the narration advances.
The opening also introduces material that the first segment introduces again.

**The voice and palette stay direct.** This does not reinstate D29's host persona,
second-person rhetoric, arbitrary camera zooms, burned-in captions, or a music
drone. The change is in the structure and the relationship between an idea and
its visual.

- `script/v8` asks for hook, context, mechanism, evidence, caveat, and payoff.
  Short hooks alternate with fuller explanations instead of every sentence
  receiving the same shape. The shipped budget is 65 seconds and 9–11 scenes;
  a sparse digest may produce fewer. Only the supplied digest supports claims.
- `episode/v6` reduces the opening to two beats within nine seconds: tension
  and promise. The series name is already on screen. Paper one starts directly;
  later papers keep short, numbered signposts. There is still no outro.
  The wrapper sees the actual segment openings and closes to avoid repetition.
- `beat` and `pause_after` are optional scene fields. Invalid optional direction
  is discarded on its own. Old scripts remain readable. Explicit zero pauses
  work, and bridge pauses retain their episode-level override.
- ElevenLabs receives the preceding and following sentence within a part, using
  its documented `previous_text` and `next_text` fields. Other engines retain
  their existing behavior. This is implemented and request-tested, not a claim
  of an audible improvement measured against a fresh generation. Reference:
  [ElevenLabs convert API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).

**The editorial renderer uses cached Pillow layers and ffmpeg.** A mechanism
builds as a sequence, a contrast becomes two panels, and a comparison reveals
labels and correctly scaled bars. Headline and reading-cue fields guide the
paper figures; the actual figures remain intact and uncropped. Reveal timing
is relative to measured scene audio, not word alignment. Every layout leaves
time to read its completed composition. No model-generated animation code,
asset downloads, or new billed generation calls are involved.

It runs in a bounded subprocess and falls back to the completed static layout
if motion fails. Hard cuts and single-scene parts support animated clips too.
`render.visual_style: classic` preserves the original rendering path; this repo's
config explicitly selects `editorial`. Pillow, already required by rendering,
is now declared as a direct dependency.

**Timing fixes surfaced by the implementation:**

- Missing middle audio now fails before encoding instead of truncating two
  unrelated lists to a common prefix and putting later narration under the
  wrong picture.
- Incoming animations receive first-frame padding during a dissolve, so their
  internal motion does not begin early against the audio. Real ffmpeg tests
  inspect the incoming frame as well as stream durations.
- Chapter markers follow the paper order when the first bridge is absent;
  bridge scene IDs retain their cached naming convention.
- Non-finite comparison values are rejected before they reach a chart.

**The review loop becomes inexpensive.** `run --through script` stops before
narration, `review` prints an estimated or measured edit timeline with actionable
flags, and `demo` renders an explicitly illustrative preview without credentials.
An early-stop invocation never advances a fetch window just because an old
package already exists. These flags are editing heuristics, not retention
predictions. No claim of improved audience retention has been measured.

**Validation:** the silent seven-scene preview rendered at 1280×720/30 with
1,215 video frames. Both encoded streams measured exactly 40.500 seconds. The
test suite covers optional direction, zero/bridge pauses, continuity request
fields, comparison geometry, static fallback, current chapter numbering, and
real hard-cut/dissolve timing. A fresh LLM-scripted and ElevenLabs-narrated
episode was not generated: those API credentials were absent. That live review
is the remaining creative validation, rather than a hidden pass in this log.

## Deferred — not yet decided

All ten stages are built. Nothing below blocks producing an episode; the audit
blocks publishing one automatically.

| ID | Decision | Spec | Status |
|---|---|---|---|
| — | **YouTube compliance audit** | §3 Stage 10 | **Owner action, and now the only thing on the critical path.** ~2-4 week lead time and no code dependency. The spec says submit it during Phase 1; Phases 1-6 are done and it has not been started. Stage 10 stays in dry-run until it clears. |
| — | **Channel + OAuth client** | §3 Stage 10 | Prerequisite for the audit and for `pipeline youtube-auth`: a Google Cloud project with the YouTube Data API enabled and a Desktop-app OAuth client. Not created. |
| — | **Turning the schedule on** | §5 | Decided and built (D25), but deliberately inert: the cron job is gated behind the `EPISODE_ENABLED` repo variable, and the two API secrets are not set. Owner action, one command. |
| — | **Music bed track** | §3 Stage 8, §8 Q4 | Supported but off; needs a licensed file in `assets/music/` (D21). |
| ✓ | **Branding / series name** | §8 Q3 | **Resolved 2026-08-13: "ML Papers of the Day"** (D30). The cold open names it and the opening card carries it. |

## Prompt changelog

| Date | Prompt | Version | Change |
|---|---|---|---|
| 2026-08-07 | `shortlist` | v1 | Initial. Scores novelty / interest / visual_potential 0-10 from title + abstract. Explicitly frames `visual_potential` as an inference, since the model cannot see figures at this stage. Includes anti-hype and full-range-usage instructions. |
| 2026-08-07 | `extract` | v1 | Initial PaperDigest prompt. Sees full paper text + the Stage 3 figure inventory. Carries the fidelity rule (cite a specific table/figure/section; only report quotable numbers), which is also enforced in code — see D17, where a live run cited a real table and invented its contents. |
| 2026-08-07 | `script` | v1 | Initial SceneManifest prompt. Speech rules rather than prose rules: spoken numbers, no LaTeX/markdown, hook first, close on the caveat. Pacing derived from `words_per_minute` so `est_seconds` is computable rather than guessed. |
| 2026-08-07 | `episode` | v1 | Episode wrapper: cold open, outro, ≤70-char YouTube title, description reusing the Stage 4 justifications, 3 thumbnail options. Split from `script` because it needs all three digests at once. |
| 2026-08-07 | `rank` | v1 | Initial. Ranks enriched candidates on claim crispness, real visual assets, breadth of interest and honest framing. States the subfield-diversity and must-have-figures constraints (both also enforced in code, D14). Tells the model that `hf_upvotes` is attention rather than quality. `justification` is written for a viewer because it is reused verbatim in the episode description. |
| 2026-08-11 | `script` | v2 | Stops offering the caveat and the "why it matters" as alternatives — v1 said one *or* the other and the model took the caveat in all three segments of the first episode, ending every paper on its weakest note. v2 requires both, caveat second-to-last and the close last. Backlog note 4. |
| 2026-08-13 | `script` | v3 | Length becomes a hard cap (scene limit + per-scene word budget) after v2 ran 50% over three times; Stage 6 now enforces the scene cap in code. Narration shape borrows structure from explainer channels: open on a gap in what the viewer believes, second person, deliberately varied sentence length. No catchphrases. D29, D30. |
| 2026-08-13 | `episode` | v3 | Names the series ("ML Papers of the Day") and restructures the cold open into three beats — series, shared thread, then the papers. Outro returns to the thread. Resolves §8 Q3. D30. |
| 2026-08-11 | `episode` | v2 | Adds `transitions`: one bridge line before each paper, including the first. Each is a single spoken sentence that settles what just played and turns toward what is next, plus a two-to-five-word `label` for the card. Carries a ban list, because every obvious phrasing here ("next up", "moving on", numbering the papers) is a dead one, and a worked good/bad example. Backlog note 3, D26. |
