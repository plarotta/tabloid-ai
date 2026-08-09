"""Cost accounting is a hard requirement, so its arithmetic is tested directly."""

from __future__ import annotations

import json

import pytest

from pipeline.llm.cost import BudgetExceeded, CostTracker, Pricing


def test_price_uses_per_million_units(pricing: Pricing):
	# 1M in @ $1.00 + 1M out @ $5.00
	cost, unpriced = pricing.price(
		"text", "anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000
	)
	assert cost == pytest.approx(6.00)
	assert unpriced is False


def test_price_scales_sublinearly_for_small_calls(pricing: Pricing):
	cost, _ = pricing.price("text", "anthropic", "claude-haiku-4-5-20251001", 1500, 200)
	assert cost == pytest.approx(1500 / 1e6 * 1.0 + 200 / 1e6 * 5.0)


def test_unknown_model_is_flagged_not_guessed(pricing: Pricing):
	cost, unpriced = pricing.price("text", "anthropic", "not-a-real-model", 1000, 1000)
	assert cost == 0.0
	assert unpriced is True


def test_unknown_provider_is_flagged(pricing: Pricing):
	_, unpriced = pricing.price("text", "nobody", "nothing", 10, 10)
	assert unpriced is True


def test_records_append_to_disk_immediately(tracker: CostTracker):
	tracker.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 1000, 100)
	tracker.record("rank", "anthropic", "claude-sonnet-4-5-20250929", 2000, 500)

	lines = tracker.calls_path.read_text().strip().split("\n")
	assert len(lines) == 2
	assert json.loads(lines[0])["stage"] == "shortlist"


def test_report_subtotals_by_stage(tracker: CostTracker):
	tracker.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0)
	tracker.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0)
	tracker.record("rank", "anthropic", "claude-sonnet-4-5-20250929", 1_000_000, 0)

	report = tracker.build_report()
	stages = {s.stage: s for s in report.by_stage}
	assert stages["shortlist"].calls == 2
	assert stages["shortlist"].cost_usd == pytest.approx(2.00)
	assert stages["rank"].cost_usd == pytest.approx(3.00)
	assert report.total_usd == pytest.approx(5.00)


def test_stage_target_breach_is_flagged_but_not_fatal(tracker: CostTracker):
	# shortlist target is $1.00; 2M input tokens at $1/M = $2.00
	tracker.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 2_000_000, 0)
	stages = {s.stage: s for s in tracker.build_report().by_stage}
	assert stages["shortlist"].over_target is True


def test_ceiling_raises(tracker: CostTracker):
	with pytest.raises(BudgetExceeded, match="ceiling"):
		tracker.record("extract", "anthropic", "claude-sonnet-4-5-20250929", 10_000_000, 0)


def test_unpriced_calls_surface_in_report(tracker: CostTracker):
	tracker.record("script", "anthropic", "mystery-model", 1000, 1000)
	assert tracker.build_report().unpriced_calls == 1


def test_report_is_written_as_valid_json(tracker: CostTracker, tmp_path):
	tracker.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 100, 10)
	path = tmp_path / "cost_report.json"
	tracker.write_report(path)
	assert json.loads(path.read_text())["run_id"] == "test"


def test_tracker_resumes_from_the_call_log(tmp_path, pricing):
	"""Regression: a `--from <stage>` resume rebuilt cost_report.json from only
	the stages that ran that time, discarding everything spent earlier."""
	calls = tmp_path / "calls.jsonl"

	first = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=100.0)
	first.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0)
	assert first.total_usd == pytest.approx(1.00)

	# A separate process later resumes the same run.
	second = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=100.0)
	assert second.total_usd == pytest.approx(1.00), "prior spend must be carried"

	second.record("rank", "anthropic", "claude-sonnet-4-5-20250929", 1_000_000, 0)
	report = second.build_report()
	assert report.total_usd == pytest.approx(4.00)
	assert {s.stage for s in report.by_stage} == {"shortlist", "rank"}


def test_ceiling_counts_spend_from_earlier_invocations(tmp_path, pricing):
	"""The ceiling is a per-run budget, so a resume must not get a fresh one."""
	calls = tmp_path / "calls.jsonl"
	first = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=5.0)
	first.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 4_000_000, 0)

	resumed = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=5.0)
	with pytest.raises(BudgetExceeded):
		resumed.record("rank", "anthropic", "claude-haiku-4-5-20251001", 2_000_000, 0)


def test_tracker_tolerates_a_truncated_log_line(tmp_path, pricing):
	calls = tmp_path / "calls.jsonl"
	t = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=100.0)
	t.record("shortlist", "anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0)
	with calls.open("a", encoding="utf-8") as fh:
		fh.write('{"timestamp": "2026-01-01T00:00:00Z", "stage": "ra')  # killed mid-write

	resumed = CostTracker(run_id="r", calls_path=calls, pricing=pricing, ceiling_usd=100.0)
	assert resumed.total_usd == pytest.approx(1.00)
