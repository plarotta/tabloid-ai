import base64

import httpx
import pytest
from pydantic import ValidationError

from pipeline.demo import continuity_manifest
from pipeline.render.editorial import EditorialScene
from pipeline.render.slides import SlideContext
from pipeline.render.timing import reveal_cues
from pipeline.schemas import SceneAudio, SpeechAlignment
from pipeline.tts.providers import ElevenLabsTTS


def alignment(text):
	return SpeechAlignment(
		characters=list(text),
		character_start_times_seconds=[i * 0.05 for i in range(len(text))],
		character_end_times_seconds=[(i + 1) * 0.05 for i in range(len(text))],
	)


def test_phrase_cue_is_measured_not_fraction_of_scene():
	scene = continuity_manifest().scenes[3]
	scene.narration = "Remove memory. The result is unavailable."
	scene.visual.diagram.nodes[-1].reveal_phrase = "result is unavailable"
	audio = SceneAudio(
		scene_id=scene.id,
		audio_file="s.mp3",
		duration_seconds=5,
		characters=len(scene.narration),
		alignment=alignment(scene.narration),
	)
	cues, warnings = reveal_cues(scene, audio)
	assert not warnings
	assert cues["node:recall"] == pytest.approx(scene.narration.index("result") * 0.05)
	design = EditorialScene(scene, SlideContext(width=640, height=360), 5, cues)
	# The output panel's text changes at the resolved cue, not at scene start.
	box = design.rect((1336, 650, 1778, 738))
	assert (
		design.frame(0).crop(box).tobytes() == design.frame(cues["node:recall"]).crop(box).tobytes()
	)
	assert design.poster().crop(box).tobytes() != design.frame(0).crop(box).tobytes()


@pytest.mark.parametrize("mode", ["missing", "repeated", "stale", "too_long"])
def test_bad_alignment_or_phrase_falls_back_explicitly(mode):
	scene = continuity_manifest().scenes[0]
	scene.narration = "word then word"
	scene.visual.diagram.nodes[-1].reveal_phrase = "word"
	a = alignment("other words" if mode == "stale" else scene.narration)
	audio = SceneAudio(
		scene_id=scene.id,
		audio_file="s.mp3",
		duration_seconds=0.1 if mode == "too_long" else 5,
		characters=14,
		alignment=None if mode == "missing" else a,
	)
	cues, warnings = reveal_cues(scene, audio)
	assert not cues and warnings


@pytest.mark.parametrize("times", [[0.1, float("nan")], [0.2, 0.1], [-1, 0.1]])
def test_invalid_character_timing_rejected(times):
	with pytest.raises(ValidationError):
		SpeechAlignment(
			characters=["a", "b"],
			character_start_times_seconds=times,
			character_end_times_seconds=[0.3, 0.4],
		)


def test_elevenlabs_keeps_audio_and_provider_alignment(tmp_path, monkeypatch, respx_mock):
	monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
	monkeypatch.setattr("pipeline.tts.providers.measure_duration", lambda path: 1.0)
	respx_mock.post("https://api.elevenlabs.io/v1/text-to-speech/test/with-timestamps").mock(
		return_value=httpx.Response(
			200,
			json={
				"audio_base64": base64.b64encode(b"audio").decode(),
				"alignment": alignment("hello").model_dump(),
			},
		)
	)
	result = ElevenLabsTTS(voice="test").synthesize("hello", tmp_path / "speech")
	assert result.audio_path.read_bytes() == b"audio"
	assert result.alignment.characters == list("hello")


def test_invalid_alignment_does_not_discard_paid_audio(tmp_path, monkeypatch, respx_mock):
	monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
	monkeypatch.setattr("pipeline.tts.providers.measure_duration", lambda path: 1.0)
	respx_mock.post("https://api.elevenlabs.io/v1/text-to-speech/test/with-timestamps").mock(
		return_value=httpx.Response(
			200, json={"audio_base64": "YXVkaW8=", "alignment": {"bad": True}}
		)
	)
	result = ElevenLabsTTS(voice="test").synthesize("hello", tmp_path / "speech")
	assert result.audio_path.read_bytes() == b"audio"
	assert result.alignment is None


def test_previous_output_stays_until_spoken_cue_then_is_replaced():
	scene = continuity_manifest().scenes[3]
	ctx = SlideContext(width=640, height=360)
	before = {"recall": "BLUE"}
	design = EditorialScene(scene, ctx, 5, {"node:recall": 3}, before)
	area = design.rect((1340, 650, 1775, 738))
	early = design.frame(0).crop(area).tobytes()
	assert early == design.frame(2.9).crop(area).tobytes()
	assert early != design.frame(4).crop(area).tobytes()
	# Final output must not contain remnants of the previous value.
	fresh = EditorialScene(scene, ctx, 5)
	assert fresh.poster().crop(area).tobytes() == design.poster().crop(area).tobytes()


def test_unverified_numeric_node_result_is_removed(ctx):
	from pipeline.stages.script import ScriptStage

	manifest = continuity_manifest()
	manifest.scenes[0].visual.diagram.nodes[0].value = "99.9 BLEU"
	ScriptStage(ctx)._check_comparisons(manifest, "Table 3 gives 25.8 BLEU")
	assert manifest.scenes[0].visual.diagram.nodes[0].value == ""


def test_optional_model_note_does_not_lose_narration():
	from pipeline.schemas import SceneManifest
	from pipeline.stages.script import _attach_direction, _detach_direction

	payload = continuity_manifest().model_dump()
	payload["scenes"][0]["visual"]["note"] = "Non-rendered direction"
	held = _detach_direction(payload)
	manifest = SceneManifest.model_validate(payload)
	_attach_direction(manifest, held)
	assert manifest.scenes[0].narration == payload["scenes"][0]["narration"]


def test_new_anthropic_sdk_without_sampling_control():
	from types import SimpleNamespace

	from pipeline.llm.providers import AnthropicClient

	seen = {}

	def create(*, model, max_tokens, messages):
		seen.update(model=model, max_tokens=max_tokens, messages=messages)
		return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=0))

	client = object.__new__(AnthropicClient)
	client.model = "test"
	client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
	assert client.complete("hello", temperature=0.6).input_tokens == 1
	assert seen["messages"][0]["content"] == "hello"


def test_demo_writes_complete_voice_metadata_and_reuses_it(tmp_path):
	import json
	import shutil

	from pipeline.demo import render_demo
	from pipeline.schemas import SceneManifest

	if not shutil.which("ffmpeg"):
		pytest.skip("ffmpeg unavailable")
	scene = continuity_manifest().scenes[0].model_copy(deep=True)
	scene.est_seconds = 0.5
	manifest = SceneManifest(arxiv_id="episode", scenes=[scene])
	render_demo(tmp_path, height=180, manifest=manifest)
	voice = json.loads((tmp_path / "preview/voice/voice.json").read_text())
	assert voice["provider"] == "silent"
	assert voice["model"] == "preview"
	render_demo(tmp_path, height=180, manifest=manifest, reuse_audio=True)
	# Refuse an edited transcript before overwriting the existing script.
	scene.narration = "Different words."
	from pipeline.stage import StageError

	with pytest.raises(StageError, match="Narration differs"):
		render_demo(tmp_path, height=180, manifest=manifest, reuse_audio=True)
