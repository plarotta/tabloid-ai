"""Stage 4 - frontier-model ranking of enriched candidates down to 3 finalists.

One call, not a batch: ~15 candidates is small enough to rank in a single pass,
and ranking is inherently comparative - splitting it into batches would produce
orderings that cannot be merged without a second pass.

Per the spec this stage sees abstract + intro + conclusion + figure captions +
signals, and never the full paper text. That bound is what keeps the stage
inside its $2 target; the full text is Stage 5's job, on 3 papers rather than 15.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..llm import LLMError, MeteredClient, parse_json_response
from ..paths import read_json, write_json
from ..prompts import load_prompt
from ..schemas import EnrichedPaper, RankedPaper, RankResult
from ..stage import Stage, StageError

log = logging.getLogger(__name__)

# Per-paper caps on the text sent to the model. The intro carries most of the
# framing value in its opening; the tail is usually related-work throat-clearing.
MAX_ABSTRACT_CHARS = 2000
MAX_INTRO_CHARS = 2500
MAX_CONCLUSION_CHARS = 1500
MAX_CAPTION_CHARS = 300
MAX_CAPTIONS = 8


def format_candidate(paper: EnrichedPaper) -> str:
	"""One block per paper. Figure captions are included because they are the
	best available evidence of whether a paper has watchable visuals."""
	p = paper.paper
	usable = paper.usable_figures
	lines = [
		"---",
		f"arxiv_id: {p.arxiv_id}",
		f"title: {p.title}",
		f"categories: {', '.join(p.categories)}",
		f"abstract: {p.abstract[:MAX_ABSTRACT_CHARS]}",
		f"usable_figures: {len(usable)}",
	]

	sig = paper.signals
	signal_bits = []
	if sig.github_url:
		signal_bits.append(f"github: {sig.github_url}")
	if sig.hf_lookup_ok:
		signal_bits.append(
			f"huggingface_daily: {'yes' if sig.huggingface else 'no'}"
			+ (f" ({sig.hf_upvotes} upvotes)" if sig.huggingface else "")
		)
	else:
		# Say so explicitly - absent evidence is not evidence of absence, and the
		# prompt should not read a failed lookup as "not featured".
		signal_bits.append("huggingface_daily: unknown (lookup failed)")
	if paper.source_quality != "latex":
		signal_bits.append(f"source_quality: {paper.source_quality}")
	lines.append(f"signals: {'; '.join(signal_bits)}")

	if paper.sections.intro:
		lines.append(f"introduction: {paper.sections.intro[:MAX_INTRO_CHARS]}")
	if paper.sections.conclusion:
		lines.append(f"conclusion: {paper.sections.conclusion[:MAX_CONCLUSION_CHARS]}")

	captions = [f for f in usable if f.caption][:MAX_CAPTIONS]
	if captions:
		lines.append("figure_captions:")
		lines.extend(f"  fig{f.number}: {f.caption[:MAX_CAPTION_CHARS]}" for f in captions)
	elif usable:
		lines.append(f"figure_captions: none available ({len(usable)} images, no captions)")

	return "\n".join(lines)


class RankStage(Stage):
	name = "rank"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("rank") / "rank.json").exists()

	def load(self) -> RankResult:
		return RankResult.model_validate(read_json(self.paths.stage_dir("rank") / "rank.json"))

	def _parse(self, text: str, valid: dict[str, EnrichedPaper]) -> list[RankedPaper]:
		try:
			payload = parse_json_response(text)
		except LLMError as e:
			# Surface as a StageError so the CLI prints a clear message rather
			# than a traceback; the spec requires failing loudly *and* legibly.
			raise StageError(
				f"Rank model did not return parseable JSON: {e}. "
				f"Check prompts/rank/ and the model's max_tokens (a truncated "
				f"response is the usual cause)."
			) from e
		if not isinstance(payload, list):
			raise StageError(
				f"Rank model returned {type(payload).__name__}, expected a JSON array."
			)

		ranked: list[RankedPaper] = []
		seen: set[str] = set()
		for item in payload:
			try:
				entry = RankedPaper.model_validate(item)
			except Exception as e:
				log.warning("Dropping malformed rank entry %r (%s)", item, e)
				continue
			if entry.arxiv_id not in valid:
				log.warning("Rank model returned unknown id %s; dropped", entry.arxiv_id)
				continue
			if entry.arxiv_id in seen:
				log.warning("Rank model repeated id %s; keeping first", entry.arxiv_id)
				continue
			seen.add(entry.arxiv_id)
			ranked.append(entry)

		# Trust the array order over the model's own `rank` field: models are more
		# reliable at ordering than at numbering, and the two sometimes disagree.
		ranked.sort(key=lambda r: r.rank)
		for i, entry in enumerate(ranked, start=1):
			entry.rank = i
		return ranked

	def _apply_constraints(
		self, ranked: list[RankedPaper], by_id: dict[str, EnrichedPaper]
	) -> list[RankedPaper]:
		"""Enforce in code what the prompt only asks for.

		The prompt states both rules, but a ranking that silently violates them
		produces a bad episode, so they are also enforced here where they cannot
		be ignored.
		"""
		cfg = self.config.rank

		if cfg.require_figures:
			eligible = [r for r in ranked if by_id[r.arxiv_id].usable_figures]
			dropped = len(ranked) - len(eligible)
			if dropped:
				log.info("%s candidate(s) excluded from winning: no usable figure", dropped)
			# Keep the figure-less papers, but only behind everything showable.
			ranked = eligible + [r for r in ranked if not by_id[r.arxiv_id].usable_figures]

		# Subfield diversity: no more than 2 of the finalists share a subfield.
		finalists: list[RankedPaper] = []
		deferred: list[RankedPaper] = []
		counts: dict[str, int] = {}
		for entry in ranked:
			if len(finalists) >= cfg.finalists:
				break
			key = (entry.subfield or "").strip().lower()
			if key and counts.get(key, 0) >= 2:
				log.info(
					"Deferring %s: subfield %r already has 2 finalists",
					entry.arxiv_id,
					entry.subfield,
				)
				deferred.append(entry)
				continue
			finalists.append(entry)
			if key:
				counts[key] = counts.get(key, 0) + 1

		# Anything not promoted stays available as a substitute, deferred papers
		# first - they were ranked higher and only lost on diversity.
		chosen = {r.arxiv_id for r in finalists}
		deferred_ids = {r.arxiv_id for r in deferred}
		rest = deferred + [
			r for r in ranked if r.arxiv_id not in chosen and r.arxiv_id not in deferred_ids
		]
		for i, entry in enumerate(finalists, start=1):
			entry.rank = i
		for i, entry in enumerate(rest, start=len(finalists) + 1):
			entry.rank = i
		return finalists + rest

	def run(self) -> RankResult:
		from .enrich import EnrichStage

		enriched = EnrichStage(self.ctx).load().papers
		if not enriched:
			raise StageError("No enriched papers to rank; run the enrich stage first.")

		cfg = self.config.rank
		if len(enriched) < cfg.finalists:
			raise StageError(
				f"Only {len(enriched)} enriched paper(s) but {cfg.finalists} finalists "
				f"are required. Widen the fetch window or lower rank.finalists."
			)

		spec = self.config.model_for(cfg.stage_model)
		prompt = load_prompt("rank")
		client = MeteredClient.for_stage(spec, self.tracker, self.name)
		by_id = {p.arxiv_id: p for p in enriched}

		blocks = "\n".join(format_candidate(p) for p in enriched)
		system, user = prompt.render(n_papers=len(enriched), papers=blocks)

		log.info(
			"Ranking %s candidates with %s/%s (prompt %s)",
			len(enriched),
			spec.provider,
			spec.model,
			prompt.name,
		)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
		except Exception as e:
			# Unlike shortlist, there is no partial result worth keeping: a failed
			# ranking means no episode.
			raise StageError(f"Ranking call failed: {e}") from e

		ranked = self._parse(resp.text, by_id)
		if len(ranked) < cfg.finalists:
			raise StageError(
				f"Ranking returned only {len(ranked)} usable entries, need "
				f"{cfg.finalists}. Model output began: {resp.text[:200]!r}"
			)

		ordered = self._apply_constraints(ranked, by_id)
		finalists = ordered[: cfg.finalists]
		substitutes = ordered[cfg.finalists : cfg.substitution_depth]

		result = RankResult(
			generated_at=datetime.now(UTC), finalists=finalists, substitutes=substitutes
		)
		write_json(self.paths.stage_dir("rank") / "rank.json", json.loads(result.model_dump_json()))

		for entry in finalists:
			log.info(
				"  %s. %s [%s] - %s",
				entry.rank,
				entry.arxiv_id,
				entry.subfield or "?",
				by_id[entry.arxiv_id].paper.title[:60],
			)
		log.info(
			"Ranked %s candidates -> %s finalists, %s substitutes ($%.3f)",
			len(enriched),
			len(finalists),
			len(substitutes),
			self.tracker.stage_total(self.name),
		)
		return result
