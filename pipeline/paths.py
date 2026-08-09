"""Run directory layout.

Every stage reads and writes under `runs/<run_id>/<stage>/`. Nothing else is a
valid channel between stages. Keeping this in one place means the re-run logic
and the tests agree on where artifacts live.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"

# Execution order. `--from <stage>` uses this to decide what to replay.
STAGE_ORDER = [
	"fetch",
	"shortlist",
	"enrich",
	"rank",
	"extract",
	"script",
	"voice",
	"render",
	"package",
	"upload",
]


def new_run_id(today: date | None = None) -> str:
	return (today or date.today()).isoformat()


class RunPaths:
	def __init__(self, run_id: str, runs_dir: Path | None = None) -> None:
		self.run_id = run_id
		self.root = (runs_dir or RUNS_DIR) / run_id

	def stage_dir(self, stage: str) -> Path:
		d = self.root / stage
		d.mkdir(parents=True, exist_ok=True)
		return d

	# Stage 1
	@property
	def papers_jsonl(self) -> Path:
		return self.stage_dir("fetch") / "papers.jsonl"

	# Stage 2
	@property
	def shortlist_json(self) -> Path:
		return self.stage_dir("shortlist") / "shortlist.json"

	@property
	def enriched_dir(self) -> Path:
		return self.stage_dir("enrich")

	@property
	def output_dir(self) -> Path:
		return self.stage_dir("output")

	# Stage 10. Lives beside the bundle it published rather than in an `upload/`
	# stage dir, because it is the record of what happened to *that* bundle - and
	# the spec names this path.
	@property
	def upload_json(self) -> Path:
		return self.output_dir / "upload.json"

	def paper_fulltext(self, arxiv_id: str) -> Path:
		"""Whole-paper prose captured by Stage 3, read by Stage 5."""
		return self.enriched_dir / arxiv_id.replace("/", "_") / "fulltext.txt"

	# Cost accounting lives at the run root, not under a stage, because it spans
	# all of them.
	@property
	def calls_jsonl(self) -> Path:
		self.root.mkdir(parents=True, exist_ok=True)
		return self.root / "calls.jsonl"

	@property
	def cost_report_json(self) -> Path:
		self.root.mkdir(parents=True, exist_ok=True)
		return self.root / "cost_report.json"

	def exists(self) -> bool:
		return self.root.exists()


def write_json(path: Path, payload: Any) -> None:
	"""Write JSON atomically so an interrupted run cannot leave a half-written
	artifact that a later `--from` re-run would happily read as valid."""
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix(path.suffix + ".tmp")
	text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
	tmp.write_text(text, encoding="utf-8")
	tmp.replace(path)


def read_json(path: Path) -> Any:
	return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix(path.suffix + ".tmp")
	with tmp.open("w", encoding="utf-8") as fh:
		for row in rows:
			fh.write(json.dumps(row, default=str) + "\n")
	tmp.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
	rows = []
	with path.open(encoding="utf-8") as fh:
		for line in fh:
			line = line.strip()
			if line:
				rows.append(json.loads(line))
	return rows
