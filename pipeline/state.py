"""Cross-run state.

Two things live here, both of which stop an episode repeating itself:

  - **when the last run's coverage ends**, so the next fetch window starts where
    the evidence stopped (D31);
  - **which papers have already carried an episode**, so a window that overlaps a
    previous one cannot hand the same paper a second segment (D32).

The marker alone is not enough for the second job. It only holds while windows
tile perfectly; the moment one is rewound - after an index stall, a failed run, a
manual replay - it stops being a guarantee and the ledger is what remains.

Gitignored: machine-local bookkeeping, not configuration. In CI it is restored
from `actions/cache`, and a cache miss degrades both fields to "no history",
which means some overlap rather than breakage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .paths import REPO_ROOT

STATE_PATH = REPO_ROOT / "state.json"


def _read(path: Path) -> dict:
	if not path.exists():
		return {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		return {}
	return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
	path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def last_successful_run(path: Path | None = None) -> datetime | None:
	raw = _read(path or STATE_PATH).get("last_successful_run")
	if not raw:
		return None
	try:
		return datetime.fromisoformat(raw)
	except (ValueError, TypeError):
		return None


def mark_successful_run(when: datetime | None = None, path: Path | None = None) -> None:
	path = path or STATE_PATH
	data = _read(path)
	data["last_successful_run"] = (when or datetime.now(UTC)).isoformat()
	_write(path, data)


def episode_number(path: Path | None = None, default: int = 1) -> int:
	"""Which episode the next packaged run is.

	Beside the window marker because it advances for the same reason and at the
	same moment: one packaged episode over one covered window. Kept here rather
	than in config so a scheduled run numbers itself.
	"""
	raw = _read(path or STATE_PATH).get("episode_number")
	return raw if isinstance(raw, int) and raw > 0 else default


def advance_episode_number(path: Path | None = None) -> int:
	"""Consume the current number and record the next. Returns what was used."""
	path = path or STATE_PATH
	data = _read(path)
	used = data.get("episode_number")
	used = used if isinstance(used, int) and used > 0 else 1
	data["episode_number"] = used + 1
	_write(path, data)
	return used


def covered_papers(path: Path | None = None) -> set[str]:
	"""arXiv IDs that have already had a segment in a packaged episode."""
	raw = _read(path or STATE_PATH).get("covered_papers")
	return {str(x) for x in raw} if isinstance(raw, list) else set()


def mark_covered(arxiv_ids: list[str], path: Path | None = None, keep: int = 400) -> set[str]:
	"""Add papers to the ledger, newest last. Returns the full set afterwards.

	Bounded because this file is a cache entry, not an archive: at three papers
	per episode, `keep` is well over a year of history, and a paper that fell off
	the end is one nobody would recognise as a repeat anyway.
	"""
	path = path or STATE_PATH
	data = _read(path)
	existing = [
		str(x)
		for x in data.get("covered_papers", [])
		if isinstance(data.get("covered_papers"), list)
	]
	merged = existing + [a for a in arxiv_ids if a and a not in existing]
	data["covered_papers"] = merged[-keep:]
	_write(path, data)
	return set(data["covered_papers"])
