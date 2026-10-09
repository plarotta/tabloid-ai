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
