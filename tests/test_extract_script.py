"""Stages 5 and 6 with the LLM mocked. No network, no spend.

The focus is the two guarantees the prompts can only ask for and this code must
enforce: results carry a real citation, and figures reference files that exist.
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
	ExtractResult,
	FigureAsset,
	PaperDigest,
	PaperSections,
	RankedPaper,
	RankResult,
	SignalsModel,
)
from pipeline.stage import StageError
from pipeline.stages.extract import ExtractStage, format_figures
from pipeline.stages.script import ScriptStage, digest_to_prompt_text, scenes_to_markdown
from tests.conftest import make_paper


class ScriptedLLM(LLMClient):
	"""Returns queued payloads in order; repeats the last one forever."""

	provider = "anthropic"

	def __init__(self, payloads, model="claude-sonnet-4-5-20250929"):
		super().__init__(model, api_key="x")
		self.payloads = list(payloads)
		self.calls = 0

	def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
		self.calls += 1
		p = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
		text = p if isinstance(p, str) else json.dumps(p)
		return LLMResponse(text, 500, 200, self.model, self.provider)


def make_enriched(pid: str, n_figures: int = 2) -> EnrichedPaper:
	return EnrichedPaper(
		arxiv_id=pid,
		paper=make_paper(pid, title=f"Title {pid}"),
		source_quality="latex",
		figures=[
			FigureAsset(
				number=i + 1,
				file=f"figures/fig{i + 1:02d}.png",
				caption=f"Caption {i + 1}",
				converted=True,
			)
			for i in range(n_figures)
		],
		sections=PaperSections(intro="An intro.", conclusion="A conclusion."),
		signals=SignalsModel(hf_lookup_ok=True),
	)


def digest_payload(pid: str, results=None, figures=None) -> dict:
	return {
		"arxiv_id": pid,
		"one_sentence_claim": "The method improves accuracy.",
		"problem_context": "Prior work could not do this.",
		"method_summary": "It works by doing a thing.",
		"headline_results": results
		if results is not None
		else [{"statement": "Accuracy rose", "source": "Table 2", "numbers": "41.2%"}],
		"key_figures": figures if figures is not None else [{"file": "figures/fig01.png"}],
		"honest_caveats": ["Only tested on one dataset."],
		"why_it_matters": "It matters.",
	}


def seed(ctx, papers, finalists, substitutes=()):
	write_json(
		ctx.paths.enriched_dir / "enrich.json",
		json.loads(EnrichResult(generated_at=datetime.now(UTC), papers=papers).model_dump_json()),
	)
	write_json(
		ctx.paths.stage_dir("rank") / "rank.json",
		json.loads(
			RankResult(
				generated_at=datetime.now(UTC),
				finalists=[
					RankedPaper(arxiv_id=p, rank=i + 1, justification="Because.")
					for i, p in enumerate(finalists)
				],
				substitutes=[
					RankedPaper(arxiv_id=p, rank=i + 10, justification="Backup.")
					for i, p in enumerate(substitutes)
				],
			).model_dump_json()
		),
	)


def patch_llm(monkeypatch, fake):
	monkeypatch.setattr(
		MeteredClient, "for_stage", classmethod(lambda cls, s, t, n, **kw: cls(fake, t, n))
	)


# --- extract -----------------------------------------------------------------


def test_format_figures_lists_only_usable():
	paper = make_enriched("2608.00001", n_figures=1)
	paper.figures.append(FigureAsset(number=9, file=None, caption="Nope", converted=False))
	block = format_figures(paper)
	assert "figures/fig01.png" in block and "Nope" not in block


def test_extract_produces_digest(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	patch_llm(monkeypatch, ScriptedLLM([digest_payload("2608.00001")]))
	result = ExtractStage(ctx).run()
	assert len(result.digests) == 1
	assert result.digests[0].headline_results[0].source == "Table 2"


def test_results_without_citation_are_dropped(ctx, monkeypatch):
	"""The fidelity rule: a result with no source cannot reach narration."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	payload = digest_payload(
		"2608.00001",
		results=[
			{"statement": "Cited", "source": "Figure 3", "numbers": "10"},
			{"statement": "Uncited", "source": "  ", "numbers": "99"},
		],
	)
	patch_llm(monkeypatch, ScriptedLLM([payload]))
	digest = ExtractStage(ctx).run().digests[0]
	assert [r.statement for r in digest.headline_results] == ["Cited"]


