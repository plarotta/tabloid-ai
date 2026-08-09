"""Stage 2 - cheap-model screen from ~1700 papers down to ~15 candidates.

Cost, measured against a real 1,225-paper window (2026-08-04..06):

    batch=1    1225 calls   $1.29 haiku   $0.055 groq llama-3.1-8b
    batch=20     62 calls   $0.77 haiku   $0.029 groq llama-3.1-8b

Batching saves ~40% by amortising the system prompt, but the abstracts dominate
the bill (~490k input tokens regardless of batch size), which caps what batching
alone can achieve. $0.77 against a $1.00 stage target is thin headroom - a heavy
window (~1,750 papers) would breach it. Switching this stage to a cheaper hosted
model is the real lever if that happens.

Batches run concurrently so 62 sequential calls do not dominate wall-clock time.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..llm import MeteredClient, parse_json_response
from ..paths import read_json, write_json
from ..prompts import load_prompt
from ..schemas import Paper, PaperScore, ShortlistEntry, ShortlistResult
from ..stage import Stage, StageError

log = logging.getLogger(__name__)


def batched(items: list[Paper], size: int) -> list[list[Paper]]:
	if size < 1:
		raise ValueError("batch size must be >= 1")
	return [items[i : i + size] for i in range(0, len(items), size)]


def format_batch(papers: list[Paper]) -> str:
	"""One compact block per paper. Abstracts are truncated because the tail of a
	long abstract is almost never what decides the score, and the savings compound
	across ~1700 papers."""
	blocks = []
	for p in papers:
		abstract = p.abstract if len(p.abstract) <= 1500 else p.abstract[:1500] + "..."
		blocks.append(
			f"---\narxiv_id: {p.arxiv_id}\n"
			f"categories: {', '.join(p.categories)}\n"
			f"title: {p.title}\n"
			f"abstract: {abstract}"
		)
	return "\n".join(blocks)


class ShortlistStage(Stage):
	name = "shortlist"

	def is_complete(self) -> bool:
		return self.paths.shortlist_json.exists()

	def load(self) -> ShortlistResult:
		return ShortlistResult.model_validate(read_json(self.paths.shortlist_json))

	def _score_batch(
		self, client: MeteredClient, prompt, batch: list[Paper], index: int
	) -> list[PaperScore]:
		system, user = prompt.render(n_papers=len(batch), papers=format_batch(batch))
		spec = self.config.model_for(self.config.shortlist.stage_model)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
			payload = parse_json_response(resp.text)
		except Exception as e:
			# One bad batch should not lose the other ~1680 papers. Those papers
			# simply score nothing and drop out of contention.
			log.warning("Batch %s failed to score (%s); its papers are dropped", index, e)
			return []

		if not isinstance(payload, list):
			log.warning(
				"Batch %s returned %s, expected list; dropped", index, type(payload).__name__
			)
			return []

		valid_ids = {p.arxiv_id for p in batch}
		scores = []
		for item in payload:
			try:
				score = PaperScore.model_validate(item)
			except Exception as e:
				log.debug("Bad score object in batch %s: %s", index, e)
				continue
			# Guard against the model inventing or mangling IDs.
			if score.arxiv_id not in valid_ids:
				log.debug("Batch %s scored unknown id %s; dropped", index, score.arxiv_id)
				continue
			scores.append(score)
		return scores

	def run(self) -> ShortlistResult:
		from .fetch import FetchStage

		papers = FetchStage(self.ctx).load()
		if not papers:
			raise StageError("No papers from fetch stage; cannot shortlist.")

		cfg = self.config.shortlist
		spec = self.config.model_for(cfg.stage_model)
		prompt = load_prompt("shortlist")
		client = MeteredClient.for_stage(spec, self.tracker, self.name)
		batches = batched(papers, cfg.batch_size)

		log.info(
			"Scoring %s papers in %s batches with %s/%s (prompt %s)",
			len(papers),
			len(batches),
			spec.provider,
			spec.model,
			prompt.name,
		)

		all_scores: list[PaperScore] = []
		with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
			futures = [
				pool.submit(self._score_batch, client, prompt, batch, i)
				for i, batch in enumerate(batches)
			]
			for fut in futures:
				all_scores.extend(fut.result())

		if not all_scores:
			raise StageError(
				"Every shortlist batch failed. Check the model name in config.yaml "
				"and the provider API key."
			)

		coverage = len(all_scores) / len(papers)
		if coverage < 0.8:
			log.warning(
				"Only %.0f%% of papers were scored (%s/%s). Shortlist quality is degraded.",
				coverage * 100,
				len(all_scores),
				len(papers),
			)

		by_id = {p.arxiv_id: p for p in papers}
		ranked = sorted(all_scores, key=lambda s: s.overall, reverse=True)
		shortlist = [
			ShortlistEntry(paper=by_id[s.arxiv_id], score=s)
			for s in ranked
			if s.overall >= cfg.min_score
		][: cfg.shortlist_size]

		if not shortlist:
			raise StageError(
				f"No paper scored above min_score={cfg.min_score}. "
				f"Best was {ranked[0].overall:.1f}. Lower the threshold in config.yaml."
			)

		result = ShortlistResult(
			generated_at=datetime.now(UTC),
			total_candidates=len(papers),
			shortlist=shortlist,
			all_scores=ranked,
		)
		write_json(self.paths.shortlist_json, json.loads(result.model_dump_json()))
		log.info(
			"Shortlisted %s of %s papers ($%.3f)",
			len(shortlist),
			len(papers),
			self.tracker.stage_total(self.name),
		)
		return result
