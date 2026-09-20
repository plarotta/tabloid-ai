"""Editorial behavior and the real ffmpeg boundary where sync can go wrong."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from datetime import UTC, datetime

import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner

from pipeline.cli import app
from pipeline.demo import demo_manifest
from pipeline.editorial import pacing_report
from pipeline.render.editorial import EditorialScene
from pipeline.render.slides import ACCENT, BASE, SlideContext
from pipeline.schemas import (
	Comparison,
	EpisodeMetadata,
	Scene,
	SceneAudio,
	SceneManifest,
	ScriptResult,
	SegmentAudio,
	Transition,
	VoiceResult,
)
from pipeline.stage import StageError
from pipeline.stages.render import RenderStage, Slide
from pipeline.stages.script import _attach_direction, _detach_direction
from pipeline.stages.voice import VoiceStage
from pipeline.tts.base import SpeechResult


def _script(manifest):
	return ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="test", description="test"),
	)


def test_optional_bad_direction_preserves_the_scene():
	payload = demo_manifest().model_dump()
	payload["scenes"][0].update(beat="excitement", pause_after=100)
	held = _detach_direction(payload)
	manifest = SceneManifest.model_validate(payload)
	_attach_direction(manifest, held)
	assert manifest.scenes[0].narration == demo_manifest().scenes[0].narration
	assert manifest.scenes[0].beat is None
	assert manifest.scenes[0].pause_after is None
	assert manifest.scenes[1].beat == "context"
	assert manifest.scenes[1].pause_after == 0.2


def test_no_bridge_into_paper_one_does_not_renumber_the_remaining_chapters():
	script = _script(demo_manifest())
	script.segments = [
		demo_manifest().model_copy(update={"arxiv_id": f"paper{i}"}) for i in range(3)
	]
	script.episode.transitions = [
		Transition(into_arxiv_id=f"paper{i}", narration="Next paper.", est_seconds=2)
		for i in (1, 2)
	]
	manifests = [script.transition_manifest(t) for t in script.episode.transitions]
	assert [m.scenes[0].visual.highlight for m in manifests] == ["02 / 03", "03 / 03"]
	assert [m.scenes[0].id for m in manifests] == ["t1", "t2"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_comparison_values_cannot_reach_a_chart(value):
	with pytest.raises(ValidationError):
		Comparison(template="count_up", label_a="Result", value_a=value)


def test_bars_use_the_actual_ratio_and_share_an_origin():
	scene = demo_manifest().scenes[4]
	design = EditorialScene(scene, SlideContext(width=640, height=360), 5)
	poster = design.poster()
	row_a, row_b = design.y(385 + 120), design.y(609 + 120)
	# Measure the plot area, excluding the colored numeric labels to its right.
	xs_a = [x for x in range(design.x(1376) + 1) if poster.getpixel((x, row_a)) == ACCENT]
	xs_b = [x for x in range(design.x(1376) + 1) if poster.getpixel((x, row_b)) == BASE]
	assert min(xs_a) == min(xs_b)
	assert len(xs_a) / len(xs_b) == pytest.approx(4, rel=0.02)


def test_motion_reveals_the_method_and_then_holds_it():
	design = EditorialScene(demo_manifest().scenes[2], SlideContext(width=640, height=360), 7)
	assert design.frame(0.5).tobytes() != design.frame(4).tobytes()
	assert design.frame(6).tobytes() == design.frame(7).tobytes()
	# Seeking backwards is deterministic; there is no accumulated animation state.
	assert design.frame(0.5).tobytes() == design.frame(0.5).tobytes()


def test_missing_figure_keeps_a_complete_readable_composition(tmp_path):
	scene = demo_manifest().scenes[3]
	design = EditorialScene(scene, SlideContext(width=640, height=360, figures_dir=tmp_path), 5)
	assert design.poster().size == (640, 360)
	assert len(design.poster().getcolors(1_000_000)) > 30


def test_static_fallback_uses_completed_editorial_layout(ctx, monkeypatch, tmp_path):
	ctx.config.render.visual_style = "editorial"
	monkeypatch.setattr("pipeline.render.editorial.render_clip", lambda *a, **k: None)
	manifest = SceneManifest(arxiv_id="episode", scenes=[demo_manifest().scenes[2]])
	audio = SegmentAudio(
		arxiv_id="episode",
		scenes=[
			SceneAudio(
				scene_id="s3",
				audio_file="s3.wav",
				duration_seconds=7,
				characters=10,
			)
		],
	)
	slides = RenderStage(ctx)._slides_for(manifest, audio, tmp_path, "demo")
	assert len(slides) == 1 and not slides[0].animated
	with Image.open(slides[0].path) as poster:
		assert poster.size == (640, 360)
		assert ACCENT in [color for _, color in poster.getcolors(1_000_000)]


def test_pacing_report_uses_audio_by_scene_id_and_marks_missing_estimates():
	manifest = demo_manifest()
	voice = VoiceResult(
		generated_at=datetime.now(UTC),
		provider="fixture",
		model="fixture",
		segments=[
			SegmentAudio(
				arxiv_id="episode",
				scenes=[
					SceneAudio(
						scene_id="s2", audio_file="s2.wav", duration_seconds=2, characters=10
					),
					SceneAudio(
						scene_id="s1", audio_file="s1.wav", duration_seconds=13, characters=10
					),
				],
			)
		],
	)
	report = pacing_report(_script(manifest), voice)
	assert report["timing"] == "mixed"
	assert report["scenes"][0]["duration_seconds"] == 13
	assert report["scenes"][1]["start_seconds"] == 13
	assert report["scenes"][1]["duration_seconds"] == 2
	assert report["scenes"][2]["timing"] == "estimated"
	assert {i["code"] for i in report["issues"]} >= {"slow_hook", "long_scene"}


def test_missing_middle_audio_fails_before_any_encoding(ctx, monkeypatch):
	manifest = SceneManifest(arxiv_id="episode", scenes=demo_manifest().scenes[:3])
	voice_dir = ctx.paths.stage_dir("voice")
	(voice_dir / "s1.wav").write_bytes(b"test")
	(voice_dir / "s3.wav").write_bytes(b"test")
	audio = SegmentAudio(
		arxiv_id="episode",
		scenes=[
			SceneAudio(scene_id=s.id, audio_file=f"{s.id}.wav", duration_seconds=3, characters=5)
			for s in manifest.scenes
		],
	)
	monkeypatch.setattr("pipeline.stages.render.run_ffmpeg", lambda *a, **k: pytest.fail("encoded"))
	with pytest.raises(StageError, match="missing audio for 's2'"):
		RenderStage(ctx)._build_segment(manifest, audio, "demo")


def test_scene_pauses_allow_zero_and_bridge_override(ctx, monkeypatch):
	pauses = []
	monkeypatch.setattr(
		"pipeline.stages.voice.append_silence",
		lambda path, seconds, reported: pauses.append(seconds) or reported + seconds,
	)
	monkeypatch.setattr("pipeline.stages.voice.shutil.which", lambda name: "/bin/ffmpeg")

	class Client:
		def synthesize(self, text, path, voice=None):
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"test")
			return SpeechResult(path, 2, len(text), "fixture", "fixture")

	manifest = SceneManifest(arxiv_id="episode", scenes=demo_manifest().scenes[:2])
	manifest.scenes[0].pause_after = 0
	manifest.scenes[1].pause_after = 0.5
	VoiceStage(ctx)._narrate(Client(), manifest, "demo")
	assert pauses == [0.5]
	pauses.clear()
	VoiceStage(ctx)._narrate(Client(), manifest, "bridge", gap=0.3)
	assert pauses == [0.3, 0.3]


def test_elevenlabs_receives_context_without_speaking_it(tmp_path, monkeypatch, respx_mock):
	import httpx

	from pipeline.tts.providers import ElevenLabsTTS

	monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
	monkeypatch.setattr("pipeline.tts.providers.measure_duration", lambda path: 2.0)
	route = respx_mock.post("https://api.elevenlabs.io/v1/text-to-speech/test-voice").mock(
		return_value=httpx.Response(200, content=b"test-audio")
	)
	result = ElevenLabsTTS(voice="test-voice").synthesize_with_context(
		"The actual line.",
		tmp_path / "scene",
		previous_text="The preceding line.",
		next_text="The following line.",
	)
	body = json.loads(route.calls.last.request.content)
	assert body["text"] == "The actual line."
	assert body["previous_text"] == "The preceding line."
	assert body["next_text"] == "The following line."
	assert result.characters == len("The actual line.")


def test_delivery_context_stays_inside_its_segment(ctx, monkeypatch):
	seen = []
	ctx.config.tts.scene_gap_seconds = 0

	class Client:
		def synthesize_with_context(self, text, path, voice=None, **context):
			seen.append(context)
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"test")
			return SpeechResult(path, 2, len(text), "fixture", "fixture")

	manifest = SceneManifest(arxiv_id="episode", scenes=demo_manifest().scenes[:2])
	for scene in manifest.scenes:
		scene.pause_after = 0
	stage = VoiceStage(ctx)
	stage._narrate(Client(), manifest, "one")
	stage._narrate(Client(), manifest, "two")
	assert seen[0]["previous_text"] == seen[2]["previous_text"] == ""
	assert seen[1]["next_text"] == seen[3]["next_text"] == ""
	assert seen[0]["next_text"] == manifest.scenes[1].narration


def test_review_missing_script_is_a_clear_error(tmp_path, monkeypatch):
	monkeypatch.setattr("pipeline.paths.RUNS_DIR", tmp_path)
	result = CliRunner().invoke(app, ["review", "--run", "missing"])
	assert result.exit_code == 1
	assert "No script found" in result.stdout


def test_through_rejects_backwards_stage_order():
	result = CliRunner().invoke(app, ["run", "--from", "voice", "--through", "script"])
	assert result.exit_code == 2
	assert "at or after" in result.stdout


def test_early_stop_does_not_advance_a_preexisting_window(ctx, monkeypatch):
	from pipeline import cli

	called = []

	class FakeStage:
		def __init__(self, context):
			pass

		def run(self):
			called.append("stage")

	monkeypatch.setattr(cli, "_build_context", lambda *a: ctx)
	monkeypatch.setattr(cli, "STAGES", {"fetch": FakeStage, "script": FakeStage})
	monkeypatch.setattr(cli, "_advance_window", lambda *a: pytest.fail("advanced window"))
	result = CliRunner().invoke(app, ["run", "--through", "script"])
	assert result.exit_code == 0, result.stdout
	assert len(called) == 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("fade", [0.0, 0.16])
def test_real_animated_cuts_preserve_duration_and_boundary(ctx, monkeypatch, tmp_path, fade):
	"""An animation in scene two must not begin early during a dissolve."""
	cfg = ctx.config.render
	cfg.width, cfg.height, cfg.fps, cfg.crossfade_seconds = 320, 180, 30, fade
	durations = [1.03, 1.07]
	clips = []
	for i, color in enumerate(("red", "blue")):
		video = tmp_path / f"scene{i}.mp4"
		# The incoming scene changes from blue to lime 0.4s into its own clock.
		subprocess.run(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel",
				"error",
				"-y",
				"-f",
				"lavfi",
				"-i",
				f"color=c={color}:s=320x180:r=30:d=1.3",
				"-vf",
				"drawbox=x=0:y=0:w=iw:h=ih:color=lime:t=fill:enable='gte(t,0.4)'",
				"-c:v",
				"libx264",
				"-threads",
				"1",
				"-pix_fmt",
				"yuv420p",
				str(video),
			],
			check=True,
			capture_output=True,
		)
		clips.append(Slide(video, "result_callout", "test", animated=True))
	manifest = SceneManifest(
		arxiv_id="episode",
		scenes=[
			Scene(
				id=f"s{i}",
				narration="test",
				visual={"type": "title_card", "title": "test"},
				est_seconds=duration,
			)
			for i, duration in enumerate(durations)
		],
	)
	audios = []
	for i, duration in enumerate(durations):
		file = ctx.paths.stage_dir("voice") / f"s{i}.wav"
		with wave.open(str(file), "wb") as wav:
			wav.setnchannels(1)
			wav.setsampwidth(2)
			wav.setframerate(44100)
			wav.writeframes(b"\0\0" * round(duration * 44100))
		audios.append(
			SceneAudio(
				scene_id=f"s{i}", audio_file=file.name, duration_seconds=duration, characters=4
			)
		)
	monkeypatch.setattr(RenderStage, "_slides_for", lambda *a, **k: clips)
	video, duration = RenderStage(ctx)._build_segment(
		manifest,
		SegmentAudio(arxiv_id="episode", scenes=audios),
		"test",
	)
	probe = json.loads(
		subprocess.check_output(
			[
				"ffprobe",
				"-v",
				"error",
				"-show_entries",
				"stream=codec_type,duration",
				"-of",
				"json",
				str(video),
			]
		)
	)
	lengths = {s["codec_type"]: float(s["duration"]) for s in probe["streams"]}
	assert duration == pytest.approx(2.1)
	assert lengths["video"] == pytest.approx(duration, abs=1 / 30)
	assert abs(lengths["video"] - lengths["audio"]) <= 1 / 30
	# 0.3s into scene two: still blue. Without first-frame padding its animation
	# is 0.16s early and this pixel has already turned lime.
	pixel = subprocess.check_output(
		[
			"ffmpeg",
			"-v",
			"error",
			"-ss",
			"1.33",
			"-i",
			str(video),
			"-frames:v",
			"1",
			"-vf",
			"scale=1:1",
			"-f",
			"rawvideo",
			"-pix_fmt",
			"rgb24",
			"pipe:1",
		]
	)
	assert pixel[2] > 200 and pixel[1] < 30, list(pixel)
