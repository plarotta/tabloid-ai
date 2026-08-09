"""Stage 4 behaviour, with the LLM mocked. No network, no spend.

Focus is on the failure modes that would silently produce a bad episode:
hallucinated IDs, duplicate entries, and the two hard constraints (figures and
subfield diversity) that the prompt asks for but cannot guarantee.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pipeline.llm import MeteredClient
from pipeline.llm.base import LLMClient, LLMResponse
from pipeline.paths import write_json
from pipeline.schemas import (
	EnrichedPaper,
	EnrichResult,
	FigureAsset,
	PaperSections,
	RankResult,
	SignalsModel,
)
from pipeline.stage import StageError
from pipeline.stages.rank import RankStage, format_candidate
from tests.conftest import make_paper


class FakeRankLLM(LLMClient):
	"""Returns a scripted ranking."""

	provider = "anthropic"

	def __init__(self, payload, model="claude-sonnet-4-5-20250929", fail=False):
		super().__init__(model, api_key="x")
		self.payload = payload
		self.fail = fail
		self.calls = 0
		self.last_prompt = ""

	def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
		self.calls += 1
		self.last_prompt = prompt
		if self.fail:
			raise RuntimeError("provider exploded")
		text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
		return LLMResponse(text, 1000, 200, self.model, self.provider)


def make_enriched(pid: str, n_figures: int = 2, subfield: str = "", captions=True) -> EnrichedPaper:
	figs = [
		FigureAsset(
			number=i + 1,
			file=f"figures/fig{i + 1:02d}.png",
			caption=f"Caption {i + 1}" if captions else "",
			converted=True,
		)
		for i in range(n_figures)
	]
	return EnrichedPaper(
		arxiv_id=pid,
		paper=make_paper(pid, title=f"Title {pid}"),
		source_quality="latex",
		figures=figs,
		sections=PaperSections(intro="An intro.", conclusion="A conclusion."),
		signals=SignalsModel(hf_lookup_ok=True),
	)


def seed_enriched(ctx, papers: list[EnrichedPaper]) -> None:
	result = EnrichResult(generated_at=datetime.now(UTC), papers=papers)
	write_json(ctx.paths.enriched_dir / "enrich.json", json.loads(result.model_dump_json()))


def run_rank(ctx, papers, payload, monkeypatch) -> RankResult:
	seed_enriched(ctx, papers)
	fake = FakeRankLLM(payload)
	stage = RankStage(ctx)
	monkeypatch.setattr(
		MeteredClient, "for_stage", classmethod(lambda cls, s, t, n, **kw: cls(fake, t, n))
	)
	return stage.run(), fake


def rank_payload(ids, subfields=None):
	subfields = subfields or {}
	return [
		{
			"arxiv_id": pid,
			"rank": i + 1,
			"subfield": subfields.get(pid, f"sub{i}"),
			"justification": f"Justification for {pid}.",
		}
		for i, pid in enumerate(ids)
	]


# --- prompt construction -----------------------------------------------------


def test_format_candidate_includes_captions_and_signal():
	paper = make_enriched("2608.00001", n_figures=2)
	block = format_candidate(paper)
	assert "usable_figures: 2" in block
	assert "Caption 1" in block
	assert "huggingface_daily: no" in block


def test_format_candidate_marks_failed_hf_lookup_as_unknown():
	"""A failed lookup must not read as 'not featured'."""
	paper = make_enriched("2608.00001")
	paper.signals = SignalsModel(hf_lookup_ok=False)
	assert "unknown (lookup failed)" in format_candidate(paper)


def test_format_candidate_omits_unusable_figures():
	paper = make_enriched("2608.00001", n_figures=1)
	paper.figures.append(FigureAsset(number=9, file=None, caption="Nope", converted=False))
	block = format_candidate(paper)
	assert "usable_figures: 1" in block
	assert "Nope" not in block


# --- ranking -----------------------------------------------------------------


def test_ranking_selects_finalists_and_substitutes(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(6)]
	ids = [p.arxiv_id for p in papers]
	result, fake = run_rank(ctx, papers, rank_payload(ids), monkeypatch)

	assert fake.calls == 1, "ranking must be a single comparative call"
	assert [f.arxiv_id for f in result.finalists] == ids[:3]
	assert [f.rank for f in result.finalists] == [1, 2, 3]
	assert [s.arxiv_id for s in result.substitutes] == ids[3:6]


def test_hallucinated_ids_are_dropped(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	payload = rank_payload([p.arxiv_id for p in papers] + ["9999.99999"])
	result, _ = run_rank(ctx, papers, payload, monkeypatch)
	assert "9999.99999" not in [r.arxiv_id for r in result.finalists + result.substitutes]


def test_duplicate_ids_keep_first_only(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	ids = [p.arxiv_id for p in papers]
	result, _ = run_rank(ctx, papers, rank_payload([*ids, ids[0]]), monkeypatch)
	all_ids = [r.arxiv_id for r in result.finalists + result.substitutes]
	assert len(all_ids) == len(set(all_ids))


def test_papers_without_figures_cannot_be_finalists(ctx, monkeypatch):
	"""The format is built around showing figures; a paper we cannot show loses."""
	papers = [
		make_enriched("2608.00000", n_figures=0),  # ranked first by the model
		make_enriched("2608.00001"),
		make_enriched("2608.00002"),
		make_enriched("2608.00003"),
	]
	ids = [p.arxiv_id for p in papers]
	result, _ = run_rank(ctx, papers, rank_payload(ids), monkeypatch)
	assert "2608.00000" not in [f.arxiv_id for f in result.finalists]
	assert "2608.00000" in [s.arxiv_id for s in result.substitutes]


def test_subfield_diversity_is_enforced(ctx, monkeypatch):
	"""Three papers from one subfield must not all win."""
	papers = [make_enriched(f"2608.0000{i}") for i in range(5)]
	ids = [p.arxiv_id for p in papers]
	subfields = {ids[0]: "rl", ids[1]: "rl", ids[2]: "rl", ids[3]: "vision", ids[4]: "nlp"}
	result, _ = run_rank(ctx, papers, rank_payload(ids, subfields), monkeypatch)

	finalist_subfields = [f.subfield for f in result.finalists]
	assert finalist_subfields.count("rl") == 2
	assert ids[2] not in [f.arxiv_id for f in result.finalists]
	# The deferred paper is the strongest substitute, not discarded.
	assert result.substitutes[0].arxiv_id == ids[2]


def test_two_papers_from_same_subfield_is_allowed(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(3)]
	ids = [p.arxiv_id for p in papers]
	subfields = {ids[0]: "rl", ids[1]: "rl", ids[2]: "vision"}
	result, _ = run_rank(ctx, papers, rank_payload(ids, subfields), monkeypatch)
	assert len(result.finalists) == 3


def test_ranking_is_persisted(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	result, _ = run_rank(ctx, papers, rank_payload([p.arxiv_id for p in papers]), monkeypatch)
	saved = RankStage(ctx).load()
	assert [f.arxiv_id for f in saved.finalists] == [f.arxiv_id for f in result.finalists]
	assert RankStage(ctx).is_complete()


# --- failure modes -----------------------------------------------------------


def test_unparseable_response_raises(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	with pytest.raises(StageError):
		run_rank(ctx, papers, "not json at all", monkeypatch)


def test_object_instead_of_array_raises(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	with pytest.raises(StageError):
		run_rank(ctx, papers, {"oops": True}, monkeypatch)


def test_too_few_usable_entries_raises(ctx, monkeypatch):
	"""Two valid entries cannot fill three finalist slots."""
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	payload = rank_payload([p.arxiv_id for p in papers[:2]])
	with pytest.raises(StageError, match="only 2"):
		run_rank(ctx, papers, payload, monkeypatch)


def test_provider_failure_raises_stage_error(ctx, monkeypatch):
	papers = [make_enriched(f"2608.0000{i}") for i in range(4)]
	seed_enriched(ctx, papers)
	fake = FakeRankLLM(rank_payload([]), fail=True)
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, s, t, n, **kw: cls(fake, t, n, max_retries=1)),
	)
	with pytest.raises(StageError, match="Ranking call failed"):
		RankStage(ctx).run()


def test_too_few_enriched_papers_raises(ctx, monkeypatch):
	seed_enriched(ctx, [make_enriched("2608.00001")])
	with pytest.raises(StageError, match="finalists"):
		RankStage(ctx).run()
