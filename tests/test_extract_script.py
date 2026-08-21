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
	EpisodeMetadata,
	ExtractResult,
	FigureAsset,
	PaperDigest,
	PaperSections,
	RankedPaper,
	RankResult,
	SceneManifest,
	ScriptResult,
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


def bridge(pid, narration="And that is only one way in."):
	return {
		"into_arxiv_id": pid,
		"narration": narration,
		"label": "Another way in",
		"est_seconds": 5,
	}


def episode_payload(transitions=None):
	payload = {
		"title": "A short title",
		"description": "Desc",
		"thumbnail_text_options": ["A", "B", "C"],
		"cold_open": {"arxiv_id": "episode", "scenes": [scene("c1")]},
		"outro": {"arxiv_id": "episode", "scenes": [scene("o1")]},
	}
	if transitions is not None:
		payload["transitions"] = transitions
	return payload


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


# --- length budget -----------------------------------------------------------


def _budget(ctx, n_scenes, words_each=6, target_words=200):
	m = SceneManifest.model_validate(
		{
			"arxiv_id": "2608.00001",
			"scenes": [
				{
					"id": f"s{i + 1}",
					"narration": " ".join(["word"] * words_each),
					"visual": {"type": "title_card", "title": f"T{i + 1}"},
					"est_seconds": 10,
				}
				for i in range(n_scenes)
			],
		}
	)
	return ScriptStage(ctx)._enforce_budget(m, target_words)


def test_a_segment_within_the_cap_is_untouched(ctx):
	assert len(_budget(ctx, 6).scenes) == 6


def test_an_overlong_segment_keeps_its_caveat_and_close(ctx):
	"""Trimming from the end would take the close, which is the one scene the
	segment must not lose. The middle goes instead."""
	ctx.config.script.max_scenes_per_segment = 6
	out = _budget(ctx, 9)
	ids = [s.id for s in out.scenes]
	assert len(ids) == 6
	assert ids[0] == "s1", "the standalone cut still opens on its title card"
	assert ids[-2:] == ["s8", "s9"], "caveat then close survive"


def test_the_cap_never_produces_an_empty_segment(ctx):
	ctx.config.script.max_scenes_per_segment = 2
	assert len(_budget(ctx, 8).scenes) >= 2


def test_running_over_the_word_budget_is_reported_not_cut(ctx, caplog):
	"""Words cannot be trimmed safely in code, so an overlong segment has to be
	loud rather than silently mangled."""
	import logging

	with caplog.at_level(logging.ERROR):
		out = _budget(ctx, 5, words_each=100, target_words=200)
	assert len(out.scenes) == 5, "no scenes removed for word count alone"
	assert any("words against a" in r.message for r in caplog.records)


# --- transitions (the seam between the cold open and paper one) --------------


def _clean(ctx, raw, order):
	return ScriptStage(ctx)._clean_transitions(raw, order)


def test_transitions_are_ordered_by_play_order_not_model_order(ctx):
	"""Stage 8 inserts each bridge before the paper it names, so the list has to
	match the order the segments actually play."""
	order = ["2608.00001", "2608.00002", "2608.00003"]
	out = _clean(ctx, [bridge("2608.00003"), bridge("2608.00001"), bridge("2608.00002")], order)
	assert [t.into_arxiv_id for t in out] == order


def test_transition_into_a_paper_not_in_the_episode_is_dropped(ctx):
	"""It would name a segment that never plays and render as a bridge to nowhere."""
	out = _clean(ctx, [bridge("2608.00001"), bridge("2608.09999")], ["2608.00001"])
	assert [t.into_arxiv_id for t in out] == ["2608.00001"]


def test_only_one_transition_per_paper_survives(ctx):
	out = _clean(
		ctx, [bridge("2608.00001", "First."), bridge("2608.00001", "Second.")], ["2608.00001"]
	)
	assert len(out) == 1 and out[0].narration == "First."


def test_a_malformed_transition_does_not_take_the_others_with_it(ctx):
	out = _clean(
		ctx, [{"into_arxiv_id": "2608.00001"}, bridge("2608.00002")], ["2608.00001", "2608.00002"]
	)
	assert [t.into_arxiv_id for t in out] == ["2608.00002"]


def test_missing_transitions_leave_a_hard_cut_rather_than_failing(ctx):
	assert _clean(ctx, None, ["2608.00001"]) == []


