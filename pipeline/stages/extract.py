"""Stage 5 - one frontier-model pass per finalist producing a PaperDigest.

This is the stage where fidelity is won or lost: every number that reaches the
narration comes from here, so the prompt requires a citation for each result and
this module drops any result that arrives without one.

Two safety properties beyond the prompt:

  - `key_figures` are validated against the figures Stage 3 actually produced.
    A hallucinated filename would render as a missing image, so invented files
    are dropped rather than passed downstream.
  - A paper that fails extraction twice is replaced by the next-ranked
    substitute (spec section 5), so an episode ships three papers rather than
    two. Substitutions are recorded in the result.

The full paper text is sent, but truncated - see MAX_FULLTEXT_CHARS.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from ..llm import MeteredClient, parse_json_response
from ..paths import read_json, write_json
from ..prompts import load_prompt
from ..schemas import EnrichedPaper, ExtractResult, PaperDigest, RankedPaper
from ..stage import Stage, StageError

log = logging.getLogger(__name__)


# A number worth verifying: has a decimal point, or is at least 3 digits. Small
# integers ("3 models", "5 tasks") appear everywhere and would only add noise.
_NUMERIC = re.compile(r"\d+\.\d+|\d{3,}")


def unverifiable_numbers(numbers: str, paper_text: str) -> list[str]:
	"""Numbers claimed in a result that do not appear anywhere in the paper.

	Substring matching, deliberately: it lets a legitimately rounded "80" match a
	paper's "80.4", while a fabricated "88.5" matches nothing. Measured on a real
	extraction that invented three of five reported figures - the citation check
	alone passed it, because the model cited a real table and then made up the
	contents.
	"""
	if not numbers or not paper_text:
		return []
	return [n for n in _NUMERIC.findall(numbers) if n not in paper_text]


def format_figures(paper: EnrichedPaper) -> str:
	"""The figure menu the model may choose from. Nothing else is a valid pick."""
	usable = paper.usable_figures
	if not usable:
		return "(none available - do not populate key_figures)"
	return "\n".join(f"- file: {f.file}\n  caption: {f.caption or '(no caption)'}" for f in usable)


class ExtractStage(Stage):
	name = "extract"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("extract") / "extract.json").exists()

	def load(self) -> ExtractResult:
		return ExtractResult.model_validate(
			read_json(self.paths.stage_dir("extract") / "extract.json")
		)

	def _fulltext(self, arxiv_id: str, paper: EnrichedPaper) -> tuple[str, bool]:
		"""Whole-paper prose from Stage 3, falling back to what we have.

		Returns (text, is_complete). `is_complete` is False when we are falling
		back to intro+conclusion, which matters for number verification: a
		fragment cannot disprove a number, so verification is skipped there rather
		than dropping legitimate results.

		Truncation is by character budget: papers vary from ~20k to ~230k
		characters, and the tail is usually appendices that do not carry the
		headline results.
		"""
		path = self.paths.paper_fulltext(arxiv_id)
		complete = path.exists()
		if complete:
			text = path.read_text(encoding="utf-8")
		else:
			# No LaTeX source (PDF-fallback papers). Intro + conclusion is thin but
			# still lets the stage produce a digest rather than dropping the paper.
			text = "\n\n".join(x for x in (paper.sections.intro, paper.sections.conclusion) if x)
			log.warning("%s: no full text; extracting from intro+conclusion only", arxiv_id)

		cap = self.config.extract.max_fulltext_chars
		if len(text) > cap:
			log.info("%s: full text %s chars, truncated to %s", arxiv_id, len(text), cap)
			text = text[:cap] + "\n\n[truncated]"
		return text, complete

	def _validate(
		self, payload: object, paper: EnrichedPaper, paper_text: str, can_verify: bool
	) -> PaperDigest:
		"""Coerce the model's JSON into a PaperDigest, enforcing what the prompt
		can only request."""
		if not isinstance(payload, dict):
			raise ValueError(f"expected a JSON object, got {type(payload).__name__}")

		# The model sometimes echoes a different id; the caller knows the truth.
		payload = dict(payload)
		payload["arxiv_id"] = paper.arxiv_id

		digest = PaperDigest.model_validate(payload)

		# Fidelity rule, part 1: a result with no citation cannot be traced back to
		# the paper, so it must not reach narration.
		kept = [r for r in digest.headline_results if r.source.strip()]
		if len(kept) != len(digest.headline_results):
			log.warning(
				"%s: dropped %s headline result(s) with no source citation",
				paper.arxiv_id,
				len(digest.headline_results) - len(kept),
			)

		# Fidelity rule, part 2: the numbers must actually appear in the paper.
		# Citing a real table and then inventing its contents passes part 1 - this
		# was observed on a real extraction, so it is checked rather than trusted.
		if self.config.extract.verify_numbers and can_verify:
			verified = []
			for r in kept:
				missing = unverifiable_numbers(r.numbers, paper_text)
				if missing:
					log.warning(
						"%s: DROPPED result citing %r - number(s) %s appear nowhere in "
						"the paper (claimed: %r)",
						paper.arxiv_id,
						r.source,
						missing,
						r.numbers,
					)
					continue
				verified.append(r)
			kept = verified

		digest.headline_results = kept

		# Figures must exist. A hallucinated path renders as a missing image.
		valid = {f.file for f in paper.usable_figures if f.file}
		real = [k for k in digest.key_figures if k.file in valid]
		if len(real) != len(digest.key_figures):
			bad = [k.file for k in digest.key_figures if k.file not in valid]
			log.warning("%s: dropped invented figure reference(s) %s", paper.arxiv_id, bad)
		digest.key_figures = real

		if not digest.one_sentence_claim.strip():
			raise ValueError("one_sentence_claim is empty")
		if not digest.headline_results:
			raise ValueError("no headline results survived citation validation")
		return digest

	def _extract_one(
		self, client: MeteredClient, prompt, paper: EnrichedPaper
	) -> PaperDigest | None:
		"""One paper, with a single retry on validation failure (spec Stage 5)."""
		spec = self.config.model_for(self.config.extract.stage_model)
		fulltext, can_verify = self._fulltext(paper.arxiv_id, paper)
		system, user = prompt.render(
			arxiv_id=paper.arxiv_id,
			title=paper.paper.title,
			figures=format_figures(paper),
			abstract=paper.paper.abstract,
			fulltext=fulltext,
		)

		last: Exception | None = None
		for attempt in (1, 2):
			try:
				resp = client.complete(
					user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
				)
				return self._validate(parse_json_response(resp.text), paper, fulltext, can_verify)
			except Exception as e:
				# Deliberately broad: pydantic raises ValidationError, the JSON
				# parser raises LLMError, and a provider can raise anything. All of
				# them mean the same thing here - this attempt yielded no usable
				# digest - and the retry/substitute path handles all of them alike.
				last = e
				log.warning("%s: extract attempt %s failed (%s)", paper.arxiv_id, attempt, e)

		log.error("%s: extraction failed twice; %s", paper.arxiv_id, last)
		return None

	def run(self) -> ExtractResult:
		from .enrich import EnrichStage
		from .rank import RankStage

		ranking = RankStage(self.ctx).load()
		enriched = {p.arxiv_id: p for p in EnrichStage(self.ctx).load().papers}

		targets: list[RankedPaper] = list(ranking.finalists)
		if self.ctx.paper_filter:
			targets = [t for t in targets if t.arxiv_id == self.ctx.paper_filter]
			if not targets:
				raise StageError(f"Paper {self.ctx.paper_filter!r} is not a finalist.")

		spec = self.config.model_for(self.config.extract.stage_model)
		prompt = load_prompt("extract")
		client = MeteredClient.for_stage(spec, self.tracker, self.name)

		log.info(
			"Extracting %s finalist(s) with %s/%s (prompt %s)",
			len(targets),
			spec.provider,
			spec.model,
			prompt.name,
		)

		digests: list[PaperDigest] = []
		failed: list[str] = []
		substituted: list[str] = []
		# Substitutes are consumed in rank order when a finalist fails.
		queue = list(ranking.substitutes)

		for entry in targets:
			paper = enriched.get(entry.arxiv_id)
			if paper is None:
				log.warning("%s: ranked but not enriched; skipping", entry.arxiv_id)
				failed.append(entry.arxiv_id)
				continue

			digest = self._extract_one(client, prompt, paper)
			while digest is None and queue and self.config.extract.substitute_on_failure:
				failed.append(entry.arxiv_id)
				nxt = queue.pop(0)
				replacement = enriched.get(nxt.arxiv_id)
				if replacement is None:
					continue
				log.warning(
					"Substituting %s for %s (spec section 5 failure policy)",
					nxt.arxiv_id,
					entry.arxiv_id,
				)
				digest = self._extract_one(client, prompt, replacement)
				if digest is not None:
					substituted.append(f"{entry.arxiv_id}->{nxt.arxiv_id}")

			if digest is None:
				if entry.arxiv_id not in failed:
					failed.append(entry.arxiv_id)
				continue
			digests.append(digest)
			log.info(
				"  %s: %s result(s), %s figure(s), %s caveat(s)",
				digest.arxiv_id,
				len(digest.headline_results),
				len(digest.key_figures),
				len(digest.honest_caveats),
			)

		if not digests:
			raise StageError(
				"No paper produced a usable digest. Check prompts/extract/ and the "
				"model's max_tokens; a truncated response is the usual cause."
			)
		if len(digests) < self.config.extract.min_digests:
			raise StageError(
				f"Only {len(digests)} digest(s) extracted, below "
				f"extract.min_digests={self.config.extract.min_digests}. "
				f"Failed: {', '.join(failed) or 'none'}"
			)

		result = ExtractResult(
			generated_at=datetime.now(UTC),
			digests=digests,
			failed=failed,
			substituted=substituted,
		)
		write_json(
			self.paths.stage_dir("extract") / "extract.json",
			json.loads(result.model_dump_json()),
		)
		log.info(
			"Extracted %s digest(s)%s ($%.3f)",
			len(digests),
			f", {len(substituted)} substitution(s)" if substituted else "",
			self.tracker.stage_total(self.name),
		)
		return result
