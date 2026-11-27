"""Stage 9 chapter arithmetic.

Chapters are the one packaged artifact with no visible failure mode: a wrong
offset still produces a valid YouTube description, and nobody notices until a
viewer clicks a chapter and lands in the middle of the previous paper. So the
offsets are asserted directly rather than through a rendered episode.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pipeline.schemas import SceneAudio, SegmentAudio, VoiceResult
from pipeline.stages.package import chapter_marks, timestamp

TITLES = {"2608.00001": "Paper One", "2608.00002": "Paper Two"}


def part(arxiv_id: str, seconds: float) -> SegmentAudio:
	return SegmentAudio(
		arxiv_id=arxiv_id,
		scenes=[
			SceneAudio(
				scene_id="x",
				audio_file=f"{arxiv_id}/x.aiff",
				duration_seconds=seconds,
				characters=10,
			)
		],
	)


def voice_result(transitions=None) -> VoiceResult:
	return VoiceResult(
		generated_at=datetime.now(UTC),
		provider="fake",
		model="f",
		cold_open=part("episode", 12.0),
		segments=[part("2608.00001", 75.0), part("2608.00002", 80.0)],
		transitions=transitions or [],
		outro=part("episode", 10.0),
	)


def test_chapters_without_bridges_are_unchanged():
	marks = chapter_marks(voice_result(), TITLES)
	assert [m["seconds"] for m in marks] == [0.0, 12.0, 87.0, 167.0]
	assert [m["label"] for m in marks] == ["Intro", "Paper One", "Paper Two", "Outro"]


def test_bridges_push_every_later_chapter_back():
	"""The bridge into a paper plays before it, so it belongs to that paper's
	chapter - and every chapter after it moves by the bridges in between."""
	marks = chapter_marks(voice_result([part("2608.00001", 5.0), part("2608.00002", 6.0)]), TITLES)
	# Intro is still the cold open alone; Paper One's chapter opens on its bridge.
	assert [m["seconds"] for m in marks] == [0.0, 12.0, 92.0, 178.0]


def test_a_paper_without_a_bridge_does_not_shift():
	"""Transitions can be dropped one at a time upstream, so the arithmetic has to
	be per-paper rather than a flat count."""
	marks = chapter_marks(voice_result([part("2608.00002", 6.0)]), TITLES)
	assert [m["seconds"] for m in marks] == [0.0, 12.0, 87.0, 173.0]


def test_chapter_total_matches_the_narrated_duration():
	"""The packaged `duration_seconds` comes from the same numbers; if bridges
	were missing from one and not the other, the last chapter would fall outside
	the video."""
	voice = voice_result([part("2608.00001", 5.0), part("2608.00002", 6.0)])
	assert voice.duration_seconds == 188.0
	assert chapter_marks(voice, TITLES)[-1]["seconds"] < voice.duration_seconds


def test_chapters_start_at_zero_for_youtube():
	assert chapter_marks(voice_result(), TITLES)[0]["time"] == "0:00"


def test_timestamp_switches_format_past_an_hour():
	assert timestamp(87) == "1:27"
	assert timestamp(3723) == "1:02:03"


# --- the series prefix and episode number ------------------------------------


def _numbered(ctx, written: str, number: int, state_path):
	from pipeline.stages.package import PackageStage

	return PackageStage(ctx)._numbered_title(written, number)


def test_the_title_carries_the_series_and_the_number(ctx, tmp_path):
	assert (
		_numbered(ctx, "Robots hijacked with a sheet of paper", 4, tmp_path)
		== "ML Papers of the Day Ep. 4: Robots hijacked with a sheet of paper"
	)


def test_an_over_long_title_loses_its_own_words_not_the_series(ctx, tmp_path):
	"""A viewer scanning a sidebar reads the prefix first, so a truncated episode
	number is worse than a truncated sentence."""
	from pipeline.stages.script import SERIES_NAME, YOUTUBE_TITLE_MAX

	out = _numbered(ctx, "a" * 200, 4, tmp_path)
	assert len(out) <= YOUTUBE_TITLE_MAX
	assert out.startswith(f"{SERIES_NAME} Ep. 4: ")
	assert out.endswith("…")


def test_no_title_stays_no_title(ctx, tmp_path):
	"""Stage 10 refuses to publish an untitled bundle; a bare prefix would slip
	past that check while saying nothing."""
	assert _numbered(ctx, "", 4, tmp_path) == ""


def test_the_episode_number_advances_once_per_packaged_episode(tmp_path):
	from pipeline.state import advance_episode_number, episode_number

	sp = tmp_path / "state.json"
	assert episode_number(sp) == 1, "an unseeded ledger starts at one"
	sp.write_text(json.dumps({"episode_number": 4}))
	assert advance_episode_number(sp) == 4, "the run uses 4"
	assert episode_number(sp) == 5, "and leaves 5 for the next"


def test_a_repackaged_episode_keeps_the_number_it_shipped_with(ctx, tmp_path, monkeypatch):
	"""Re-rendering a finished episode must not relabel it with whatever number
	the series has since reached (D38)."""
	from pipeline import state
	from pipeline.stages.package import PackageStage

	sp = tmp_path / "state.json"
	sp.write_text(json.dumps({"episode_number": 9}))
	monkeypatch.setattr(state, "STATE_PATH", sp)

	out = ctx.paths.output_dir
	out.mkdir(parents=True, exist_ok=True)
	stage = PackageStage(ctx)
	assert stage._episode_number(out) == 9, "an unpackaged run takes the live counter"

	(out / "metadata.json").write_text(json.dumps({"episode_number": 3}))
	assert stage._episode_number(out) == 3, "a packaged one keeps what it stamped"


def test_a_corrupt_manifest_falls_back_to_the_counter(ctx, tmp_path, monkeypatch):
	from pipeline import state
	from pipeline.stages.package import PackageStage

	sp = tmp_path / "state.json"
	sp.write_text(json.dumps({"episode_number": 7}))
	monkeypatch.setattr(state, "STATE_PATH", sp)
	out = ctx.paths.output_dir
	out.mkdir(parents=True, exist_ok=True)
	(out / "metadata.json").write_text("{not json")
	assert PackageStage(ctx)._episode_number(out) == 7


def test_a_trimmed_title_stops_at_a_word(ctx, tmp_path):
	"""Ep. 4 came back three characters over and was cut to "...on their…", which
	reads as a bug rather than as an abbreviation (D38)."""
	out = _numbered(ctx, "AI agents discover math theorems on their own", 4, tmp_path)
	assert out == "ML Papers of the Day Ep. 4: AI agents discover math theorems…"
	assert len(out) <= 70


def test_a_single_over_long_word_is_still_cut(ctx, tmp_path):
	"""No word boundary to fall back to, so a hard cut is all that is left."""
	out = _numbered(ctx, "a" * 90, 4, tmp_path)
	assert len(out) <= 70
	assert out.endswith("…")
