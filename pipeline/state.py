"""Cross-run state.

Only one thing lives here: when the last run succeeded, so the next fetch window
starts where the previous one ended and papers are neither missed nor re-covered.
Gitignored - it is machine-local bookkeeping, not configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .paths import REPO_ROOT

STATE_PATH = REPO_ROOT / "state.json"


def last_successful_run(path: Path | None = None) -> datetime | None:
	path = path or STATE_PATH
	if not path.exists():
		return None
	try:
		raw = json.loads(path.read_text(encoding="utf-8")).get("last_successful_run")
	except (json.JSONDecodeError, AttributeError):
		return None
	if not raw:
		return None
	try:
		return datetime.fromisoformat(raw)
	except ValueError:
		return None


def mark_successful_run(when: datetime | None = None, path: Path | None = None) -> None:
	path = path or STATE_PATH
	path.write_text(
		json.dumps({"last_successful_run": (when or datetime.now(UTC)).isoformat()}, indent=2),
		encoding="utf-8",
	)
