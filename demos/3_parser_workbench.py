#!/usr/bin/env python
"""Demo 3 (complex) - run Stage 3 live against any arXiv paper.

Answers "does the parser generalise beyond the papers it was built on?" Two modes:

  Live paper   enter any arXiv ID; downloads the real e-print, unpacks it, parses
               it and rasterises its figures, timing each step. Nothing is
               mocked - this is the actual Stage 3 code path.
  Snippet      paste LaTeX and see exactly what the parser extracts from it.
               Instant, no network, good for probing edge cases.

    python demos/3_parser_workbench.py
    python demos/3_parser_workbench.py --port 8766

Downloads go to a temp directory and are deleted afterwards; nothing touches
runs/. arXiv is a free service, so requests are rate-limited and results cached
per-process.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _shared import esc, page, stat

from pipeline import figures as figlib
from pipeline import signals as siglib
from pipeline.eprint import EPrintError, EPrintFetcher
from pipeline.latex import (
	extract_figures,
	extract_sections,
	find_root_tex,
	flatten_includes,
	parse_source,
	resolve_graphics_file,
)

SAMPLE = r"""\documentclass{article}
\begin{document}
% This commented figure must NOT be picked up:
% \begin{figure}\includegraphics{ghost.pdf}\caption{Ghost}\end{figure}

\includegraphics[height=24pt]{logo/company-logo.pdf}  % inline logo, not a figure

\section{Introduction}
We show that \textcolor{red}{colour commands} do not leak into the text,
and that citations \cite{smith2024} are removed cleanly.

\begin{figure}
  \includegraphics[width=\textwidth]{figures/main_result.pdf}
  \caption{\textbf{Headline result on \textit{ImageNet}}: accuracy improves by
  4.2\% over the \underline{baseline} on lp\_ship12l.}
  \label{fig:main}
\end{figure}

\section{Conclusion}
Nested braces in captions are matched by depth, not by regex.
\end{document}
"""

_cache: dict[str, dict] = {}
_lock = threading.Lock()
_last_fetch = [0.0]


def timed(label: str, fn):
	t0 = time.perf_counter()
	result = fn()
	return result, (label, (time.perf_counter() - t0) * 1000)


def analyse_live(arxiv_id: str) -> dict:
	"""Run the real Stage 3 path against one paper. Never raises."""
	with _lock:
		if arxiv_id in _cache:
			return _cache[arxiv_id]
		# arXiv asks for >=3s between requests.
		wait = 3.0 - (time.time() - _last_fetch[0])
		if wait > 0:
			time.sleep(wait)
		_last_fetch[0] = time.time()

	work = Path(tempfile.mkdtemp(prefix=f"workbench_{arxiv_id}_"))
	steps: list[tuple[str, float]] = []
	out: dict = {"arxiv_id": arxiv_id, "steps": steps, "error": None, "figures": []}
	fetcher = EPrintFetcher(delay_seconds=1.0, max_retries=2)
	try:
		(source_dir, kind), t = timed(
			"download + unpack", lambda: fetcher.fetch_source(arxiv_id, work)
		)
		steps.append(t)
		out["kind"] = kind

		root, t = timed("find root .tex", lambda: find_root_tex(source_dir))
		steps.append(t)
		out["root"] = root.name if root else None

		if root is None:
			out["error"] = f"No .tex root in the archive (arXiv returned kind={kind!r})."
			return out

		flat, t = timed("flatten includes", lambda: flatten_includes(root, source_dir))
		steps.append(t)
		out["flat_chars"] = len(flat)
		out["tex_files"] = len(list(source_dir.rglob("*.tex")))

		parsed, t = timed("parse figures + sections", lambda: parse_source(source_dir))
		steps.append(t)
		out["parsed"] = parsed

		fig_dir = work / "png"
		converted = 0
		t0 = time.perf_counter()
		for ref in parsed.figures[:12]:
			primary = ref.graphics[0] if ref.graphics else ""
			src = resolve_graphics_file(primary, source_dir) if primary else None
			entry = {
				"number": ref.number,
				"caption": ref.caption,
				"ref": primary,
				"file": None,
				"fmt": src.suffix.lower().lstrip(".") if src else "",
			}
			if src is not None:
				written = figlib.normalise(src, fig_dir, f"fig{ref.number:02d}")
				if written is not None:
					entry["file"] = str(written)
					converted += 1
			out["figures"].append(entry)
		steps.append(("rasterise figures", (time.perf_counter() - t0) * 1000))
		out["converted"] = converted

		sig, t = timed(
			"external signals",
			lambda: siglib.collect(arxiv_id, intro=parsed.intro),
		)
		steps.append(t)
		out["signals"] = sig
	except EPrintError as e:
		out["error"] = f"Could not retrieve the e-print: {e}"
	except Exception as e:  # a demo must not die on one bad paper
		out["error"] = f"{type(e).__name__}: {e}"
		out["trace"] = traceback.format_exc()
	finally:
		fetcher.close()
		out["_work"] = work

	with _lock:
		_cache[arxiv_id] = out
	return out


def render_live(res: dict) -> str:
	if res.get("error"):
		trace = res.get("trace")
		extra = (
			f'<div class="scroll" style="margin-top:10px"><pre>{esc(trace)}</pre></div>'
			if trace
			else ""
		)
		return (
			f'<div class="note" style="border-left-color:var(--bad);color:#f2b8c6">'
			f"<strong>Failed:</strong> {esc(res['error'])}</div>{extra}"
		)

	parsed = res["parsed"]
	steps = res["steps"]
	total = sum(ms for _, ms in steps)
	step_rows = "".join(
		f"<tr><td>{esc(label)}</td><td style='width:55%'>"
		f"<div class='bar'><i style='width:{ms / max(total, 1) * 100:.1f}%'></i></div></td>"
		f"<td style='text-align:right'>{ms:,.0f} ms</td></tr>"
		for label, ms in steps
	)

	pairs = []
	for f in res["figures"]:
		if f["file"]:
			img = f'<img src="/localfig?path={esc(f["file"])}" loading="lazy">'
		else:
			img = (
				'<div class="card" style="text-align:center;padding:34px 10px">'
				'<span class="pill bad">not rasterised</span></div>'
			)
		pairs.append(
			f'<div class="figpair">{img}<div>'
			f'<div style="margin-bottom:8px"><span class="pill dim">fig {f["number"]}</span> '
			f'<span class="pill dim">{esc(f["fmt"] or "?")}</span></div>'
			f'<div class="cap">{esc(f["caption"] or "(no caption parsed)")}</div>'
			f'<div class="muted" style="margin-top:9px;font-size:12px">'
			f"source: <code>{esc(f['ref'])}</code></div></div></div>"
		)

	sig = res.get("signals")
	sig_bits = []
	if sig:
		if sig.github_url:
			sig_bits.append(f'<a class="pill dim" href="{esc(sig.github_url)}">github</a>')
		if sig.huggingface:
			sig_bits.append(f'<span class="pill ok">HF daily · {sig.hf_upvotes} upvotes</span>')
		elif sig.hf_lookup_ok:
			sig_bits.append('<span class="pill dim">not on HF daily</span>')
		else:
			sig_bits.append('<span class="pill warn">HF lookup failed</span>')

	nfig = len(parsed.figures)
	conv = res.get("converted", 0)
	return f"""
