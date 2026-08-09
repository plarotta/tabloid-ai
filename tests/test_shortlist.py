"""Stage 2 behaviour, with the LLM mocked. No network, no spend."""

from __future__ import annotations

import json

import pytest

from pipeline.llm import MeteredClient
from pipeline.llm.base import LLMClient, LLMResponse
from pipeline.paths import write_jsonl
from pipeline.schemas import PaperScore
from pipeline.stage import StageError
from pipeline.stages.shortlist import ShortlistStage, batched, format_batch
from tests.conftest import make_paper


class FakeLLM(LLMClient):
	"""Returns a scripted score for every paper it is shown."""

	provider = "anthropic"

	def __init__(self, model="claude-haiku-4-5-20251001", scores=None, fail=False):
		super().__init__(model, api_key="x")
		self.scores = scores or {}
		self.fail = fail
		self.calls = 0

	def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
		self.calls += 1
		if self.fail:
			raise RuntimeError("provider exploded")
		ids = [
			ln.split("arxiv_id: ")[1] for ln in prompt.split("\n") if ln.startswith("arxiv_id: ")
		]
		payload = [
			{
				"arxiv_id": pid,
				"novelty": self.scores.get(pid, 5.0),
				"interest": self.scores.get(pid, 5.0),
				"visual_potential": self.scores.get(pid, 5.0),
				"rationale": "because",
			}
			for pid in ids
		]
		return LLMResponse(json.dumps(payload), 100, 50, self.model, self.provider)


def _seed_papers(ctx, n=6):
	papers = [make_paper(f"260{i}.0000{i}", title=f"Paper {i}") for i in range(n)]
	write_jsonl(ctx.paths.papers_jsonl, [p.model_dump(mode="json") for p in papers])
	return papers


def test_batched_splits_evenly():
	items = list(range(10))
	assert [len(b) for b in batched(items, 3)] == [3, 3, 3, 1]


def test_batched_rejects_zero():
	with pytest.raises(ValueError):
		batched([1, 2], 0)


def test_format_batch_truncates_long_abstracts():
	p = make_paper("2608.00001", abstract="x" * 5000)
	out = format_batch([p])
	assert "..." in out
	assert len(out) < 2000


def test_overall_weights_visual_potential_lowest():
	s = PaperScore(arxiv_id="a", novelty=10, interest=10, visual_potential=0)
	assert s.overall == pytest.approx(8.0)


def test_shortlist_ranks_and_truncates(ctx, monkeypatch):
	_seed_papers(ctx, 6)
	high = {"2600.00000": 9.0, "2601.00001": 8.0, "2602.00002": 7.0}
	fake = FakeLLM(scores=high)
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage)),
	)

	result = ShortlistStage(ctx).run()

	# shortlist_size is 3 in the test config
	assert len(result.shortlist) == 3
	assert result.total_candidates == 6
	assert [e.paper.arxiv_id for e in result.shortlist] == list(high)
	# every paper keeps a score for auditing, not just survivors
	assert len(result.all_scores) == 6


def test_shortlist_writes_artifact(ctx, monkeypatch):
	_seed_papers(ctx, 4)
	fake = FakeLLM()
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage)),
	)
	ShortlistStage(ctx).run()

	assert ctx.paths.shortlist_json.exists()
	assert json.loads(ctx.paths.shortlist_json.read_text())["total_candidates"] == 4


def test_shortlist_records_cost_per_call(ctx, monkeypatch):
	_seed_papers(ctx, 4)  # batch_size 2 -> 2 calls
	fake = FakeLLM()
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage)),
	)
	ShortlistStage(ctx).run()

	assert fake.calls == 2
	assert len(ctx.tracker.records) == 2
	assert ctx.tracker.stage_total("shortlist") > 0


def test_one_bad_batch_does_not_lose_the_run(ctx, monkeypatch):
	"""A batch that fails to parse drops only its own papers."""
	_seed_papers(ctx, 4)

	class FlakyLLM(FakeLLM):
		def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
			self.calls += 1
			if self.calls == 1:
				return LLMResponse("not json at all", 10, 10, self.model, self.provider)
			return super().complete(prompt, system, max_tokens, temperature)

	fake = FlakyLLM()
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage)),
	)
	result = ShortlistStage(ctx).run()
	assert len(result.all_scores) == 2  # only the surviving batch


def test_all_batches_failing_raises(ctx, monkeypatch):
	_seed_papers(ctx, 4)
	fake = FakeLLM(fail=True)
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(
			lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage, max_retries=1)
		),
	)
	with pytest.raises(StageError, match="Every shortlist batch failed"):
		ShortlistStage(ctx).run()


def test_hallucinated_ids_are_dropped(ctx, monkeypatch):
	_seed_papers(ctx, 2)

	class LiarLLM(FakeLLM):
		def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
			self.calls += 1
			payload = [
				{
					"arxiv_id": "9999.99999",
					"novelty": 10,
					"interest": 10,
					"visual_potential": 10,
					"rationale": "invented",
				}
			]
			return LLMResponse(json.dumps(payload), 10, 10, self.model, self.provider)

	fake = LiarLLM()
	monkeypatch.setattr(
		MeteredClient,
		"for_stage",
		classmethod(lambda cls, spec, tracker, stage, **kw: cls(fake, tracker, stage)),
	)
	with pytest.raises(StageError):
		ShortlistStage(ctx).run()