def test_a_bad_transition_does_not_cost_the_title_and_description(ctx, monkeypatch):
	"""Both come back from the same call, so transitions are validated on their
	own - an unusable bridge must not degrade the whole wrapper."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	payload = episode_payload(transitions=[{"into_arxiv_id": "2608.00001", "est_seconds": 0}])
	patch_llm(monkeypatch, ScriptedLLM([script_payload("2608.00001"), payload]))

	episode = ScriptStage(ctx).run().episode
	assert episode.title == "A short title"
	assert episode.transitions == []


def test_transitions_reach_the_reviewable_script(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	payload = episode_payload(
		transitions=[bridge("2608.00001", "The paper is not the only way in.")]
	)
	segment = script_payload("2608.00001", [scene(narration="The segment proper starts here.")])
	patch_llm(monkeypatch, ScriptedLLM([segment, payload]))

	result = ScriptStage(ctx).run()
	assert len(result.episode.transitions) == 1
	md = (ctx.paths.stage_dir("script") / "episode.md").read_text()
	assert "The paper is not the only way in." in md
	# Read in the order it plays: the bridge before the segment it introduces.
	assert md.index("Transition") < md.index("The segment proper starts here.")


def test_transition_manifest_carries_a_chapter_marker(ctx):
	t = _clean(ctx, [bridge("2608.00001")], ["2608.00001"])[0]
	scene_ = t.manifest(1, 3).scenes[0]
	assert scene_.visual.type == "transition"
	assert scene_.visual.title == "Another way in"
	assert scene_.visual.highlight == "02 / 03"


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


# --- an account that cannot make calls is not a call that failed --------------


class DeadAccountLLM(ScriptedLLM):
	"""The exact shape of the 2026-08-19 failure: a 400 whose body says the
	credit balance is gone, on every call."""

	def __init__(self, message="Error code: 400 - Your credit balance is too low"):
		super().__init__(["{}"])
		self.message = message

	def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
		self.calls += 1
		raise RuntimeError(self.message)


def test_a_credit_failure_is_recognised_and_an_ordinary_one_is_not():
	from pipeline.llm import is_account_failure

	assert is_account_failure(RuntimeError("Error code: 400 - Your credit balance is too low"))
	assert is_account_failure(RuntimeError("invalid api key"))
	assert not is_account_failure(RuntimeError("Connection reset by peer"))
	assert not is_account_failure(ValueError("expected an object, got list"))


def test_a_credit_failure_is_not_retried(ctx):
	"""Three attempts to be told the same thing, then a degraded result, is how
	a dead account reached Stage 7 and spent a narration bill."""
	from pipeline.llm import ProviderUnavailable

	fake = DeadAccountLLM()
	client = MeteredClient(fake, ctx.tracker, "script", max_retries=3)
	with pytest.raises(ProviderUnavailable):
		client.complete("anything")
	assert fake.calls == 1, "no retries against an account-level failure"


def test_the_wrapper_does_not_degrade_when_the_account_is_dead(ctx, monkeypatch):
	"""A bad JSON parse costs the wrapper and nothing else (D18). No credit is a
	different thing: every later call fails too, so the stage must stop before
	Stage 7 narrates an episode that has no title."""
	from pipeline.llm import ProviderUnavailable

	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])

	class SegmentsThenNoCredit(ScriptedLLM):
		def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
			self.calls += 1
			if self.calls == 1:
				return super().complete(prompt, system, max_tokens, temperature)
			raise RuntimeError("Error code: 400 - Your credit balance is too low")

	patch_llm(monkeypatch, SegmentsThenNoCredit([script_payload("2608.00001")]))
	with pytest.raises(ProviderUnavailable):
		ScriptStage(ctx).run()
	assert not (ctx.paths.stage_dir("script") / "script.json").exists()


# --- the condense pass (words, unlike scenes, are cut by a model not by code) -


def _long(n_scenes=3, words_each=40):
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2608.00001",
			"scenes": [
				{
					"id": f"s{i + 1}",
					"narration": " ".join(["word"] * words_each),
					"visual": {"type": "figure", "figure_file": "figures/fig01.png"},
					"est_seconds": 16,
				}
				for i in range(n_scenes)
			],
		}
	)


def _shorter(scene_ids, words_each=10):
	return {
		"arxiv_id": "2608.00001",
		"scenes": [
			{"id": i, "narration": " ".join(["tight"] * words_each), "est_seconds": 4}
			for i in scene_ids
		],
	}


def _condense(ctx, manifest, target_words, fake):
	return ScriptStage(ctx)._condense(
		MeteredClient(fake, ctx.tracker, "script"), manifest, target_words
	)


def test_a_part_within_budget_is_never_sent_back(ctx):
	"""The pass costs a call, so it only runs on something actually over."""
	fake = ScriptedLLM([_shorter(["s1", "s2", "s3"])])
	out = _condense(ctx, _long(words_each=10), 100, fake)
	assert fake.calls == 0
	assert out.scenes[0].narration.startswith("word")


def test_a_tightened_part_keeps_its_visuals(ctx):
	"""Only narration is rewritten - the slides were already chosen, and the
	rewrite is not asked for them."""
	fake = ScriptedLLM([_shorter(["s1", "s2", "s3"])])
	out = _condense(ctx, _long(), 60, fake)
	assert sum(len(s.narration.split()) for s in out.scenes) == 30
	assert [s.visual.figure_file for s in out.scenes] == ["figures/fig01.png"] * 3
	assert out.scenes[0].est_seconds == pytest.approx(10 / 150 * 60)


def test_a_tightened_scene_is_re_checked_for_leakage(ctx, caplog):
	"""The leakage check ran over the draft; the rewrite is different text."""
	import logging

	bad = {
		"arxiv_id": "2608.00001",
		"scenes": [
			{"id": f"s{i}", "narration": "the loss is $\\alpha$ here", "est_seconds": 2}
			for i in (1, 2, 3)
		],
	}
	with caplog.at_level(logging.WARNING):
		_condense(ctx, _long(), 60, ScriptedLLM([bad]))
	assert "tightened narration contains LaTeX" in caplog.text


def test_a_rewrite_that_drops_a_scene_is_rejected(ctx):
	"""Losing a scene would take a beat of the argument with it, so the long
	version stands and the word budget is reported instead."""
	fake = ScriptedLLM([_shorter(["s1", "s3"])])
	out = _condense(ctx, _long(), 60, fake)
	assert len(out.scenes) == 3
	assert sum(len(s.narration.split()) for s in out.scenes) == 120


def test_a_rewrite_that_is_not_shorter_is_rejected(ctx):
	fake = ScriptedLLM([_shorter(["s1", "s2", "s3"], words_each=50)])
	out = _condense(ctx, _long(), 60, fake)
	assert sum(len(s.narration.split()) for s in out.scenes) == 120


def test_an_unparseable_rewrite_leaves_the_segment_alone(ctx):
	"""A failed tightening is a long segment, not a lost one."""
	fake = ScriptedLLM(["not json"])
	out = _condense(ctx, _long(), 60, fake)
	assert len(out.scenes) == 3


def test_tightening_repeats_while_it_is_still_making_progress(ctx):
	"""One pass took the worst real segment from 279 words to 242 against a
	budget of 175 - shorter, but not short enough to stop."""
	fake = ScriptedLLM(
		[_shorter(["s1", "s2", "s3"], words_each=40), _shorter(["s1", "s2", "s3"], words_each=20)]
	)
	out = _condense(ctx, _long(words_each=50), 60, fake)
	assert fake.calls == 2
	assert sum(len(s.narration.split()) for s in out.scenes) == 60


def test_a_rejected_pass_is_not_retried(ctx):
	"""A refusal is not progress, so paying for the same one twice is waste."""
	fake = ScriptedLLM(["not json"])
	_condense(ctx, _long(words_each=50), 60, fake)
	assert fake.calls == 1


def test_tightening_stops_once_the_part_is_inside_its_budget(ctx):
	fake = ScriptedLLM([_shorter(["s1", "s2", "s3"], words_each=10)])
	out = _condense(ctx, _long(words_each=50), 60, fake)
	assert fake.calls == 1
	assert sum(len(s.narration.split()) for s in out.scenes) == 30


def test_the_cap_is_applied_before_the_rewrite_is_paid_for(ctx):
	"""Condensing scenes that are about to be dropped is money for nothing."""
	ctx.config.script.max_scenes_per_segment = 3
	fake = ScriptedLLM([_shorter(["s1", "s4", "s5"])])
	client = MeteredClient(fake, ctx.tracker, "script")
	out = ScriptStage(ctx)._enforce_budget(_long(n_scenes=5), 60, client)
	assert [s.id for s in out.scenes] == ["s1", "s4", "s5"]
	assert sum(len(s.narration.split()) for s in out.scenes) == 30


# --- projecting the finished runtime from the words -------------------------


@pytest.mark.parametrize(
	("words", "clips", "measured"),
	[
		(689, 27, 291.5),  # 2026-08-07, the fastest read of the four
		(750, 28, 315.6),  # 2026-08-13
		(570, 24, 253.2),  # 2026-08-19, the slowest
	],
)
def test_the_runtime_projection_tracks_the_shipped_episodes(ctx, words, clips, measured):
	"""Calibration, not arithmetic. The read rate moves with how long the words
	are - 2.33 to 2.45 spoken words a second across these three - so the
	projection is only ever good to about 5%. If it drifts past that, the
	constant is stale and every length decision made from it is too."""
	per_clip, extra = divmod(words, clips)
	counts = [per_clip + 1] * extra + [per_clip] * (clips - extra)
	it = iter(counts)

	def m(arxiv_id, n):
		return SceneManifest.model_validate(
			{
				"arxiv_id": arxiv_id,
				"scenes": [
					{
						"id": f"s{i}",
						"narration": " ".join(["word"] * next(it)),
						"visual": {"type": "title_card", "title": "T"},
						"est_seconds": 10,
					}
					for i in range(n)
				],
			}
		)

	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[m(f"2608.0000{i}", clips // 3) for i in (1, 2, 3)],
		episode=EpisodeMetadata(
			title="t",
			description="d",
			thumbnail_text_options=[],
			cold_open=m("episode", clips - 3 * (clips // 3)),
		),
	)
	ctx.config.tts.scene_gap_seconds = 0.35
	assert ScriptStage(ctx)._projected_seconds(result) == pytest.approx(measured, rel=0.05)


# --- the wrapper's own budget ------------------------------------------------


def _wrapper(ctx, cold_words=52, trans_words=12, outro_words=25, n=1):
	episode = EpisodeMetadata.model_validate(episode_payload([bridge("2608.00001")]))
	ScriptStage(ctx)._report_wrapper_budget(episode, cold_words, trans_words, outro_words)
	return episode


def test_a_wrapper_within_budget_is_quiet(ctx, caplog):
	import logging

	with caplog.at_level(logging.ERROR):
		_wrapper(ctx)
	assert not caplog.records


def test_an_overlong_wrapper_names_the_part_that_is_over(ctx, caplog):
	"""The 2026-08-13 cold open ran 73 words against 45 and nothing said so until
	the finished episode came out at 5:16 (D34)."""
	import logging

	with caplog.at_level(logging.ERROR):
		_wrapper(ctx, cold_words=1)
	assert "cold open is 4 words against a 1 budget" in caplog.text


def test_wrapper_budget_survives_a_dropped_bridge(ctx, caplog):
	"""A bridge can be dropped for being unparseable, so the budget is measured
	against the transitions that survived, not the number that were asked for."""
	import logging

	episode = EpisodeMetadata.model_validate(episode_payload([]))
	with caplog.at_level(logging.INFO):
		ScriptStage(ctx)._report_wrapper_budget(episode, 52, 12, 25)
	assert "transitions" not in caplog.text


# --- the live prompts, rendered with what the stage actually passes -----------


def test_the_live_script_prompt_has_every_placeholder_the_stage_fills(ctx, monkeypatch):
	"""A placeholder added to a prompt without being wired into Stage 6 raises at
	render time - which is three paid stages into a run."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	patch_llm(monkeypatch, ScriptedLLM([script_payload("2608.00001"), episode_payload()]))

	# load_prompt() is unpatched here, so this renders prompts/script/ and
	# prompts/episode/ at their highest version - the ones a real run uses.
	result = ScriptStage(ctx).run()
	assert result.segments and result.episode.title


