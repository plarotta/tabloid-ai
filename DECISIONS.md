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

---

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