<div class="grid cols-4">
  {stat(nfig, "figures found")}
  {stat(f"{conv}/{min(nfig, 12)}", "rasterised", "good" if conv == min(nfig, 12) and nfig else "warn")}
  {stat(f"{len(parsed.intro):,}", "intro chars", "good" if parsed.intro else "warn")}
  {stat(f"{len(parsed.conclusion):,}", "conclusion chars", "good" if parsed.conclusion else "warn")}
</div>
<div class="grid cols-2" style="margin-top:14px">
  <div class="card"><h3>Pipeline steps <span class="muted" style="font-weight:400">
    (total {total:,.0f} ms)</span></h3><table>{step_rows}</table></div>
  <div class="card"><h3>Source</h3><table>
    <tr><td>archive kind</td><td><code>{esc(res.get("kind"))}</code></td></tr>
    <tr><td>root .tex</td><td><code>{esc(res.get("root"))}</code></td></tr>
    <tr><td>.tex files</td><td>{res.get("tex_files", 0)}</td></tr>
    <tr><td>flattened</td><td>{res.get("flat_chars", 0):,} chars</td></tr>
    <tr><td>sections</td><td>{len(parsed.sections)}</td></tr>
    <tr><td>signals</td><td>{" ".join(sig_bits) or "-"}</td></tr>
  </table></div>
</div>
<div class="card" style="margin-top:14px"><h3>Section titles ({len(parsed.sections)})</h3>
  <div class="muted" style="font-size:13px">{esc(" · ".join(parsed.sections)) or "(none)"}</div></div>
<div class="grid cols-2" style="margin-top:14px">
  <div class="card"><h3>Introduction</h3><div class="scroll"><pre>{esc(parsed.intro or "(none)")}</pre></div></div>
  <div class="card"><h3>Conclusion</h3><div class="scroll"><pre>{esc(parsed.conclusion or "(none)")}</pre></div></div>
</div>
<h2>Figures &amp; captions ({nfig}{", first 12 shown" if nfig > 12 else ""})</h2>
{"".join(pairs) or '<div class="card muted">No figure environments with graphics.</div>'}"""


def render_snippet(src: str) -> str:
	from pipeline.latex import strip_comments

	text = strip_comments(src)
	figs = extract_figures(text)
	secs = extract_sections(text)

	fig_rows = "".join(
		f"<tr><td>{f.number}</td><td><code>{esc(', '.join(f.graphics))}</code></td>"
		f"<td>{esc(f.caption) or '<span class=muted>(none)</span>'}</td>"
		f"<td><code>{esc(f.label) or '-'}</code></td></tr>"
		for f in figs
	)
	sec_rows = "".join(
		f"<tr><td>{esc(title)}</td><td>{len(body):,} chars</td></tr>"
		for title, body in secs.items()
	)
	return f"""
