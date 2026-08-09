#!/usr/bin/env python
"""Demo 1 (simple) - static HTML report for a pipeline run.

Answers "did the pipeline produce sane output?" at a glance: what each stage
emitted on real data, what it cost, and - importantly - which stages have never
actually run. Writes one self-contained page and opens it.

    python demos/1_run_report.py                 # newest run with enrich output
    python demos/1_run_report.py --run 2026-08-07
    python demos/1_run_report.py --no-open

No server, no dependencies. Figures are referenced by relative path so the page
works over file://.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shared import REPO_ROOT, esc, load_json, load_jsonl, page, pick_run, stat

# Order matters: this is the pipeline's own STAGE_ORDER.
STAGES = [
	("fetch", "Fetch", "arXiv Atom API"),
	("shortlist", "Shortlist", "cheap LLM screen"),
	("enrich", "Enrich", "LaTeX source, figures, captions"),
	("rank", "Rank", "frontier LLM, top 3"),
	("extract", "Extract", "PaperDigest"),
	("script", "Script", "SceneManifest"),
	("voice", "Voice", "TTS"),
	("render", "Render", "video assembly"),
	("package", "Package", "mp4 + metadata"),
]

IMPLEMENTED = {"fetch", "shortlist", "enrich", "rank"}


def rel(target: Path, start: Path) -> str:
	return os.path.relpath(target, start).replace(os.sep, "/")


# Stages that cannot produce output without an LLM call. For these, the presence
# of an artifact is NOT evidence the stage ran: a fixture can be written by hand.
LLM_STAGES = {"shortlist", "rank"}


def billed_stages(run: Path) -> set[str]:
	"""Stages with at least one recorded billed call in this run.

	`calls.jsonl` is appended to at the moment each call returns, so it is the
	authoritative record of what actually talked to a provider.
	"""
	path = run / "calls.jsonl"
	if not path.exists():
		return set()
	return {rec.get("stage") for rec in load_jsonl(path) if rec.get("stage")}


def stage_status_table(run: Path) -> str:
	artifacts = {
		"fetch": run / "fetch" / "papers.jsonl",
		"shortlist": run / "shortlist" / "shortlist.json",
		"enrich": run / "enrich" / "enrich.json",
		"rank": run / "rank" / "rank.json",
	}
	billed = billed_stages(run)
	rows = []
	for key, name, desc in STAGES:
		path = artifacts.get(key)
		if key not in IMPLEMENTED:
			badge = '<span class="pill dim">not built</span>'
			detail = "Phase 3+"
		elif path is not None and path.exists():
			if key in LLM_STAGES and key not in billed:
				# The artifact exists but nothing was ever billed for it, so it
				# was hand-made (a smoke-test fixture), not model output. Saying
				# "ran" here would misrepresent the state of the project.
				badge = '<span class="pill bad">fixture</span>'
				detail = (
					f"<code>{esc(rel(path, run.parent.parent))}</code><br>"
					'<span class="muted" style="font-size:12px">hand-written test input &mdash; '
					"no billed LLM call recorded, so this is not model output</span>"
				)
			else:
				badge = '<span class="pill ok">ran</span>'
				detail = f"<code>{esc(rel(path, run.parent.parent))}</code>"
		else:
			badge = '<span class="pill warn">no output</span>'
			detail = '<span class="muted">not run for this run id</span>'
		rows.append(
			f"<tr><td><strong>{esc(name)}</strong><br>"
			f'<span class="muted" style="font-size:12px">{esc(desc)}</span></td>'
			f"<td>{badge}</td><td>{detail}</td></tr>"
		)
	return (
		"<table><thead><tr><th>Stage</th><th>Status</th><th>Artifact</th></tr></thead>"
		f"<tbody>{''.join(rows)}</tbody></table>"
	)


def fetch_section(run: Path) -> str:
	path = run / "fetch" / "papers.jsonl"
	if not path.exists():
		return '<div class="card muted">No fetch output for this run.</div>'
	papers = load_jsonl(path)
	cats = Counter(p.get("primary_category", "?") for p in papers)
	days = Counter((p.get("submitted") or "")[:10] for p in papers)
	top = cats.most_common(8)
	widest = max((c for _, c in top), default=1)

	cat_rows = "".join(
		f"<tr><td><code>{esc(c)}</code></td><td style='width:60%'>"
		f"<div class='bar'><i style='width:{n / widest * 100:.0f}%'></i></div></td>"
		f"<td style='text-align:right'>{n}</td></tr>"
		for c, n in top
	)
	day_rows = "".join(
		f"<tr><td><code>{esc(d or '?')}</code></td><td style='text-align:right'>{n}</td></tr>"
		for d, n in sorted(days.items())
	)
	sample = "".join(
		f"<tr><td><code>{esc(p['arxiv_id'])}</code></td><td>{esc(p['title'][:95])}</td></tr>"
		for p in papers[:8]
	)
	return f"""