def test_invented_figure_reference_is_dropped(ctx, monkeypatch):
	papers = [make_enriched("2608.00001", n_figures=1)]
	seed(ctx, papers, ["2608.00001"])
	payload = digest_payload(
		"2608.00001",
		figures=[{"file": "figures/fig01.png"}, {"file": "figures/does_not_exist.png"}],
	)
	patch_llm(monkeypatch, ScriptedLLM([payload]))
	digest = ExtractStage(ctx).run().digests[0]
	assert [k.file for k in digest.key_figures] == ["figures/fig01.png"]


def test_extract_retries_once_then_succeeds(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	fake = ScriptedLLM(["not json", digest_payload("2608.00001")])
	patch_llm(monkeypatch, fake)
	assert len(ExtractStage(ctx).run().digests) == 1
	assert fake.calls == 2


def test_failed_paper_is_substituted(ctx, monkeypatch):
	"""Spec section 5: ship three papers, not two."""
	papers = [make_enriched("2608.00001"), make_enriched("2608.00002")]
	seed(ctx, papers, ["2608.00001"], substitutes=["2608.00002"])
	# Two failures for the finalist, then a good digest for the substitute.
	fake = ScriptedLLM(["nope", "nope", digest_payload("2608.00002")])
	patch_llm(monkeypatch, fake)
	result = ExtractStage(ctx).run()
	assert [d.arxiv_id for d in result.digests] == ["2608.00002"]
	assert result.substituted == ["2608.00001->2608.00002"]
	assert "2608.00001" in result.failed


def test_all_failures_raise(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	patch_llm(monkeypatch, ScriptedLLM(["not json at all"]))
	with pytest.raises(StageError):
		ExtractStage(ctx).run()


def test_digest_with_no_surviving_results_fails(ctx, monkeypatch):
	"""A digest whose every result lacked a citation is not usable."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	payload = digest_payload("2608.00001", results=[{"statement": "x", "source": ""}])
	patch_llm(monkeypatch, ScriptedLLM([payload]))
	with pytest.raises(StageError):
		ExtractStage(ctx).run()


# --- script ------------------------------------------------------------------


def scene(sid="s1", vtype="title_card", figure=None, narration="Some spoken words here."):
	return {
		"id": sid,
		"narration": narration,
		"visual": {
			"type": vtype,
			"figure_file": figure,
			"title": "T",
			"bullets": [],
			"highlight": None,
		},
		"est_seconds": 10,
	}


def script_payload(pid, scenes=None):
	return {
		"arxiv_id": pid,
		"scenes": scenes or [scene(), scene("s2", "figure", "figures/fig01.png")],
	}


def episode_payload():
	return {
		"title": "A short title",
		"description": "Desc",
		"thumbnail_text_options": ["A", "B", "C"],
		"cold_open": {"arxiv_id": "episode", "scenes": [scene("c1")]},
		"outro": {"arxiv_id": "episode", "scenes": [scene("o1")]},
	}


def seed_extract(ctx, digests):
	write_json(
		ctx.paths.stage_dir("extract") / "extract.json",
		json.loads(
			ExtractResult(generated_at=datetime.now(UTC), digests=digests).model_dump_json()
		),
	)


def test_script_writes_segments_and_markdown(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	patch_llm(monkeypatch, ScriptedLLM([script_payload("2608.00001"), episode_payload()]))

	result = ScriptStage(ctx).run()
	assert len(result.segments) == 1
	assert result.est_seconds > 0
	md = ctx.paths.stage_dir("script") / "segment_2608.00001.md"
	assert md.exists() and "Some spoken words here." in md.read_text()
	assert (ctx.paths.stage_dir("script") / "episode.md").exists()


def test_invented_figure_downgrades_to_bullet_slide(ctx, monkeypatch):
	"""A figure path that does not exist would render as a missing image."""
	papers = [make_enriched("2608.00001", n_figures=1)]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	bad = script_payload("2608.00001", [scene(), scene("s2", "figure", "figures/ghost.png")])
	patch_llm(monkeypatch, ScriptedLLM([bad, episode_payload()]))

	seg = ScriptStage(ctx).run().segments[0]
	assert seg.scenes[1].visual.type == "bullet_slide"
	assert seg.scenes[1].visual.figure_file is None


def test_episode_wrapper_failure_does_not_lose_segments(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	patch_llm(monkeypatch, ScriptedLLM([script_payload("2608.00001"), "garbage"]))

	result = ScriptStage(ctx).run()
	assert len(result.segments) == 1
	assert result.episode.title == ""  # degraded, not fatal


def test_all_segments_failing_raises(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	patch_llm(monkeypatch, ScriptedLLM(["not json"]))
	with pytest.raises(StageError):
		ScriptStage(ctx).run()


def test_digest_to_prompt_text_includes_citations():
	d = PaperDigest.model_validate(digest_payload("2608.00001"))
	text = digest_to_prompt_text(d)
	assert "source: Table 2" in text and "41.2%" in text


def test_scenes_to_markdown_reports_pacing():
	from pipeline.schemas import SceneManifest

	m = SceneManifest.model_validate(script_payload("2608.00001"))
	md = scenes_to_markdown(m, "A Title")
	assert "A Title" in md and "2 scenes" in md and "20s" in md


# --- number verification (the fidelity rule that citations alone miss) --------


def test_unverifiable_numbers_flags_only_fabricated():
	from pipeline.stages.extract import unverifiable_numbers

	paper = "we report 80.4% on the first split and 99.0% overall across 1200 runs"
	# Real, and a legitimately rounded restatement of 80.4.
	assert unverifiable_numbers("80.4% and 99.0%", paper) == []
	assert unverifiable_numbers("80% overall", paper) == []
	# Fabricated.
	assert unverifiable_numbers("88.5% and 93.8%", paper) == ["88.5", "93.8"]
	# Small integers are not checked - they appear everywhere.
	assert unverifiable_numbers("3 models, 5 tasks", paper) == []


def test_result_with_fabricated_numbers_is_dropped(ctx, monkeypatch, tmp_path):
	"""Regression: a real extraction cited 'Section 5, Table 1' and invented
	three of the five percentages it reported. The citation check passed it."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	# The paper text the stage will verify against.
	fulltext = ctx.paths.enriched_dir / "2608.00001" / "fulltext.txt"
	fulltext.parent.mkdir(parents=True, exist_ok=True)
	fulltext.write_text("The model reached 80.4 percent and 99.0 percent.", encoding="utf-8")

	payload = digest_payload(
		"2608.00001",
		results=[
			{"statement": "Real", "source": "Table 1", "numbers": "80.4%"},
			{"statement": "Invented", "source": "Table 1", "numbers": "88.5%, 93.8%"},
		],
	)
	patch_llm(monkeypatch, ScriptedLLM([payload]))
	digest = ExtractStage(ctx).run().digests[0]
	assert [r.statement for r in digest.headline_results] == ["Real"]


def test_verification_can_be_disabled(ctx, monkeypatch):
	ctx.config.extract.verify_numbers = False
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	payload = digest_payload(
		"2608.00001",
		results=[{"statement": "Unchecked", "source": "Table 1", "numbers": "88.5%"}],
	)
	patch_llm(monkeypatch, ScriptedLLM([payload]))
	assert len(ExtractStage(ctx).run().digests[0].headline_results) == 1
