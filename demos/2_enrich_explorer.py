#!/usr/bin/env python
"""Demo 2 (medium) - browse Stage 3 output paper by paper.

Answers "is the LaTeX parser actually correct?" Every extracted figure is shown
next to the caption the parser paired with it, so a mispairing is obvious at a
glance rather than buried in JSON. Also shows the extracted intro/conclusion, the
external signals, and anything the stage flagged.

    python demos/2_enrich_explorer.py
    python demos/2_enrich_explorer.py --run enrich-smoke --port 8765

Serves on localhost. Stdlib only; reads artifacts already on disk and makes no
network calls of its own.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shared import esc, load_json, page, pick_run, stat

MIME = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
}


def paper_nav(papers: list[dict], current: str | None) -> str:
	links = []
	for p in papers:
		on = " on" if p["arxiv_id"] == current else ""
		figs = sum(1 for f in p["figures"] if f["converted"] and f["file"])
		links.append(
			f'<a class="{on.strip()}" href="/?paper={esc(p["arxiv_id"])}">'
			f'{esc(p["arxiv_id"])} <span style="opacity:.6">({figs})</span></a>'
		)
	return f'<nav class="tabs"><a href="/" class="{"" if current else "on"}">Overview</a>{"".join(links)}</nav>'


def overview(papers: list[dict], run_name: str) -> str:
	total = sum(len(p["figures"]) for p in papers)
	usable = sum(1 for p in papers for f in p["figures"] if f["converted"] and f["file"])
	caps = sum(1 for p in papers for f in p["figures"] if f.get("caption"))
	rows = []
	for p in papers:
		figs = p["figures"]
		ok = sum(1 for f in figs if f["converted"] and f["file"])
		ncap = sum(1 for f in figs if f.get("caption"))
		q = p["source_quality"]
		pill = (
			'<span class="pill ok">latex</span>'
			if q == "latex"
			else f'<span class="pill warn">{esc(q)}</span>'
		)
		sec = p["sections"]
		rows.append(
			f'<tr><td><a href="/?paper={esc(p["arxiv_id"])}"><code>{esc(p["arxiv_id"])}</code></a><br>'
			f'<span style="font-size:12.5px">{esc(p["paper"]["title"][:78])}</span></td>'
			f"<td>{pill}</td><td>{ok}/{len(figs)}</td><td>{ncap}</td>"
			f"<td>{len(sec.get('intro', '')):,}</td><td>{len(sec.get('conclusion', '')):,}</td>"
			f"<td>{len(sec.get('section_titles', []))}</td></tr>"
		)
	return f"""
<div class="grid cols-4">
  {stat(len(papers), "papers")}
  {stat(f"{usable}/{total}", "figures usable")}
  {stat(caps, "captions parsed")}
  {stat(run_name, "run")}
</div>
<div class="card" style="margin-top:16px">
<table><thead><tr><th>Paper</th><th>Source</th><th>Figures</th><th>Captions</th>
<th>Intro</th><th>Conclusion</th><th>Sections</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="sub" style="margin-top:14px">Pick a paper above to see every figure beside
the caption the parser paired with it.</p>"""


def paper_view(p: dict, run_name: str) -> str:
	sec = p["sections"]
	sig = p.get("signals", {})
	notes = p.get("notes") or []

	pairs = []
	for f in p["figures"]:
		num = f["number"]
		if f["converted"] and f["file"]:
			src = f"/fig?paper={p['arxiv_id']}&file={f['file']}"
			img = f'<img src="{esc(src)}" alt="figure {num}" loading="lazy">'
		else:
			img = (
				'<div class="card" style="text-align:center;padding:38px 12px" class="muted">'
				'<span class="pill bad">not rasterised</span>'
				f'<div class="muted" style="margin-top:8px;font-size:12.5px">'
				f"{esc(f.get('original_ref') or 'unresolved reference')}</div></div>"
			)
		cap = f.get("caption") or ""
		cap_html = (
			f'<div class="cap">{esc(cap)}</div>'
			if cap
			else '<div class="muted">No caption. Expected for the PDF-fallback route, '
			"which has no LaTeX to read captions from.</div>"
		)
		meta = [f'<span class="pill dim">fig {num}</span>']
		if f.get("source_format"):
			meta.append(f'<span class="pill dim">{esc(f["source_format"])}</span>')
		if f.get("label"):
			meta.append(f'<code style="font-size:11.5px">{esc(f["label"])}</code>')
		pairs.append(
			f'<div class="figpair">{img}<div><div style="margin-bottom:8px">'
			f"{' '.join(meta)}</div>{cap_html}"
			f'<div class="muted" style="margin-top:10px;font-size:12px">'
			f"source: <code>{esc(f.get('original_ref') or '-')}</code></div></div></div>"
		)

	badges = []
	if sig.get("github_url"):
		badges.append(f'<a class="pill dim" href="{esc(sig["github_url"])}">github</a>')
	if sig.get("huggingface"):
		badges.append(f'<span class="pill ok">HF daily · {sig.get("hf_upvotes", 0)} upvotes</span>')
	elif sig.get("hf_lookup_ok"):
		badges.append('<span class="pill dim">not on HF daily</span>')
	else:
		badges.append('<span class="pill warn">HF lookup failed</span>')

	note_html = (
		'<div class="note" style="margin-bottom:16px"><strong>Stage notes</strong><br>'
		+ "<br>".join(esc(n) for n in notes)
		+ "</div>"
		if notes
		else ""
	)

	return f"""
