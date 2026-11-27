"""The script's numbers against the digest's (D49): what is read, what is backed."""

from __future__ import annotations

import pytest

from pipeline.numbers import Digest, digit_numbers, spoken_numbers, unbacked
from pipeline.schemas import SceneManifest


@pytest.mark.parametrize(
	("text", "expected"),
	[
		("sixty-nine point five percent", [69.5]),
		("eleven thousand six hundred twenty-eight records", [11628]),
		("twenty-five hundred runs", [2500]),
		("four point four four stars", [4.44]),
		("a cosine of point one two", [0.12]),
		("one hundred and five tasks", [105]),
		("four billion parameters", [4e9]),
		("from forty-two to twenty-four", [42, 24]),
		("and then the first one", [1]),
		("no numbers in this line at all", []),
	],
)
def test_spoken_numbers(text, expected):
	assert spoken_numbers(text) == expected


def test_digit_numbers_read_separators_and_magnitudes():
	assert digit_numbers("2,518 runs at 4.44 stars") == [2518, 4.44]
	assert digit_numbers("a 4B verifier") == [4, 4e9]
	assert digit_numbers("v2.0.1") == []  # a version is a name, not a claim


DIGEST = Digest(
	"Agents solved 57% of tasks against 85% for humans. Collected 2,518 runs with a "
	"4B verifier. Incomplete coverage in 67.9% of runs. Ratings averaged 4.4."
)


@pytest.mark.parametrize(
	"claim",
	[
		"fifty-seven percent",  # stated
		"twenty-five hundred runs",  # 2,518, rounded as spoken
		"sixty-eight percent",  # 67.9, rounded
		"four billion parameters",  # "4B"
		"a twenty-eight point gap",  # 85 - 57
	],
)
def test_a_backed_number_passes(claim):
	assert unbacked(claim, DIGEST, spoken=True) == []


@pytest.mark.parametrize(
	("claim", "flag"),
	[
		("four point four four stars", "4.44"),  # the D46 re-run's real case
		("ninety percent", "90"),  # 85 does not round to 90
		("eighty-eight point five percent", "88.5"),
	],
)
def test_an_unbacked_number_is_flagged(claim, flag):
	assert unbacked(claim, DIGEST, spoken=True) == [flag]


def test_small_whole_numbers_are_not_checked():
	""" "three models", "GPT-four" - everywhere, and never what gets invented."""
	assert unbacked("three models and GPT-four", DIGEST, spoken=True) == []
	assert unbacked("Top 3 of 5", DIGEST) == []


def _manifest(narration, visual):
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2609.00001",
			"scenes": [{"id": "s1", "narration": narration, "visual": visual, "est_seconds": 5}],
		}
	)


def test_the_stage_flags_speech_and_screen_and_keeps_the_scene(ctx):
	from pipeline.stages.script import ScriptStage, scenes_to_markdown

	m = _manifest(
		"Ratings average four point four four stars.",
		{"type": "result_callout", "highlight": "4.44 stars"},
	)
	out = ScriptStage(ctx)._check_numbers(m, DIGEST.text)
	assert out.scenes[0].unverified_numbers == ["4.44"]
	assert out.scenes[0].visual.highlight == "4.44 stars", "flagged, never rewritten"
	assert "not in the digest" in scenes_to_markdown(out)


def test_an_unbacked_chart_note_is_dropped_and_the_chart_kept(ctx):
	from pipeline.stages.script import ScriptStage

	m = _manifest(
		"Humans solved eighty-five percent.",
		{
			"type": "result_callout",
			"highlight": "85%",
			"comparison": {
				"template": "gap",
				"label_a": "Agents",
				"value_a": 57,
				"label_b": "Humans",
				"value_b": 85,
				"scale": "percent",
				"note": "a 31-point gap",
			},
		},
	)
	out = ScriptStage(ctx)._check_numbers(m, DIGEST.text)
	assert out.scenes[0].visual.comparison is not None
	assert out.scenes[0].visual.comparison.note == ""


def test_a_correct_derived_note_survives(ctx):
	from pipeline.stages.script import ScriptStage

	m = _manifest(
		"Humans solved eighty-five percent.",
		{
			"type": "result_callout",
			"highlight": "85%",
			"comparison": {
				"template": "gap",
				"label_a": "Agents",
				"value_a": 57,
				"label_b": "Humans",
				"value_b": 85,
				"scale": "percent",
				"note": "a 28-point gap",
			},
		},
	)
	out = ScriptStage(ctx)._check_numbers(m, DIGEST.text)
	assert out.scenes[0].visual.comparison.note == "a 28-point gap"
	assert out.scenes[0].unverified_numbers == []
