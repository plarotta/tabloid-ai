"""Config validation, artifact round-tripping, and stage caching."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pipeline.config import Config, load_config
from pipeline.llm import MissingAPIKey
from pipeline.llm.providers import build_client
from pipeline.paths import RunPaths, read_jsonl, write_json, write_jsonl
from pipeline.schemas import Paper
from pipeline.stages.fetch import FetchStage
from pipeline.tts import build_tts_client
from tests.conftest import make_paper


def test_repo_config_is_valid():
	"""The checked-in config.yaml must parse - it is the default every run uses."""
	cfg = load_config()
	assert "shortlist" in cfg.models
	assert cfg.budget.ceiling_usd > 0


def test_unknown_config_key_is_rejected():
	with pytest.raises(ValidationError):
		Config.model_validate(
			{
				"fetch": {"categories": ["cs.LG"], "typo_here": 1},
				"shortlist": {},
				"rank": {},
				"budget": {},
				"models": {},
			}
		)


def test_model_for_unknown_stage_names_the_options(config: Config):
	with pytest.raises(KeyError, match="No model configured"):
		config.model_for("nonexistent")


def test_paper_roundtrips_through_jsonl(tmp_path):
	papers = [make_paper("2608.00001"), make_paper("2608.00002")]
	path = tmp_path / "papers.jsonl"
	write_jsonl(path, [p.model_dump(mode="json") for p in papers])
	restored = [Paper.model_validate(r) for r in read_jsonl(path)]
	assert [p.arxiv_id for p in restored] == ["2608.00001", "2608.00002"]
	assert restored[0].submitted == papers[0].submitted


def test_write_json_is_atomic(tmp_path):
	path = tmp_path / "out.json"
	write_json(path, {"a": 1})
	assert json.loads(path.read_text()) == {"a": 1}
	# no stray temp file left behind
	assert list(tmp_path.glob("*.tmp")) == []


def test_extra_field_in_artifact_is_rejected():
	"""Schema drift between stages should fail loudly, not be silently ignored."""
	with pytest.raises(ValidationError):
		Paper.model_validate(
			{
				"arxiv_id": "1",
				"title": "t",
				"abstract": "a",
				"authors": [],
				"categories": [],
				"primary_category": "cs.LG",
				"submitted": "2026-08-06T00:00:00Z",
				"updated": "2026-08-06T00:00:00Z",
				"unexpected_field": True,
			}
		)


def test_stage_is_complete_reflects_artifact(ctx):
	stage = FetchStage(ctx)
	assert stage.is_complete() is False
	write_jsonl(ctx.paths.papers_jsonl, [make_paper("2608.00001").model_dump(mode="json")])
	assert stage.is_complete() is True
	assert stage.load()[0].arxiv_id == "2608.00001"


def test_ensure_uses_cache_instead_of_refetching(ctx):
	"""This is what makes `--from` cheap; if it regressed, re-runs would hit the
	network and the LLM again."""
	write_jsonl(ctx.paths.papers_jsonl, [make_paper("2608.00001").model_dump(mode="json")])
	stage = FetchStage(ctx)
	stage.run = lambda: pytest.fail("run() must not be called when artifacts exist")
	assert len(stage.ensure()) == 1


def test_fetch_window_falls_back_when_no_state(ctx, monkeypatch):
	monkeypatch.setattr("pipeline.stages.fetch.last_successful_run", lambda: None)
	start, end = FetchStage(ctx).window()
	assert (end - start).days == ctx.config.fetch.fallback_window_days


def test_run_paths_are_stage_scoped(tmp_path):
	paths = RunPaths("2026-08-07", runs_dir=tmp_path)
	assert paths.papers_jsonl.parent.name == "fetch"
	assert paths.shortlist_json.parent.name == "shortlist"
	# cost spans stages, so it sits at the run root
	assert paths.cost_report_json.parent.name == "2026-08-07"


def test_empty_api_key_fails_fast(monkeypatch):
	"""An empty-but-set key is the exact footgun found in this environment."""
	monkeypatch.setenv("ANTHROPIC_API_KEY", "")
	with pytest.raises(MissingAPIKey, match="unset or empty"):
		build_client("anthropic", "claude-haiku-4-5-20251001")


def test_tts_providers_are_wired():
	"""Phase 4 wired the engines; the Phase 1 NotImplementedError is gone."""
	from pipeline.tts import PROVIDERS, TTSError

	assert {"macos", "openai", "elevenlabs"} <= set(PROVIDERS)
	assert build_tts_client("macos").provider == "macos"

	with pytest.raises(TTSError, match="Unknown TTS provider"):
		build_tts_client("nope-tts")
	with pytest.raises(TTSError, match=r"tts\.provider is not set"):
		build_tts_client(None)
