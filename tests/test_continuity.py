"""Continuity must preserve object identity, evidence boundaries, and fallback speech."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pipeline.demo import continuity_manifest
from pipeline.editorial import pacing_report
from pipeline.render.editorial import EditorialScene
from pipeline.render.slides import SlideContext, render_visual
from pipeline.schemas import EpisodeMetadata, SceneManifest, ScriptResult
from pipeline.stages.script import _attach_direction, _detach_direction


@pytest.mark.parametrize("change", ["rename", "reorder", "duplicate", "two_focus"])
def test_diagram_identity_rejects_ambiguous_edits(change):
	data = continuity_manifest().model_dump()
	nodes = data["scenes"][1]["visual"]["diagram"]["nodes"]
	if change == "rename":
		nodes[0]["label"] = "Different object"
	elif change == "reorder":
		nodes.reverse()
	elif change == "duplicate":
		nodes[1]["id"] = nodes[0]["id"]
	else:
		nodes[0]["state"] = "focus"
	with pytest.raises(ValidationError):
		SceneManifest.model_validate(data)


def test_malformed_optional_diagram_keeps_narration_and_valid_diagrams():
	payload = continuity_manifest().model_dump()
	payload["scenes"][1]["visual"]["diagram"]["nodes"][0]["label"] = "Wrong name"
	held = _detach_direction(payload)
	manifest = SceneManifest.model_validate(payload)
	_attach_direction(manifest, held)
	assert manifest.scenes[1].visual.diagram is None
	assert manifest.scenes[1].narration == payload["scenes"][1]["narration"]
	assert manifest.scenes[0].visual.diagram
	assert manifest.scenes[2].visual.diagram
	assert manifest.scenes[1].teaches


def test_objects_stay_fixed_while_the_focus_changes():
	scenes = continuity_manifest().scenes
	ctx = SlideContext(width=960, height=540)
	a, b = [EditorialScene(s, ctx, 5.5) for s in scenes[:2]]
	# Label area: focus outlines/labels must not move the persistent words.
	labels = a.rect((140, 520, 590, 625))
	assert a.frame(0).crop(labels).tobytes() == b.frame(3).crop(labels).tobytes()
	assert a.frame(3).crop(labels).tobytes() == a.frame(0).crop(labels).tobytes()
	# Focus motion is deterministic, including non-monotonic seeks.
	first = b.frame(0.6).tobytes()
	b.frame(5.5)
	assert first == b.frame(0.6).tobytes()


def test_removed_node_keeps_label_but_breaks_connection():
	scenes = continuity_manifest().scenes
	ctx = SlideContext(width=960, height=540)
	intact = EditorialScene(scenes[2], ctx, 5.5)
	removed = EditorialScene(scenes[3], ctx, 5.5)
	label = intact.rect((710, 530, 1208, 625))
	assert intact.poster().crop(label).tobytes() == removed.poster().crop(label).tobytes()
	connection = intact.rect((630, 570, 710, 610))
	assert intact.poster().crop(connection).tobytes() != removed.poster().crop(connection).tobytes()


def test_classic_fallback_does_not_depend_on_optional_bullets():
	scene = continuity_manifest().scenes[3]
	scene.visual.bullets = []
	assert render_visual(SlideContext(width=640, height=360), scene.visual).size == (640, 360)


def test_review_flags_repeated_goals_but_allows_a_developing_diagram():
	manifest = continuity_manifest()
	manifest.scenes[1].teaches = manifest.scenes[0].teaches
	script = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="Example", description="Illustrative"),
	)
	issues = pacing_report(script)["issues"]
	assert any(i["code"] == "repeated_learning_goal" for i in issues)
	assert not any(i["code"] == "repeated_layout" for i in issues)


def test_ablation_without_source_is_rejected():
	data = continuity_manifest().model_dump()
	data["scenes"][3]["visual"]["source"] = ""
	with pytest.raises(ValidationError, match="source"):
		SceneManifest.model_validate(data)


def test_node_text_has_readable_contrast():
	from pipeline.render.slides import FG

	def luminance(rgb):
		channels = [v / 255 for v in rgb]
		linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
		return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

	design = EditorialScene(continuity_manifest().scenes[0], SlideContext(), 5.5)
	background = design.base.getpixel((design.x(150), design.y(740)))
	light, dark = sorted([luminance(FG), luminance(background)], reverse=True)
	assert (light + 0.05) / (dark + 0.05) >= 4.5


def test_removed_state_survives_cut_until_restoration_cue():
	scene = continuity_manifest().scenes[3].model_copy(deep=True)
	ctx = SlideContext(width=640, height=360)
	previous = {n.id: n.state for n in scene.visual.diagram.nodes}
	removed = EditorialScene(scene, ctx, 5)
	for node in scene.visual.diagram.nodes:
		node.state = "normal"
	cues = {f"node:{n.id}": 3 for n in scene.visual.diagram.nodes}
	restored = EditorialScene(scene, ctx, 5, cues=cues, previous_states=previous)
	# Connector and marker stay removed across the cut, including backward seeks.
	for coords in [(630, 568, 710, 611), (710, 440, 1200, 505)]:
		box = restored.rect(coords)
		before = restored.frame(0).crop(box).tobytes()
		assert before == removed.poster().crop(box).tobytes()
		assert before == restored.frame(2.9).crop(box).tobytes()
		assert before != restored.frame(4).crop(box).tobytes()
		assert before == restored.frame(0).crop(box).tobytes()
		assert (
			restored.poster().crop(box).tobytes()
			== EditorialScene(scene, ctx, 5).poster().crop(box).tobytes()
		)


def test_unchanged_focus_does_not_blink_and_handoff_waits_for_destination():
	scenes = continuity_manifest().scenes
	ctx = SlideContext(width=640, height=360)
	previous = {n.id: n.state for n in scenes[0].visual.diagram.nodes}
	old_focus = next(key for key, value in previous.items() if value == "focus")
	unchanged = EditorialScene(scenes[0], ctx, 5, previous_states=previous)
	box = unchanged.rect((140, 440, 590, 505))
	assert unchanged.frame(0).crop(box).tobytes() == unchanged.poster().crop(box).tobytes()
	new_focus = next(n.id for n in scenes[1].visual.diagram.nodes if n.state == "focus")
	assert new_focus != old_focus
	handoff = EditorialScene(
		scenes[1], ctx, 5, cues={f"node:{new_focus}": 3}, previous_states=previous
	)
	assert handoff.frame(0).crop(box).tobytes() == handoff.frame(2.9).crop(box).tobytes()
	assert handoff.frame(0).crop(box).tobytes() != handoff.frame(4).crop(box).tobytes()


def test_fading_out_state_changes_encoder_frame_cache_key():
	from PIL import Image

	from pipeline.render.editorial import Layer

	layer = Layer(Image.new("RGBA", (2, 2), "white"), (0, 0), -0.4, 0.4, "fade", 3)
	assert layer.progress(0) == layer.progress(2.9) == 1
	assert 0 < layer.progress(3.2) < 1
	assert layer.progress(3.5) == 0
