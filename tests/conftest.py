from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.config import Config
from pipeline.llm.cost import CostTracker, Pricing
from pipeline.paths import RunPaths
from pipeline.schemas import Paper
from pipeline.stage import StageContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing() -> Pricing:
	return Pricing(
		{
			"text": {
				"anthropic": {
					"claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
					"claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
				}
			},
			"audio": {"openai": {"tts-1": {"input": 15.00}}},
		}
	)


@pytest.fixture
def tracker(tmp_path: Path, pricing: Pricing) -> CostTracker:
	return CostTracker(
		run_id="test",
		calls_path=tmp_path / "calls.jsonl",
		pricing=pricing,
		ceiling_usd=20.0,
		stage_targets={"shortlist": 1.0},
	)


@pytest.fixture
def config() -> Config:
	return Config.model_validate(
		{
			"fetch": {
				"categories": ["cs.LG", "cs.CL"],
				"page_size": 10,
				"request_delay_seconds": 0.0,
			},
			"shortlist": {
				"batch_size": 2,
				"shortlist_size": 3,
				"max_concurrency": 1,
				"min_score": 0.0,
			},
			"enrich": {"request_delay_seconds": 0.0, "max_concurrency": 1},
			"rank": {},
			"extract": {"min_digests": 1},
			"script": {"max_concurrency": 1},
			"tts": {"provider": "fake", "model": "fake-1"},
			"render": {"width": 640, "height": 360, "fps": 24},
			"budget": {
				"ceiling_usd": 20.0,
				"stage_targets_usd": {"shortlist": 1.0, "rank": 2.0},
			},
			"models": {
				"shortlist": {
					"provider": "anthropic",
					"model": "claude-haiku-4-5-20251001",
					"max_tokens": 2048,
				},
				"rank": {
					"provider": "anthropic",
					"model": "claude-sonnet-4-5-20250929",
					"max_tokens": 4096,
				},
				"extract": {
					"provider": "anthropic",
					"model": "claude-sonnet-4-5-20250929",
					"max_tokens": 8192,
				},
				"script": {
					"provider": "anthropic",
					"model": "claude-sonnet-4-5-20250929",
					"max_tokens": 8192,
				},
			},
		}
	)


def make_paper(pid: str, title: str = "A Title", abstract: str = "An abstract.") -> Paper:
	return Paper(
		arxiv_id=pid,
		title=title,
		abstract=abstract,
		authors=["Ada Lovelace"],
		categories=["cs.LG"],
		primary_category="cs.LG",
		submitted=datetime(2026, 8, 6, tzinfo=UTC),
		updated=datetime(2026, 8, 6, tzinfo=UTC),
	)


@pytest.fixture
def ctx(config: Config, tracker: CostTracker, tmp_path: Path) -> StageContext:
	return StageContext(config=config, paths=RunPaths("test", runs_dir=tmp_path), tracker=tracker)