<div class="grid cols-4">
  {stat(f"{len(papers):,}", "papers fetched")}
  {stat(len(cats), "primary categories")}
  {stat(len(days), "submission days")}
  {stat(f"{len({p['arxiv_id'] for p in papers}):,}", "unique ids")}
</div>
<div class="grid cols-2" style="margin-top:14px">
  <div class="card"><h3>Primary category</h3><table>{cat_rows}</table></div>
  <div class="card"><h3>Submission date</h3><table>{day_rows}</table></div>
</div>
<div class="card" style="margin-top:14px"><h3>First few papers</h3>
  <table>{sample}</table></div>"""


def enrich_section(run: Path, out_dir: Path) -> str:
	path = run / "enrich" / "enrich.json"
	if not path.exists():
		return (
			'<div class="card muted">No enrich output for this run. '
			"Run <code>pipeline run --from enrich --run &lt;id&gt;</code>.</div>"
		)
	data = load_json(path)
	papers = data.get("papers", [])
	total_figs = sum(len(p["figures"]) for p in papers)
	usable = sum(1 for p in papers for f in p["figures"] if f["converted"] and f["file"])
	with_caption = sum(1 for p in papers for f in p["figures"] if f.get("caption"))
	latex = sum(1 for p in papers if p["source_quality"] == "latex")

	rows = []
	for p in papers:
		figs = p["figures"]
		ok = sum(1 for f in figs if f["converted"] and f["file"])
		caps = sum(1 for f in figs if f.get("caption"))
		q = p["source_quality"]
		qpill = (
			'<span class="pill ok">latex</span>'
			if q == "latex"
			else f'<span class="pill warn">{esc(q)}</span>'
		)
		notes = p.get("notes") or []
		note_html = (
			f'<span class="muted" style="font-size:12px">{esc("; ".join(notes)[:90])}</span>'
			if notes
			else '<span class="muted">-</span>'
		)
		sig = p.get("signals", {})
		badges = []
		if sig.get("github_url"):
			badges.append('<span class="pill dim">github</span>')
		if sig.get("huggingface"):
			badges.append(f'<span class="pill ok">HF {sig.get("hf_upvotes", 0)}</span>')
		rows.append(
			f"<tr><td><code>{esc(p['arxiv_id'])}</code><br>"
			f"<span style='font-size:12.5px'>{esc(p['paper']['title'][:70])}</span></td>"
			f"<td>{qpill}</td><td>{ok}/{len(figs)}</td><td>{caps}</td>"
			f"<td>{len(p['sections'].get('intro', ''))}</td>"
			f"<td>{' '.join(badges) or '<span class=muted>-</span>'}</td>"
			f"<td>{note_html}</td></tr>"
		)

	# A visual sample: first usable figure from each paper, with its caption.
	cards = []
	for p in papers:
		first = next((f for f in p["figures"] if f["converted"] and f["file"]), None)
		if not first:
			continue
		img = run / "enrich" / p["arxiv_id"] / first["file"]
		if not img.exists():
			continue
		cap = first.get("caption") or "(no caption - PDF fallback route)"
		cards.append(
			f'<div class="card"><img src="{esc(rel(img, out_dir))}" '
			f'style="width:100%;background:#fff;border-radius:6px">'
			f'<div style="margin-top:9px;font-size:12.5px"><code>{esc(p["arxiv_id"])}</code> '
			f"fig{first['number']}</div>"
			f'<div class="muted" style="font-size:12.5px;margin-top:4px">{esc(cap[:180])}</div></div>'
		)

	pct = (usable / total_figs * 100) if total_figs else 0
	cap_pct = (with_caption / total_figs * 100) if total_figs else 0
	return f"""
<div class="grid cols-4">
  {stat(len(papers), "papers enriched")}
  {stat(f"{usable}/{total_figs}", f"figures usable ({pct:.0f}%)", "good" if pct > 90 else "warn")}
  {stat(f"{cap_pct:.0f}%", "figures with captions", "good" if cap_pct > 80 else "warn")}
  {stat(f"{latex}/{len(papers)}", "from LaTeX source")}
