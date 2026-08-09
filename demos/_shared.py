"""Shared HTML scaffolding for the demos.

Stdlib only, deliberately: the demos are for inspecting pipeline output, and
they should never be the reason someone has to install something.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CSS = """
:root {
  --bg:#0f1115; --panel:#171a21; --panel-2:#1e222b; --line:#2a2f3a;
  --text:#e6e9ef; --dim:#9aa4b2; --accent:#7aa2f7; --good:#9ece6a;
  --warn:#e0af68; --bad:#f7768e; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
a { color:var(--accent); }
.wrap { max-width:1200px; margin:0 auto; padding:28px 22px 80px; }
header.top { border-bottom:1px solid var(--line); margin-bottom:24px; padding-bottom:16px; }
h1 { margin:0 0 6px; font-size:24px; letter-spacing:-0.02em; }
h2 { margin:32px 0 12px; font-size:17px; letter-spacing:-0.01em; }
h3 { margin:0 0 8px; font-size:15px; }
.sub { color:var(--dim); font-size:13px; margin:0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
.grid { display:grid; gap:14px; }
.cols-4 { grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }
.cols-2 { grid-template-columns:repeat(auto-fit,minmax(380px,1fr)); }
.stat .n { font-size:26px; font-weight:600; letter-spacing:-0.02em; }
.stat .l { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
table { width:100%; border-collapse:collapse; font-size:13.5px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--dim); font-weight:600; font-size:12px; text-transform:uppercase;
     letter-spacing:.05em; }
tr:last-child td { border-bottom:none; }
code,.mono { font-family:var(--mono); font-size:12.5px; }
.pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11.5px;
        font-weight:600; border:1px solid transparent; }
.pill.ok   { background:rgba(158,206,106,.14); color:var(--good); border-color:rgba(158,206,106,.3); }
.pill.warn { background:rgba(224,175,104,.14); color:var(--warn); border-color:rgba(224,175,104,.3); }
.pill.bad  { background:rgba(247,118,142,.14); color:var(--bad); border-color:rgba(247,118,142,.3); }
.pill.dim  { background:rgba(154,164,178,.12); color:var(--dim); border-color:rgba(154,164,178,.25); }
.note { background:rgba(224,175,104,.08); border-left:3px solid var(--warn);
        padding:10px 14px; border-radius:0 6px 6px 0; color:#f0d8ae; font-size:13.5px; }
.figpair { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1fr); gap:16px;
           align-items:start; padding:16px 0; border-bottom:1px solid var(--line); }
.figpair img { width:100%; background:#fff; border-radius:8px; display:block;
               border:1px solid var(--line); }
.cap { color:var(--text); font-size:13.5px; }
.muted { color:var(--dim); }
.scroll { max-height:340px; overflow:auto; background:var(--panel-2);
          border:1px solid var(--line); border-radius:8px; padding:12px; }
.scroll pre { margin:0; white-space:pre-wrap; word-wrap:break-word;
              font-family:var(--mono); font-size:12.5px; color:#c8d0dc; }
input,textarea,button,select {
  font:inherit; color:var(--text); background:var(--panel-2);
  border:1px solid var(--line); border-radius:8px; padding:9px 12px;
}
textarea { width:100%; font-family:var(--mono); font-size:12.5px; min-height:200px; }
button { background:var(--accent); color:#0b0d12; font-weight:600; cursor:pointer;
         border-color:transparent; }
button:hover { filter:brightness(1.08); }
.bar { height:7px; background:var(--panel-2); border-radius:4px; overflow:hidden; }
.bar > i { display:block; height:100%; background:var(--accent); }
nav.tabs { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }
nav.tabs a { padding:7px 13px; border-radius:8px; background:var(--panel);
             border:1px solid var(--line); text-decoration:none; font-size:13.5px; }
nav.tabs a.on { background:var(--accent); color:#0b0d12; font-weight:600;
                border-color:transparent; }
"""


def esc(s: object) -> str:
	return html.escape(str(s if s is not None else ""))


def page(title: str, body: str, subtitle: str = "") -> str:
	return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<header class="top"><h1>{esc(title)}</h1><p class="sub">{esc(subtitle)}</p></header>
{body}
</div></body></html>"""


def stat(n: object, label: str, tone: str = "") -> str:
	colour = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}.get(tone, "")
	style = f' style="color:{colour}"' if colour else ""
	return (
		f'<div class="card stat"><div class="n"{style}>{esc(n)}</div>'
		f'<div class="l">{esc(label)}</div></div>'
	)


def load_json(path: Path):
	return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list:
	rows = []
	with path.open(encoding="utf-8") as fh:
		for line in fh:
			line = line.strip()
			if line:
				rows.append(json.loads(line))
	return rows


def find_runs() -> list[Path]:
	runs = REPO_ROOT / "runs"
	if not runs.exists():
		return []
	return sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda p: p.name)


def pick_run(preferred: str | None = None, needs: str = "") -> Path | None:
	"""Choose a run directory, preferring one that has the artifact we need."""
	candidates = find_runs()
	if preferred:
		match = [d for d in candidates if d.name == preferred]
		if match:
			return match[0]
	if needs:
		with_artifact = [d for d in candidates if (d / needs).exists()]
		if with_artifact:
			return with_artifact[-1]
	return candidates[-1] if candidates else None