# --- animated callouts: the numbers must be the paper's ----------------------


def _with_comparison(**over):
	params = dict(
		template="two_bar",
		label_a="Diffusion",
		value_a=200,
		label_b="Chinchilla",
		value_b=20,
		note="10x",
	)
	params.update(over)
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2608.00001",
			"scenes": [
				{
					"id": "s1",
					"narration": "Two hundred tokens per parameter, ten times the rule of twenty.",
					"visual": {"type": "result_callout", "highlight": "200", "comparison": params},
					"est_seconds": 9,
				}
			],
		}
	)


def test_a_comparison_backed_by_the_digest_survives(ctx):
	digest = "compute-optimal at 200 image tokens per parameter, against Chinchilla's 20"
	out = ScriptStage(ctx)._check_comparisons(_with_comparison(), digest)
	assert out.scenes[0].visual.comparison is not None


def test_an_invented_baseline_is_dropped_to_a_static_callout(ctx, caplog):
	"""The chart is the most credible thing on screen - a bar at a tenth the
	length of another is the claim - so a baseline the digest never stated is
	worse here than anywhere else (D36)."""
	import logging

	digest = "compute-optimal at 200 image tokens per parameter"  # no 20 anywhere
	with caplog.at_level(logging.WARNING):
		out = ScriptStage(ctx)._check_comparisons(_with_comparison(), digest)
	assert out.scenes[0].visual.comparison is None
	assert out.scenes[0].visual.type == "result_callout", (
		"the scene survives, the animation does not"
	)
	assert "does not contain" in caplog.text