<div class="grid cols-4">
  {stat(len(figs), "figures extracted")}
  {stat(len(secs), "sections found")}
  {stat(f"{len(text):,}", "chars after comments")}
  {stat(sum(1 for f in figs if f.caption), "captions parsed")}
</div>
<div class="card" style="margin-top:14px"><h3>Figures</h3>
<table><thead><tr><th>#</th><th>graphics</th><th>caption (cleaned)</th><th>label</th></tr></thead>
<tbody>{fig_rows or '<tr><td colspan=4 class="muted">none</td></tr>'}</tbody></table></div>
<div class="card" style="margin-top:14px"><h3>Sections</h3>
<table><thead><tr><th>title</th><th>body</th></tr></thead>
<tbody>{sec_rows or '<tr><td colspan=2 class="muted">none</td></tr>'}</tbody></table></div>"""


def shell(active: str, inner: str, arxiv_id: str = "", snippet: str = SAMPLE) -> str:
	tabs = (
		f'<nav class="tabs">'
		f'<a href="/" class="{"on" if active == "live" else ""}">Live paper</a>'
		f'<a href="/snippet" class="{"on" if active == "snippet" else ""}">LaTeX snippet</a>'
		f"</nav>"
	)
	if active == "live":
		form = f"""<form class="card" method="get" action="/" style="margin-bottom:18px">
  <h3>Run Stage 3 against a real arXiv paper</h3>
  <p class="sub" style="margin-bottom:10px">Downloads the actual e-print and parses it.
  Nothing is mocked. Try <code>2507.06261</code> (subimport-heavy),
  <code>2505.09388</code> (commented-out include), or any recent ID.</p>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <input name="id" value="{esc(arxiv_id)}" placeholder="2507.06261" style="flex:1;min-width:220px">
    <button type="submit">Parse</button></div></form>"""
	else:
		form = f"""<form class="card" method="post" action="/snippet" style="margin-bottom:18px">
  <h3>Paste LaTeX</h3>
  <p class="sub" style="margin-bottom:10px">Runs the same extraction the pipeline uses.
  The default sample contains the edge cases that broke earlier versions.</p>
  <textarea name="src">{esc(snippet)}</textarea>
  <div style="margin-top:10px"><button type="submit">Parse snippet</button></div></form>"""
	return page(
		"Parser workbench",
		tabs + form + inner,
		"Stage 3 (enrich) - live against real papers, or against pasted LaTeX",
	)


class Handler(BaseHTTPRequestHandler):
	def log_message(self, *a):
		pass

	def _send(self, body: bytes, ctype: str, code: int = 200):
		self.send_response(code)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _html(self, s: str):
		self._send(s.encode("utf-8"), "text/html; charset=utf-8")

	def do_POST(self):
		url = urlparse(self.path)
		if url.path != "/snippet":
			return self._send(b"not found", "text/plain", 404)
		length = int(self.headers.get("Content-Length", 0))
		fields = parse_qs(self.rfile.read(length).decode("utf-8"))
		src = (fields.get("src") or [""])[0]
		self._html(shell("snippet", render_snippet(src), snippet=src))

	def do_GET(self):
		url = urlparse(self.path)
		qs = parse_qs(url.query)

		if url.path == "/localfig":
			# Only serve files this process produced, under a workbench temp dir.
			raw = (qs.get("path") or [""])[0]
			target = Path(raw).resolve()
			tmp_root = Path(tempfile.gettempdir()).resolve()
			ok = (
				str(target).startswith(str(tmp_root))
				and "workbench_" in str(target)
				and target.is_file()
			)
			if not ok:
				return self._send(b"not found", "text/plain", 404)
			mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(
				target.suffix.lower(), "application/octet-stream"
			)
			return self._send(target.read_bytes(), mime)

		if url.path == "/snippet":
			return self._html(shell("snippet", render_snippet(SAMPLE)))

		if url.path != "/":
			return self._send(b"not found", "text/plain", 404)

		arxiv_id = (qs.get("id") or [""])[0].strip()
		if not arxiv_id:
			intro = (
				'<div class="card muted">Enter an arXiv ID above to download and parse a '
				"real paper end to end.</div>"
			)
			return self._html(shell("live", intro))
		self._html(shell("live", render_live(analyse_live(arxiv_id)), arxiv_id=arxiv_id))


def main() -> int:
	ap = argparse.ArgumentParser(description=__doc__)
	ap.add_argument("--port", type=int, default=8766)
	ap.add_argument("--no-open", action="store_true")
	args = ap.parse_args()

	server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
	url = f"http://127.0.0.1:{args.port}/"
	print("Parser workbench - runs the real Stage 3 code path")
	print(f"  {url}   (ctrl-c to stop)")
	if not args.no_open:
		threading.Timer(0.4, lambda: webbrowser.open(url)).start()
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		print("\nstopped")
	finally:
		for res in _cache.values():
			shutil.rmtree(res.get("_work", ""), ignore_errors=True)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
