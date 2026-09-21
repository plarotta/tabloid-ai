# Demos

Three local, visual demos for validating the original research stages. These
are **stdlib only** (no extra installs) and all run against **real data** — no
mocked outputs, no illustrative placeholders.

None of them make a billed LLM call, so running them costs nothing.

The video format also has a self-contained preview:

```bash
pipeline demo                       # 40.5s silent preview, no API calls
pipeline demo --narrate              # same example through configured TTS (billed)
```

This newer demo uses explicitly illustrative content, unlike the three research
inspection tools below. It exercises the actual motion renderer and muxer,
including the hook, contrast, process, figure, comparison, caveat, and payoff.
It writes `editorial-preview.mp4`, `storyboard.png`, `script.md`, and a pacing
report under `demos/out/editorial/`. `pipeline review --run <id>` reviews actual
episode scripts and measured narration.

```bash
python demos/1_run_report.py         # static HTML report, opens in a browser
python demos/2_enrich_explorer.py    # localhost:8765 - browse Stage 3 output
python demos/3_parser_workbench.py   # localhost:8766 - parse any paper, live
```

---

## 1. Run report (simple)

*"Did the pipeline produce sane output?"*

One static page summarising a run: which stages have output, the fetch window
broken down by category and date, enrichment quality, and cost. Written to
`demos/out/report.html`.

Each section is sourced from the newest run that actually has that artifact, and
labelled with which run that was — fetch and enrich currently live in different
run directories, and showing one run's data under another's heading would be
misleading.

It also **distinguishes real stage output from test fixtures.** A `shortlist.json`
can be hand-written; the demo cross-checks `calls.jsonl` and marks any LLM stage
with no recorded billed call as `fixture`, not `ran`.

```bash
python demos/1_run_report.py --run 2026-08-07   # pin a run
python demos/1_run_report.py --no-open
```

## 2. Enrichment explorer (medium)

*"Is the LaTeX parser actually correct?"*

The one that catches real bugs. Every extracted figure is rendered **beside the
caption the parser paired with it**, so a mispairing is visible immediately
rather than buried in JSON. Also shows the extracted intro/conclusion, parsed
section titles, external signals, and anything the stage flagged.

Figures that could not be rasterised are shown as such rather than omitted, so
the failure is visible too.

```bash
python demos/2_enrich_explorer.py --run enrich-smoke --port 8765
```

Reads artifacts already on disk; makes no network calls.

## 3. Parser workbench (complex)

*"Does this generalise beyond the papers it was built on?"*

Runs the **real Stage 3 code path** — nothing is mocked.

- **Live paper** — enter any arXiv ID. Downloads the actual e-print, unpacks it,
  flattens the include graph, parses figures and sections, rasterises the
  figures, and fetches external signals, timing every step.
- **LaTeX snippet** — paste LaTeX and see exactly what the parser extracts.
  Instant, no network. The default sample contains the edge cases that broke
  earlier versions: a commented-out figure, an inline logo, a nested-brace
  caption, `\textcolor`, and escaped literals.

```bash
python demos/3_parser_workbench.py --port 8766
```

Downloads go to a temp directory and are cleaned up on exit; `runs/` is never
touched. Requests are rate-limited — arXiv is a free service.

Papers worth trying: `2507.06261` (reaches all content via two-argument
`\subimport`), `2505.09388` (non-standard root filename, one `\input` commented
out), `2506.15841` (conventional layout).

---

## What these demos found

They are not just presentation. Building and looking at them surfaced four real
bugs, all since fixed and covered by regression tests:

| Bug | Symptom | Found by |
|---|---|---|
| JPEG bytes written to `.png` filenames | 9 of 58 figures had a lying extension | figure integrity check |
| `\href{URL}{anchor}` collapsing | `"...showPreview=truetake a video of a lecture"` | demo 3, live paper |
| `\cite{}` removal left a space | `"robustness to noise ."` | demo 2, intro panel |
| PDF fallback returning slivers | median figure 1057×249 | demo 1, figure sample |

## What they do and don't cover

Written during Phase 2, when only stages 1 and 3 had run live. All nine stages
have since run on real data, so the demos no longer show a half-finished
pipeline — but they were never extended past Stage 3 either:

- **Covered:** the fetch window (demo 1), and Stage 3 enrichment in depth
  (demos 2 and 3) — the stage with the most surface area for silent wrongness.
- **Not covered:** digests, scripts, narration and rendered video. Those have
  their own review surfaces: `runs/<date>/script/*.md` for the scripts, and the
  rendered `.mp4` files themselves.

Demo 1 still cross-checks `calls.jsonl` before claiming an LLM stage ran, so it
stays honest if pointed at a run where one was hand-seeded.
