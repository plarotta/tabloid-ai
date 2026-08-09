"""Stages 7 and 8 without a TTS engine, a network, or ffmpeg.

Slide composition is a pure function of (visual, context) -> image, so it is
tested by asserting on the produced pixels. The ffmpeg invocation is tested
through the concat-list builder, which is where the audio/video sync actually
comes from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from pipeline.paths import write_json
from pipeline.render.slides import (
	ACCENT,
	BG,
	CARD,
	SlideContext,
	bullet_slide,
	figure_slide,
	render_visual,
	result_callout,
	title_card,
	wrap,
)
from pipeline.schemas import (
	EpisodeMetadata,
	SceneAudio,
	SceneManifest,
	ScriptResult,
	SegmentAudio,
	Visual,
	VoiceResult,
)
from pipeline.stage import StageError
from pipeline.stages.render import RenderStage, concat_list
from pipeline.stages.voice import VoiceStage
from pipeline.tts.base import SpeechResult, TTSClient

CTX = SlideContext(width=640, height=360, arxiv_id="2608.00001")


def colours(img: Image.Image) -> set[tuple[int, int, int]]:
	return {c for _, c in img.convert("RGB").getcolors(maxcolors=1_000_000)}


# --- slide composition -------------------------------------------------------


def test_title_card_uses_the_visual_system():
	img = title_card(CTX, "A Clear Finding")
	assert img.size == (640, 360)
	cols = colours(img)
	assert BG in cols, "dark background"
	assert ACCENT in cols, "accent rule present"


def test_slides_are_not_blank():
	"""A slide that renders as a flat rectangle would ship as a blank frame."""
	for img in (
		title_card(CTX, "Title"),
		bullet_slide(CTX, "Heading", ["one", "two"]),
		result_callout(CTX, "41 percent"),
	):
		assert len(colours(img)) > 1


def test_bullet_slide_without_title_is_centred():
	"""Regression: bullets used to sit high with the frame empty beneath them.

	Rendered at the shipping resolution, and the bottom band is excluded because
	the arXiv footer lives there and would drag the measured centre down.
	"""
	full = SlideContext(width=1920, height=1080, arxiv_id="2608.00001")
	img = bullet_slide(full, "", ["alpha", "beta"])
	px = img.convert("RGB").load()
	body = full.height - 160  # above the footer
	rows = [y for y in range(body) if any(px[x, y] != BG for x in range(0, full.width, 3))]
	assert rows, "something was drawn"
	centre = (rows[0] + rows[-1]) / 2
	assert abs(centre - full.height / 2) < full.height * 0.15


def test_figure_slide_puts_the_figure_on_a_light_card(tmp_path: Path):
	"""Paper figures are drawn for white paper; on a dark background their axes
	and text disappear."""
	fig = tmp_path / "fig01.png"
	Image.new("RGB", (400, 300), (20, 90, 200)).save(fig)
	img = figure_slide(CTX, fig, "Figure 1, arXiv:2608.00001", "A title")
	cols = colours(img)
	assert CARD in cols, "light card behind the figure"
	assert BG in cols, "dark surround"


def test_figure_slide_survives_a_missing_file(tmp_path: Path):
	"""A missing figure must degrade to a placeholder, not crash the render."""
	img = figure_slide(CTX, tmp_path / "nope.png", "Figure 9, arXiv:x")
	assert img.size == (640, 360)


def test_transparent_figure_is_flattened_onto_the_card(tmp_path: Path):
	"""RGBA figures composited onto black would render as black-on-black."""
	fig = tmp_path / "t.png"
	Image.new("RGBA", (200, 150), (255, 0, 0, 0)).save(fig)
	img = figure_slide(CTX, fig, "Figure 1, arXiv:x")
	assert CARD in colours(img)


def test_render_visual_dispatches_each_type(tmp_path: Path):
	fig = tmp_path / "figures" / "fig01.png"
	fig.parent.mkdir()
	Image.new("RGB", (100, 80), (200, 30, 30)).save(fig)
	ctx = SlideContext(width=640, height=360, arxiv_id="x", figures_dir=fig.parent)

	assert render_visual(ctx, Visual(type="title_card", title="T")).size == (640, 360)
	assert render_visual(ctx, Visual(type="bullet_slide", bullets=["a"])).size == (640, 360)
	assert render_visual(ctx, Visual(type="result_callout", highlight="9")).size == (640, 360)
	img = render_visual(ctx, Visual(type="figure", figure_file="figures/fig01.png"))
	assert CARD in colours(img)


def test_figure_type_without_a_figures_dir_still_renders():
	"""Falls back rather than raising when Stage 3 produced nothing."""
	ctx = SlideContext(width=640, height=360, arxiv_id="x", figures_dir=None)
	assert render_visual(ctx, Visual(type="figure", figure_file="figures/a.png")).size == (640, 360)


def test_long_title_shrinks_to_fit():
	long = " ".join(["Supercalifragilistic"] * 24)
	img = title_card(CTX, long)
	px = img.convert("RGB").load()
	# Nothing drawn in the bottom band means the text did not overflow the frame.
	assert all(px[x, CTX.height - 3] == BG for x in range(0, CTX.width, 7))


def test_wrap_respects_width():
	img = Image.new("RGB", (200, 100))
	from PIL import ImageDraw

	from pipeline.render.slides import regular

	d = ImageDraw.Draw(img)
	f = regular(20)
	lines = wrap(d, "word " * 40, f, 180)
	assert len(lines) > 1
	assert all(d.textlength(ln, font=f) <= 180 for ln in lines)


# --- ffmpeg concat lists (where sync comes from) -----------------------------


def test_concat_list_without_durations():
	out = concat_list([Path("/a/one.aiff"), Path("/a/two.aiff")])
	assert out.splitlines() == ["file '/a/one.aiff'", "file '/a/two.aiff'"]


def test_concat_list_repeats_the_last_image():
	"""The concat demuxer drops the final image unless it is repeated."""
	out = concat_list([Path("/s/a.png"), Path("/s/b.png")], [2.5, 4.0])
	lines = out.splitlines()
	assert lines == [
		"file '/s/a.png'",
		"duration 2.5000",
		"file '/s/b.png'",
		"duration 4.0000",
		"file '/s/b.png'",
	]


def test_concat_durations_come_from_measured_audio():
	"""Sync depends on the video timeline using measured, not estimated, times."""
	measured = [3.31, 9.87]
	out = concat_list([Path("a.png"), Path("b.png")], measured)
	assert "duration 3.3100" in out and "duration 9.8700" in out


# --- voice stage -------------------------------------------------------------


class FakeTTS(TTSClient):
	provider = "fake"

	def __init__(self, duration=4.0):
		self.duration = duration
		self.calls = 0

	def synthesize(self, text, out_path, voice=None):
		self.calls += 1
		out_path = out_path.with_suffix(".aiff")
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_bytes(b"FAKEAUDIO")
		return SpeechResult(out_path, self.duration, len(text), self.provider, "fake-1")


def seed_script(ctx, scenes=2):
	manifest = SceneManifest(
		arxiv_id="2608.00001",
		scenes=[
			{
				"id": f"s{i + 1}",
				"narration": f"Spoken line number {i + 1}.",
				"visual": {"type": "title_card", "title": "T"},
				"est_seconds": 6.0,
			}
			for i in range(scenes)
		],
	)
	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="t", description="d"),
	)
	write_json(ctx.paths.stage_dir("script") / "script.json", json.loads(result.model_dump_json()))
	return manifest


def test_voice_measures_rather_than_estimates(ctx, monkeypatch):
	seed_script(ctx, scenes=2)
	fake = FakeTTS(duration=4.0)
	monkeypatch.setattr("pipeline.stages.voice.build_tts_client", lambda *a, **k: fake)
	ctx.config.tts.provider = "fake"

	result = VoiceStage(ctx).run()
	assert fake.calls == 2
	clip = result.segments[0].scenes[0]
	assert clip.duration_seconds == 4.0  # measured
	assert clip.est_seconds == 6.0  # scripted
	assert clip.drift_seconds == -2.0


def test_voice_skips_empty_narration(ctx, monkeypatch):
	manifest = seed_script(ctx, scenes=2)
	manifest.scenes[0].narration = "   "
	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="t", description="d"),
	)
	write_json(ctx.paths.stage_dir("script") / "script.json", json.loads(result.model_dump_json()))
	fake = FakeTTS()
	monkeypatch.setattr("pipeline.stages.voice.build_tts_client", lambda *a, **k: fake)
	assert len(VoiceStage(ctx).run().segments[0].scenes) == 1


def test_voice_reports_a_clear_error_without_a_provider(ctx):
	seed_script(ctx)
	ctx.config.tts.provider = None
	with pytest.raises(StageError, match=r"tts\.provider"):
		VoiceStage(ctx).run()


# --- render stage guard ------------------------------------------------------


def test_render_without_ffmpeg_fails_clearly(ctx, monkeypatch):
	seed_script(ctx)
	voice = VoiceResult(
		generated_at=datetime.now(UTC),
		provider="fake",
		model="f",
		segments=[
			SegmentAudio(
				arxiv_id="2608.00001",
				scenes=[
					SceneAudio(scene_id="s1", audio_file="a.aiff", duration_seconds=1, characters=5)
				],
			)
		],
	)
	write_json(ctx.paths.stage_dir("voice") / "voice.json", json.loads(voice.model_dump_json()))
	monkeypatch.setattr("pipeline.stages.render.have_ffmpeg", lambda: False)
	with pytest.raises(StageError, match="ffmpeg"):
		RenderStage(ctx).run()


# --- crossfade arithmetic (silently truncates narration when wrong) ----------


def _xfade(n, durations, fade):
	from pipeline.stages.render import RenderStage

	return RenderStage.__dict__["_xfade_filter"](None, n, durations, fade)


def test_xfade_offsets_are_cumulative():
	"""Regression: offsets accumulated `d - fade` per step, which subtracted the
	fade once per transition and cut the last seconds of narration."""
	graph, out = _xfade(3, [10.0, 20.0, 30.0], 0.4)
	assert "offset=9.600" in graph  # 10 - 0.4
	assert "offset=29.600" in graph  # 10 + 20 - 0.4, NOT 29.2
	assert out == "x2"


def test_xfade_preserves_total_duration():
	"""Each clip is padded by `fade`; every transition must consume exactly that,
	so the finished segment still matches the narration length."""
	durations = [6.0, 9.0, 4.5, 12.25]
	fade = 0.4
	graph, _ = _xfade(len(durations), durations, fade)
	offsets = [float(p.split("offset=")[1].split("[")[0]) for p in graph.split(";")]
	# Output length of the chain is the final offset plus the last padded clip.
	assert offsets[-1] + (durations[-1] + fade) == pytest.approx(sum(durations))


def test_xfade_single_clip_has_no_transitions():
	graph, out = _xfade(1, [5.0], 0.4)
	assert graph == "" and out == "0:v"