def test_a_value_is_not_matched_inside_a_longer_number(ctx):
	"""A digest saying 1200 does not license a claim of 200."""
	out = ScriptStage(ctx)._check_comparisons(_with_comparison(), "trained on 1200 and 4020 things")
	assert out.scenes[0].visual.comparison is None


def test_a_rounded_value_still_matches(ctx):
	"""Same tolerance the extract check uses: a digest's 80.4 backs a stated 80."""
	out = ScriptStage(ctx)._check_comparisons(
		_with_comparison(value_a=80, value_b=20), "reached 80.4 percent against 20.1 before"
	)
	assert out.scenes[0].visual.comparison is not None


def test_the_wrapper_never_animates(ctx, monkeypatch):
	"""A cold-open hook is a dozen words, and no digest reaches that call to
	check its numbers against."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])
	wrapper = episode_payload()
	wrapper["cold_open"]["scenes"][0]["visual"] = {
		"type": "result_callout",
		"highlight": "200",
		"comparison": {
			"template": "count_up",
			"label_a": "Tokens",
			"value_a": 200,
			"unit": "per parameter",
		},
	}
	patch_llm(monkeypatch, ScriptedLLM([script_payload("2608.00001"), wrapper]))

	episode = ScriptStage(ctx).run().episode
	assert episode.cold_open is not None
	assert all(s.visual.comparison is None for s in episode.cold_open.scenes)


def test_a_malformed_comparison_costs_the_animation_not_the_segment(ctx, monkeypatch):
	"""Regression: the first live run of prompt v6 returned a `two_bar` with no
	`value_b`, and because the comparison was validated inside the SceneManifest
	the whole segment failed to parse and the episode lost a paper (D36)."""
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	seed_extract(ctx, [PaperDigest.model_validate(digest_payload("2608.00001"))])

	scenes = [scene(), scene("s2")]
	scenes[1]["visual"]["type"] = "result_callout"
	scenes[1]["visual"]["highlight"] = "200"
	scenes[1]["visual"]["comparison"] = {  # two_bar with nothing to compare against
		"template": "two_bar",
		"label_a": "This paper",
		"value_a": 200,
	}
	patch_llm(
		monkeypatch,
		ScriptedLLM([script_payload("2608.00001", scenes), episode_payload()]),
	)

	result = ScriptStage(ctx).run()
	assert len(result.segments) == 1, "the segment survives"
	seg = result.segments[0]
	assert len(seg.scenes) == 2, "and keeps every scene"
	assert seg.scenes[1].visual.type == "result_callout"
	assert seg.scenes[1].visual.comparison is None, "only the animation is lost"


def test_a_valid_comparison_still_arrives(ctx, monkeypatch):
	papers = [make_enriched("2608.00001")]
	seed(ctx, papers, ["2608.00001"])
	digest = digest_payload(
		"2608.00001", results=[{"statement": "200 against 20", "source": "Table 1"}]
	)
	seed_extract(ctx, [PaperDigest.model_validate(digest)])

	scenes = [scene(), scene("s2")]
	scenes[1]["visual"]["type"] = "result_callout"
	scenes[1]["visual"]["comparison"] = {
		"template": "two_bar",
		"label_a": "This paper",
		"value_a": 200,
		"label_b": "Prior",
		"value_b": 20,
	}
	patch_llm(
		monkeypatch,
		ScriptedLLM([script_payload("2608.00001", scenes), episode_payload()]),
	)
	seg = ScriptStage(ctx).run().segments[0]
	assert seg.scenes[1].visual.comparison is not None
	assert seg.scenes[1].visual.comparison.template == "two_bar"


@pytest.mark.parametrize(
	("field", "value"),
	[("note", 65), ("unit", None), ("label_b", 20), ("note", None)],
)
def test_a_label_that_is_not_a_string_does_not_cost_the_animation(field, value):
	"""Both of these were seen on the first live runs of prompt v6: `note: 65`
	and `unit: null`. The value reaches the slide as text either way (D37)."""
	from pipeline.schemas import Comparison

	params = dict(template="two_bar", label_a="A", value_a=200, label_b="B", value_b=20)
	params[field] = value
	c = Comparison.model_validate(params)
	assert isinstance(getattr(c, field), str)