<div class="card" style="margin-bottom:16px">
  <h3>{esc(p["paper"]["title"])}</h3>
  <div class="muted" style="font-size:13px">
    <code>{esc(p["arxiv_id"])}</code> ·
    {esc(", ".join(p["paper"].get("categories", [])[:5]))} ·
    {esc(len(p["paper"].get("authors", [])))} authors
  </div>
  <div style="margin-top:10px">{" ".join(badges)}
    <span class="pill {"ok" if p["source_quality"] == "latex" else "warn"}">
      {esc(p["source_quality"])} source</span></div>
</div>
{note_html}
<div class="grid cols-2">
  <div class="card"><h3>Introduction <span class="muted"
    style="font-weight:400">({len(sec.get("intro", "")):,} chars)</span></h3>
    <div class="scroll"><pre>{esc(sec.get("intro") or "(none extracted)")}</pre></div></div>
  <div class="card"><h3>Conclusion <span class="muted"
    style="font-weight:400">({len(sec.get("conclusion", "")):,} chars)</span></h3>
    <div class="scroll"><pre>{esc(sec.get("conclusion") or "(none extracted)")}</pre></div></div>
</div>
<div class="card" style="margin-top:14px"><h3>Section titles parsed
  ({len(sec.get("section_titles", []))})</h3>
  <div class="muted" style="font-size:13px">
  {esc(" · ".join(sec.get("section_titles", [])) or "(none)")}</div></div>

<h2>Figures &amp; captions ({len(p["figures"])})</h2>
<p class="sub">Each image is shown with the caption the parser paired to it. If a
caption does not describe the image beside it, the pairing is wrong.</p>
{"".join(pairs)}"""


class Handler(BaseHTTPRequestHandler):
	def __init__(self, *a, run: Path, data: dict, **kw):
		self.run = run
		self.data = data
		super().__init__(*a, **kw)

	def log_message(self, *a):  # keep the console quiet
		pass

	def _send(self, body: bytes, ctype: str, code: int = 200):
		self.send_response(code)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_GET(self):
		url = urlparse(self.path)
		qs = parse_qs(url.query)
		papers = self.data["papers"]

		if url.path == "/fig":
			pid = (qs.get("paper") or [""])[0]
			rel = (qs.get("file") or [""])[0]
			target = (self.run / "enrich" / pid / rel).resolve()
			root = (self.run / "enrich").resolve()
			# Never serve outside the run's enrich directory.
			if not str(target).startswith(str(root)) or not target.is_file():
				return self._send(b"not found", "text/plain", 404)
			ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
			return self._send(target.read_bytes(), ctype)

		if url.path != "/":
			return self._send(b"not found", "text/plain", 404)

		current = (qs.get("paper") or [None])[0]
		match = next((p for p in papers if p["arxiv_id"] == current), None)
		body = paper_nav(papers, match["arxiv_id"] if match else None) + (
			paper_view(match, self.run.name) if match else overview(papers, self.run.name)
		)
		html = page(
			"Enrichment explorer",
			body,
			f"Stage 3 output · run {self.run.name} · {len(papers)} papers",
		)
		return self._send(html.encode("utf-8"), "text/html; charset=utf-8")


def main() -> int:
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--run", help="run id (default: newest with enrich output)")
	ap.add_argument("--port", type=int, default=8765)
	ap.add_argument("--no-open", action="store_true")
	args = ap.parse_args()

	run = pick_run(args.run, needs="enrich/enrich.json")
	if run is None or not (run / "enrich" / "enrich.json").exists():
		print(
			"No enrich output found. Run:  pipeline run --from enrich --run <id>",
			file=sys.stderr,
		)
		return 1

	data = load_json(run / "enrich" / "enrich.json")
	handler = partial(Handler, run=run, data=data)
	server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
	url = f"http://127.0.0.1:{args.port}/"
	print(f"Enrichment explorer for run {run.name} ({len(data['papers'])} papers)")
	print(f"  {url}   (ctrl-c to stop)")
	if not args.no_open:
		threading.Timer(0.4, lambda: webbrowser.open(url)).start()
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		print("\nstopped")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
