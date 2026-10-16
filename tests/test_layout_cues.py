"""A spoken reveal must hide its answer until the measured cue in every layout."""

from datetime import UTC, datetime

import pytest

from pipeline.editorial import pacing_report
from pipeline.render.editorial import EditorialScene
from pipeline.render.slides import SlideContext
from pipeline.render.timing import cue_preflight, reveal_cues
from pipeline.schemas import (
	EpisodeMetadata,
	Scene,
	SceneAudio,
	SceneManifest,
	ScriptResult,
	SpeechAlignment,
)
from pipeline.stages.script import _attach_direction, _detach_direction, scenes_to_markdown


def make_scene(visual):
	return Scene(
		id="s1",
		narration="First consider the setup. Now see the outcome.",
		visual=visual,
		est_seconds=6,
	)


def audio_for(scene):
	return SceneAudio(
		scene_id=scene.id,
		audio_file="test.wav",
		duration_seconds=6,
		characters=len(scene.narration),
		alignment=SpeechAlignment(
			characters=list(scene.narration),
			character_start_times_seconds=[i * 0.1 for i in range(len(scene.narration))],
			character_end_times_seconds=[(i + 1) * 0.1 for i in range(len(scene.narration))],
		),
	)


@pytest.mark.parametrize(
	"kind, target, extra, region",
	[
		("process", "item:1", {"bullets": ["Input", "Output"]}, (1000, 420, 1800, 760)),
		("contrast", "item:1", {"bullets": ["Before", "After"]}, (1000, 420, 1800, 760)),
		("bullet_slide", "item:1", {"bullets": ["Setup", "Outcome"]}, (250, 500, 1780, 610)),
		("figure", "figure", {}, (112, 315, 1808, 865)),
		("title_card", "highlight", {"highlight": "The outcome"}, (118, 774, 1650, 894)),
		("result_callout", "highlight", {"highlight": "The outcome"}, (112, 383, 1804, 760)),
		(
			"result_callout",
			"value:a",
			{"comparison": {"template": "count_up", "label_a": "Score", "value_a": 42}},
			(112, 339, 1770, 690),
		),
		(
			"result_callout",
			"value:b",
			{
				"comparison": {
					"template": "two_bar",
					"label_a": "A",
					"value_a": 42,
					"label_b": "B",
					"value_b": 21,
				}
			},
			(1430, 676, 1800, 788),
		),
		(
			"result_callout",
			"value:a",
			{"comparison": {"template": "split", "label_a": "A", "label_b": "B", "value_a": 42}},
			(112, 368, 1808, 800),
		),
	],
)
def test_layout_reveals_at_measured_phrase_and_seeks_deterministically(kind, target, extra, region):
	scene = make_scene(
		{
			"type": kind,
			"title": "Consider the test",
			**extra,
			"cue_phrases": {target: "Now see the outcome"},
		}
	)
	cues, warnings = reveal_cues(scene, audio_for(scene))
	assert not warnings
	at = scene.narration.index("Now") * 0.1
	assert cues[target] == pytest.approx(at)
	design = EditorialScene(scene, SlideContext(width=640, height=360), 6, cues)
	box = design.rect(region)
	early = design.frame(at - 0.05).crop(box).tobytes()
	assert early == design.frame(at).crop(box).tobytes()
	assert early != design.frame(at + 0.55).crop(box).tobytes()
	assert early == design.frame(at - 0.05).crop(box).tobytes()


def test_both_bar_and_number_wait_for_the_same_phrase():
	scene = make_scene(
		{
			"type": "result_callout",
			"comparison": {
				"template": "two_bar",
				"label_a": "A",
				"value_a": 42,
				"label_b": "B",
				"value_b": 21,
			},
			"cue_phrases": {"value:a": "Now see the outcome"},
		}
	)
	cues, _ = reveal_cues(scene, audio_for(scene))
	design = EditorialScene(scene, SlideContext(width=640, height=360), 6, cues)
	for region in [(116, 466, 1376, 543), (1430, 452, 1800, 564)]:
		box = design.rect(region)
		assert design.frame(2).crop(box).tobytes() != design.frame(5).crop(box).tobytes()
	# Final quantities and proportions are identical to an uncued chart.
	assert design.poster().tobytes() == EditorialScene(scene, design.ctx, 6).poster().tobytes()


def test_pre_voice_review_catches_ambiguous_and_unrendered_targets():
	scene = make_scene(
		{
			"type": "contrast",
			"bullets": ["A", "B"],
			"cue_phrases": {"item:0": "the", "item:1": "Never spoken", "item:2": "Now"},
		}
	)
	requests, warnings = cue_preflight(scene)
	assert not requests and len(warnings) == 3
	manifest = SceneManifest(arxiv_id="test", scenes=[scene])
	script = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="test", description="test"),
	)
	report = pacing_report(script)
	assert len([x for x in report["issues"] if x["code"] == "invalid_reveal_phrase"]) == 3
	assert report["scenes"][0]["reveal_cues_seconds"] == {}
	assert "Review cue:" in scenes_to_markdown(manifest)


def test_split_cannot_separately_reveal_complement_and_missing_audio_is_explicit():
	scene = make_scene(
		{
			"type": "result_callout",
			"comparison": {"template": "split", "label_a": "A", "value_a": 42, "label_b": "B"},
			"cue_phrases": {"value:a": "Now", "value:b": "outcome"},
		}
	)
	audio = audio_for(scene)
	audio.alignment = None
	cues, warnings = reveal_cues(scene, audio)
	assert not cues
	assert any("no rendered target" in w for w in warnings)
	assert any("No character alignment" in w for w in warnings)


def test_malformed_optional_cues_preserve_narration_and_other_direction():
	payload = {
		"arxiv_id": "test",
		"scenes": [make_scene({"type": "title_card", "title": "Test"}).model_dump()],
	}
	payload["scenes"][0]["visual"].update(cue_phrases={"highlight": 42}, reveal_phrase=["wrong"])
	held = _detach_direction(payload)
	manifest = SceneManifest.model_validate(payload)
	_attach_direction(manifest, held)
	assert manifest.scenes[0].narration.startswith("First consider")
	assert manifest.scenes[0].visual.cue_phrases == {}
	assert manifest.scenes[0].visual.reveal_phrase == ""


def test_old_manifest_is_valid_and_cued_title_without_separate_headline_waits():
	scene = make_scene({"type": "title_card", "highlight": "Outcome", "reveal_phrase": "Now"})
	cues, warnings = reveal_cues(scene, audio_for(scene))
	assert not warnings
	design = EditorialScene(scene, SlideContext(width=640, height=360), 6, cues)
	box = design.rect((112, 314, 1690, 725))
	assert design.frame(2).crop(box).tobytes() != design.frame(5).crop(box).tobytes()
