"""Stage 1 - fetch new arXiv submissions for the run window."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..arxiv import ArxivClient
from ..paths import read_json, read_jsonl, write_json, write_jsonl
from ..schemas import Paper
from ..stage import Stage, StageError
from ..state import last_successful_run

log = logging.getLogger(__name__)


class FetchStage(Stage):
	name = "fetch"

	def window(self) -> tuple[datetime, datetime]:
		"""Window runs from the last successful run to now, falling back to a
		fixed lookback so a first run (or a run after a long outage) still works."""
		end = datetime.now(UTC)
		last = last_successful_run()
		fallback = end - timedelta(days=self.config.fetch.fallback_window_days)
		if last is None:
			return fallback, end
		# Guard against a stale state file producing an enormous window.
		return max(last, end - timedelta(days=14)), end

	def is_complete(self) -> bool:
		return self.paths.papers_jsonl.exists()

	def load(self) -> list[Paper]:
		return [Paper.model_validate(r) for r in read_jsonl(self.paths.papers_jsonl)]

	def run(self) -> list[Paper]:
		start, end = self.window()
		cfg = self.config.fetch
		log.info(
			"Fetching %s from %s to %s",
			", ".join(cfg.categories),
			start.isoformat(timespec="minutes"),
			end.isoformat(timespec="minutes"),
		)

		client = ArxivClient(
			page_size=cfg.page_size,
			delay_seconds=cfg.request_delay_seconds,
			max_retries=cfg.max_retries,
			rate_limit_backoff_seconds=cfg.rate_limit_backoff_seconds,
		)
		try:
			papers = list(client.search(cfg.categories, start, end, max_papers=cfg.max_papers))
		finally:
			client.close()

		if not papers:
			raise StageError(
				f"arXiv returned no papers for {start:%Y-%m-%d} to {end:%Y-%m-%d}. "
				f"Check the category list and window in config.yaml."
			)

		write_jsonl(
			self.paths.papers_jsonl,
			[p.model_dump(mode="json") for p in papers],
		)
		# Recorded for the end-of-run state advance. The marker must move to the
		# newest paper actually seen, **not** to the requested `end`.
		#
		# D25 moved it to `end` on the reasoning that papers submitted during the
		# run fall between "end" and "now". True, but it missed a bigger gap:
		# arXiv's search index lags real time, sometimes by many hours. On
		# 2026-08-12 the newest indexed paper was 17:58Z while `end` was 02:27Z the
		# next day - so the marker jumped 8.5 hours past the last real data, and
		# every paper submitted in that gap would have been skipped forever once
		# the index caught up.
		#
		# `max(submitted)` is the only defensible frontier: everything up to it has
		# demonstrably been covered, and everything after it has not been seen yet,
		# so the next run starts exactly where the evidence stops.
		frontier = max(p.submitted for p in papers)
		if frontier < end:
			log.info(
				"arXiv's index stops at %s, %.1fh before the requested window end; "
				"the marker follows the index, not the clock",
				frontier.isoformat(timespec="minutes"),
				(end - frontier).total_seconds() / 3600,
			)
		write_json(
			self.paths.fetch_window_json,
			{
				"start": start.isoformat(),
				"end": frontier.isoformat(),
				"requested_end": end.isoformat(),
				"papers": len(papers),
			},
		)
		log.info("Wrote %s papers to %s", len(papers), self.paths.papers_jsonl)
		return papers

	def window_end(self) -> datetime | None:
		"""The newest submission this run actually saw, if it was recorded.

		Named `end` on disk for continuity, but it is the index frontier rather
		than the requested end of the window - see `run()`.
		"""
		if not self.paths.fetch_window_json.exists():
			return None
		try:
			return datetime.fromisoformat(read_json(self.paths.fetch_window_json)["end"])
		except (KeyError, ValueError, TypeError):
			return None
