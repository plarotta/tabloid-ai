"""Concepts explain structure without inventing observations or losing speech."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.render.editorial import EditorialScene
from pipeline.render.slides import SlideContext, render_visual
from pipeline.render.timing import cue_preflight
from pipeline.schemas import SceneManifest
from pipeline.stages.script import ScriptStage, _attach_direction, _detach_direction


def fixture():
	return SceneManifest.model_validate_json(Path("demos/transformer_review.json").read_text())


def test_every_core_scene_has_a_concept_and_no_prose_slide():
	manifest = fixture()
	assert all(s.visual.concept and not s.visual.bullets for s in manifest.scenes)
	assert all(not cue_preflight(s)[1] for s in manifest.scenes)
	assert {s.visual.concept.kind for s in manifest.scenes} == {
		"parallel_paths",
		"parameter_sweep",
		"evaluation_scope",
	}


def test_flow_constructs_split_then_combine_and_can_seek_backwards():
	scene = fixture().scenes[1]
	d = EditorialScene(
		scene, SlideContext(width=640, height=360), 6, {"input": 0, "branches": 1, "combine": 4}
	)
	region = d.rect((1000, 260, 1770, 800))
	assert d.frame(3).crop(region).tobytes() != d.frame(5).crop(region).tobytes()
	early = d.frame(1.3).tobytes()
	d.frame(6)
	assert early == d.frame(1.3).tobytes()


def test_head_count_changes_only_at_its_cue_and_old_score_waits():
	scene = fixture().scenes[3]
	d = EditorialScene(scene, SlideContext(width=640, height=360), 6, {"branches": 2, "point:1": 4})
	fan = d.rect((500, 260, 1100, 790))
	assert d.frame(0).crop(fan).tobytes() == d.frame(1.9).crop(fan).tobytes()
	assert d.frame(0).crop(fan).tobytes() != d.frame(3).crop(fan).tobytes()
	value = d.rect((1480, 255, 1810, 370))
	assert d.frame(1).crop(value).tobytes() == d.frame(3.9).crop(value).tobytes()
	assert d.frame(1).crop(value).tobytes() != d.frame(5).crop(value).tobytes()


def test_hidden_measurements_never_appear_and_axes_remain_fixed():
	scene = fixture().scenes[0]
	d = EditorialScene(scene, SlideContext(width=640, height=360), 5)
	changed = scene.model_copy(deep=True)
	changed.visual.concept.points[3].state = "context"
	e = EditorialScene(changed, d.ctx, 5)
	assert d.poster().tobytes() != e.poster().tobytes()
	axis = d.rect((120, 270, 290, 810))
	assert d.poster().crop(axis).tobytes() == e.poster().crop(axis).tobytes()


@pytest.mark.parametrize(
	"field,value", [("branches", 33), ("branches", 0), ("kind", "generated_code")]
)
def test_bad_optional_concept_preserves_narration(field, value):
	payload = fixture().model_dump()
	payload["scenes"][1]["visual"]["concept"][field] = value
	held = _detach_direction(payload)
	manifest = SceneManifest.model_validate(payload)
	_attach_direction(manifest, held)
	assert manifest.scenes[1].visual.concept is None
	assert manifest.scenes[1].narration == payload["scenes"][1]["narration"]


def test_concepts_require_source_and_verified_quantities(ctx):
	payload = fixture().model_dump()
	payload["scenes"][0]["visual"]["source"] = ""
	with pytest.raises(ValidationError, match="source"):
		SceneManifest.model_validate(payload)
	manifest = fixture()
	manifest.scenes[0].visual.concept.points[0].value = 99.7
	ScriptStage(ctx)._check_comparisons(manifest, "1 8 16 32 24.9 25.8 25.4")
	assert manifest.scenes[0].visual.concept is None
	assert manifest.scenes[1].visual.concept is not None


def test_classic_fallback_is_completed_diagram():
	scene = fixture().scenes[1]
	ctx = SlideContext(width=640, height=360)
	assert (
		render_visual(ctx, scene.visual).tobytes()
		== EditorialScene(scene, ctx, 5).poster().tobytes()
	)
