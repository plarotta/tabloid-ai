"""Stage 9 chapter arithmetic.

Chapters are the one packaged artifact with no visible failure mode: a wrong
offset still produces a valid YouTube description, and nobody notices until a
viewer clicks a chapter and lands in the middle of the previous paper. So the
offsets are asserted directly rather than through a rendered episode.
"""

from __future__ import annotations

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