</div>
<div class="card" style="margin-top:14px">
<table><thead><tr><th>Paper</th><th>Source</th><th>Figures</th><th>Captions</th>
<th>Intro chars</th><th>Signals</th><th>Notes</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<h2>Sample figure per paper</h2>
<p class="sub">Rendered from the real extracted files. Check that each caption
describes the image beside it - that pairing is the thing most likely to break.</p>
<div class="grid cols-2" style="margin-top:12px">{"".join(cards)}</div>"""


def cost_section(run: Path) -> str:
	path = run / "cost_report.json"
	if not path.exists():
		return '<div class="card muted">No cost report.</div>'
	rep = load_json(path)
	by_stage = rep.get("by_stage", [])
	if not by_stage:
		return f"""<div class="grid cols-4">
  {stat(f"${rep['total_usd']:.4f}", "total spent", "good")}
  {stat(f"${rep['ceiling_usd']:.2f}", "ceiling")}
  {stat(rep.get("unpriced_calls", 0), "unpriced calls")}
  {stat(0, "billed calls")}
</div>
<div class="note" style="margin-top:14px">No billed LLM calls in this run.
Stages 1 and 3 make no LLM calls at all; stages 2 and 4 need an API key, which is
not configured, so they have never run live.</div>"""
	rows = "".join(
		f"<tr><td>{esc(s['stage'])}</td><td>{s['calls']}</td>"
		f"<td>{s['input_units']:,}</td><td>{s['output_units']:,}</td>"
		f"<td>${s['cost_usd']:.4f}</td></tr>"
		for s in by_stage
	)
	return f"""<div class="grid cols-4">
  {stat(f"${rep['total_usd']:.4f}", "total spent")}
  {stat(f"${rep['ceiling_usd']:.2f}", "ceiling")}
  {stat(rep.get("unpriced_calls", 0), "unpriced calls")}
</div>
<div class="card" style="margin-top:14px"><table><thead><tr><th>Stage</th><th>Calls</th>
<th>In</th><th>Out</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def source_label(run: Path) -> str:
	"""Name the run a section's data came from.

	Sections are sourced independently because stage outputs currently live in
	different run directories (fetch was validated on a full real window; enrich
	on a smaller smoke run). Labelling each is more honest than silently showing
	one run's data under another run's heading.
	"""
	return f'<p class="sub">source: <code>runs/{esc(run.name)}/</code></p>'


def build(out: Path, preferred: str | None = None) -> Path:
	out.mkdir(parents=True, exist_ok=True)
	report = out / "report.html"

	fetch_run = pick_run(preferred, needs="fetch/papers.jsonl")
	enrich_run = pick_run(preferred, needs="enrich/enrich.json")
	status_run = enrich_run or fetch_run
	cost_run = enrich_run or fetch_run

	def section(title: str, run: Path | None, html_fn) -> str:
		if run is None:
			return f'<h2>{title}</h2><div class="card muted">No run has this artifact yet.</div>'
		return f"<h2>{title}</h2>{source_label(run)}{html_fn(run)}"

	body = f"""
<div class="note">Stages 2 (shortlist) and 4 (rank) require an LLM API key, which
is <strong>not configured</strong>. They have never run live and show no output
below. Everything shown here is real data produced by stages that do run.</div>

<h2>Pipeline stages</h2>
{source_label(status_run) if status_run else ""}
{stage_status_table(status_run) if status_run else ""}

{section("Stage 1 &mdash; Fetch", fetch_run, fetch_section)}

{section("Stage 3 &mdash; Enrich", enrich_run, lambda r: enrich_section(r, out))}

{section("Cost", cost_run, cost_section)}
"""
	runs_used = " · ".join(sorted({r.name for r in (fetch_run, enrich_run) if r is not None}))
	report.write_text(
		page("Pipeline run report", body, f"runs: {runs_used}  ·  {REPO_ROOT.name}"),
		encoding="utf-8",
	)
	return report


def main() -> int:
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--run", help="run id (default: newest with enrich output)")
	ap.add_argument("--no-open", action="store_true", help="do not open a browser")
	args = ap.parse_args()

	if not pick_run(args.run):
		print("No runs found under runs/. Run the pipeline first.", file=sys.stderr)
		return 1

	report = build(Path(__file__).resolve().parent / "out", args.run)
	print(f"Report written: {report}")
	if not args.no_open:
		webbrowser.open(report.as_uri())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
