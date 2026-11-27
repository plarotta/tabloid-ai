"""Stages 7 and 8 without a TTS engine, a network, or ffmpeg.

Slide composition is a pure function of (visual, context) -> image, so it is
tested by asserting on the produced pixels. The ffmpeg invocation is tested
through the concat-list builder, which is where the audio/video sync actually
comes from.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from pipeline.paths import write_json
from pipeline.render import captions
from pipeline.render.slides import (
	BG,
	CARD,
	SlideContext,
	accent_for,
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
	Transition,
	Visual,
	VoiceResult,
)
from pipeline.stage import StageError
from pipeline.stages.render import RenderStage, Slide, concat_list
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
	assert BG in cols, "dark ground"
	assert CTX.accent in cols, "accent rule present"


def test_every_part_wears_the_one_accent():
	"""D27 gave each paper its own hue; D35 put episode one's single accent back.
	Whatever the running order, the accent is the same and it reaches the frame."""
	from pipeline.render.slides import ACCENT

	for index in (None, 0, 1, 2):
		ctx = SlideContext(width=640, height=360, arxiv_id="x", paper_index=index)
		assert ctx.accent == ACCENT
		assert ACCENT in colours(title_card(ctx, "A Clear Finding"))


def test_the_running_order_still_indexes_the_accent_list():
	"""The per-paper seam is kept even though the list holds one colour, because
	a paper index out of range would raise rather than wrap."""
	from pipeline.render.slides import PAPER_ACCENTS, accent_for

	assert len(PAPER_ACCENTS) >= 1
	assert accent_for(7) == PAPER_ACCENTS[7 % len(PAPER_ACCENTS)]


def test_transition_card_does_not_share_the_title_card_ground():
	"""Its whole job is to be unmistakably not-a-segment for a few seconds. On the
	plain ground it would read as one more title card, which is the monotony it
	exists to break."""
	from pipeline.render.slides import tint, transition_card

	ctx = SlideContext(width=640, height=360, arxiv_id="x", paper_index=0)
	cols = colours(transition_card(ctx, "Another way in", "02 / 03"))
	assert tint(ctx.accent) in cols
	assert BG not in cols, "the bridge must not sit on the segment ground"
	assert colours(title_card(ctx, "Another way in")) != cols


def test_the_bridge_ground_is_the_accent_washed_into_the_ground():
	"""Under D27 the wash carried the *next* paper's hue, so the colour of the
	coming chapter arrived a beat early. With one accent there is no such signal
	left - the wash now only says "not a segment", which is the job that
	survives (D35)."""
	from pipeline.render.slides import tint, transition_card

	for index in (0, 1, 2):
		ctx = SlideContext(width=320, height=180, paper_index=index)
		assert tint(accent_for(index)) in colours(transition_card(ctx, "x", "01"))


def test_transition_card_renders_without_a_marker():
	from pipeline.render.slides import transition_card

	assert transition_card(CTX, "Just a label").size == (640, 360)


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


def test_figure_slide_puts_the_figure_on_a_white_card(tmp_path: Path):
	"""On the paper ground the card no longer rescues the figure from a clashing
	background, but it still marks its edge (D27)."""
	fig = tmp_path / "fig01.png"
	Image.new("RGB", (400, 300), (20, 90, 200)).save(fig)
	img = figure_slide(CTX, fig, "Figure 1, arXiv:2608.00001", "A title")
	cols = colours(img)
	assert CARD in cols, "white card behind the figure"
	assert BG in cols, "paper surround"


def test_figure_slide_survives_a_missing_file(tmp_path: Path):
	"""A missing figure must degrade to a placeholder, not crash the render."""
	img = figure_slide(CTX, tmp_path / "nope.png", "Figure 9, arXiv:x")
	assert img.size == (640, 360)


def test_transparent_figure_is_flattened_onto_the_card(tmp_path: Path):
	"""Transparency must be flattened onto the card, not onto whatever is behind
	it - a figure with an alpha channel would otherwise composite unpredictably."""
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
		# Every per-call settings dict Stage 7 asked for, in order.
		self.settings_seen: list[dict | None] = []

	def synthesize(self, text, out_path, voice=None, settings=None):
		self.calls += 1
		self.settings_seen.append(settings)
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


def test_voice_narrates_transitions_keyed_to_their_paper(ctx, monkeypatch):
	"""Stages 8 and 9 look bridges up by the paper they lead into, so the audio
	has to carry that id rather than the "episode" its manifest reports."""
	manifest = seed_script(ctx, scenes=1)
	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(
			title="t",
			description="d",
			transitions=[
				Transition(
					into_arxiv_id="2608.00001",
					narration="And the way in is not always physical.",
					label="Another way in",
					est_seconds=5.0,
				)
			],
		),
	)
	write_json(ctx.paths.stage_dir("script") / "script.json", json.loads(result.model_dump_json()))
	monkeypatch.setattr("pipeline.stages.voice.build_tts_client", lambda *a, **k: FakeTTS(3.0))
	ctx.config.tts.provider = "fake"

	voice = VoiceStage(ctx).run()
	assert [t.arxiv_id for t in voice.transitions] == ["2608.00001"]
	assert voice.transitions[0].duration_seconds == 3.0
	# One segment scene plus the bridge, both counted toward the episode.
	assert voice.duration_seconds == 6.0


def test_a_bridge_gets_a_longer_beat_than_a_scene_change(ctx, monkeypatch):
	"""The bridge is a bare signpost now - "the second paper is about X" - so the
	silence after it is what actually separates two papers (D35)."""
	manifest = seed_script(ctx, scenes=1)
	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(
			title="t",
			description="d",
			transitions=[
				Transition(
					into_arxiv_id="2608.00001",
					narration="The second paper is about inferring physics from one video.",
					label="Physics from video",
					est_seconds=5.0,
				)
			],
		),
	)
	write_json(ctx.paths.stage_dir("script") / "script.json", json.loads(result.model_dump_json()))

	asked: list[float] = []

	def record(path, seconds, reported):
		asked.append(seconds)
		return reported + seconds

	monkeypatch.setattr("pipeline.stages.voice.append_silence", record)
	monkeypatch.setattr("pipeline.stages.voice.build_tts_client", lambda *a, **k: FakeTTS(3.0))
	ctx.config.tts.provider = "fake"
	ctx.config.tts.scene_gap_seconds = 0.35
	ctx.config.tts.bridge_pause_seconds = 1.0

	VoiceStage(ctx).run()
	assert asked == [0.35, 1.0], "the segment scene takes the scene gap, the bridge the pause"


def test_a_paper_run_does_not_narrate_bridges(ctx, monkeypatch):
	"""A standalone segment must not open mid-thought - that is why bridges are
	episode-level in the first place."""
	manifest = seed_script(ctx, scenes=1)
	result = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(
			title="t",
			description="d",
			transitions=[
				Transition(into_arxiv_id="2608.00001", narration="Bridge.", est_seconds=5.0)
			],
		),
	)
	write_json(ctx.paths.stage_dir("script") / "script.json", json.loads(result.model_dump_json()))
	monkeypatch.setattr("pipeline.stages.voice.build_tts_client", lambda *a, **k: FakeTTS(3.0))
	ctx.config.tts.provider = "fake"

	voice = VoiceStage(dataclasses.replace(ctx, paper_filter="2608.00001")).run()
	assert voice.transitions == []
	assert voice.cold_open is None


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
	# `v0` is the caller's own pre-scaled/padded label, so a lone clip maps
	# straight through the same name the chain would have produced.
	assert graph == "" and out == "v0"


def test_xfade_consumes_prelabelled_streams():
	"""The chain reads [v0], [v1]...; the callers label their padded streams to
	match. It used to read raw [i:v] and get re-labelled by string replacement."""
	graph, _ = _xfade(3, [5.0, 5.0, 5.0], 0.4)
	assert "[v1]" in graph and "[v2]" in graph
	assert ":v]" not in graph, "no raw input labels left for a caller to patch"


# --- episode seams -----------------------------------------------------------

PARTS = [(Path("/r/cold_open.mp4"), 12.0), (Path("/r/bridge.mp4"), 5.0), (Path("/r/seg.mp4"), 75.0)]


def _stitch(ctx, monkeypatch, parts=PARTS, fade=0.6):
	"""Capture the ffmpeg argv `_stitch_episode` would run."""
	calls: list[list[str]] = []
	monkeypatch.setattr("pipeline.stages.render.run_ffmpeg", lambda args, what: calls.append(args))
	ctx.config.render.episode_crossfade_seconds = fade
	work = ctx.paths.stage_dir("render") / "_work"
	RenderStage(ctx)._stitch_episode(parts, Path("/r/episode.mp4"), work)
	assert len(calls) == 1
	return calls[0]


def _graph(argv: list[str]) -> str:
	return argv[argv.index("-filter_complex") + 1]


def test_episode_seams_are_cross_dissolved(ctx, monkeypatch):
	argv = _stitch(ctx, monkeypatch)
	graph = _graph(argv)
	assert graph.count("xfade") == len(PARTS) - 1
	assert "-c" not in argv, "a filtergraph cannot also be a stream copy"


def test_seam_padding_keeps_video_in_sync_with_the_audio(ctx, monkeypatch):
	"""The audio is a plain concat, so part i's *content* must still begin at the
	un-faded elapsed time. That holds only if each part after the first is padded
	at the START by exactly what the dissolve eats - padding the end instead
	slides every part `fade` seconds early against its own narration."""
	fade = 0.6
	graph = _graph(_stitch(ctx, monkeypatch, fade=fade))

	assert f"[0:v]tpad=stop_mode=clone:stop_duration={fade:.3f}" in graph
	assert "[0:v]tpad=start_mode" not in graph, "the first part sets the timeline"

	cum = 0.0
	for i in range(1, len(PARTS)):
		cum += PARTS[i - 1][1]
		assert f"[{i}:v]tpad=start_mode=clone:start_duration={fade:.3f}" in graph
		# Dissolve ends exactly on the boundary; the start pad gives it back.
		assert f"offset={cum - fade:.3f}" in graph


def test_seam_audio_is_concatenated_not_faded(ctx, monkeypatch):
	"""Cross-fading narration would clip the last syllable before each seam."""
	graph = _graph(_stitch(ctx, monkeypatch))
	assert f"concat=n={len(PARTS)}:v=0:a=1[a]" in graph
	assert "acrossfade" not in graph


def test_seam_crossfade_can_be_turned_off(ctx, monkeypatch):
	"""0 restores the stream copy, which is the only way to avoid a re-encode."""
	argv = _stitch(ctx, monkeypatch, fade=0.0)
	assert "-filter_complex" not in argv
	assert argv[argv.index("-c") + 1] == "copy"


def test_a_part_too_short_to_dissolve_falls_back_to_hard_cuts(ctx, monkeypatch):
	parts = [(Path("/r/a.mp4"), 12.0), (Path("/r/tiny.mp4"), 0.5)]
	argv = _stitch(ctx, monkeypatch, parts=parts, fade=0.6)
	assert "-filter_complex" not in argv


def test_a_failed_seam_crossfade_still_produces_an_episode(ctx, monkeypatch):
	"""A hard-cut episode beats no episode; the parts are already on disk."""
	calls: list[list[str]] = []

	def flaky(args, what):
		calls.append(args)
		if "-filter_complex" in args:
			raise StageError("no such filter: xfade")

	monkeypatch.setattr("pipeline.stages.render.run_ffmpeg", flaky)
	ctx.config.render.episode_crossfade_seconds = 0.6
	RenderStage(ctx)._stitch_episode(PARTS, Path("/r/episode.mp4"), ctx.paths.stage_dir("render"))
	assert len(calls) == 2
	assert calls[1][calls[1].index("-c") + 1] == "copy"


def test_speech_markup_is_stripped_for_engines_that_would_read_it():
	"""ElevenLabs interprets <break>; `say` and the OpenAI voices pronounce it.
	A published episode must never contain "break time zero point eight s"."""
	from pipeline.tts.providers import strip_ssml

	assert strip_ssml('One. <break time="0.8s" /> Two.') == "One. Two."
	assert strip_ssml("One. <break time='1s'> Two.") == "One. Two."
	assert strip_ssml("No markup here.") == "No markup here."


# --- captions (D33) ----------------------------------------------------------

CAP_CTX = dataclasses.replace(CTX, width=1920, height=1080)


def test_captions_lose_no_word_of_the_narration():
	"""The cues are the narration, re-cut. Anything else is a caption that
	contradicts the voice track."""
	line = "Agents fail on real workflows, and the gap is wider than reported."
	cues = captions.cues_for(line, 6.0)
	assert " ".join(c.text for c in cues) == line
	assert len(cues) > 1, "a sentence this long is not one caption"


def test_cues_tile_the_scene_exactly():
	"""Sync comes from the cues covering the scene end to end: a gap holds the
	previous caption over the next one's speech, an overlap truncates it."""
	cues = captions.cues_for("One thing happened, then a second thing happened.", 9.0, start=4.0)
	assert cues[0].start == 4.0
	assert cues[-1].end == 13.0  # snapped, not left wherever the division landed
	for a, b in itertools.pairwise(cues):
		assert a.end == b.start
	assert all(c.seconds > 0 for c in cues)


def test_cue_length_tracks_phrase_length():
	"""Timing is proportional to characters, which is the whole approximation."""
	short, long_ = captions.cues_for("Tiny bit here; a very much longer stretch of words.", 10.0)
	assert short.seconds < long_.seconds


def test_captions_never_show_speech_markup():
	"""The narration handed to ElevenLabs carries <break> markup. On screen it
	would read as literal angle-bracket noise."""
	cues = captions.cues_for('The result is clear. <break time="0.8s" /> Mostly.', 5.0)
	assert "break" not in " ".join(c.text for c in cues)
	assert "<" not in " ".join(c.text for c in cues)


def test_a_runt_tail_is_folded_into_the_cue_before_it():
	"""A two-word cue flashes for a fraction of a second and reads as a glitch."""
	phrases = captions.split_phrases("A reasonably long opening clause here, and so.")
	assert phrases[-1] != "and so."
	assert phrases[-1].endswith("and so.")


def test_captions_break_at_clauses_rather_than_mid_thought():
	phrases = captions.split_phrases("It scores well on paper, but the audit tells another story.")
	assert phrases[0] == "It scores well on paper,"


def test_empty_narration_produces_no_cues():
	assert captions.cues_for("   ", 5.0) == []
	assert captions.cues_for("Something.", 0.0) == []


def test_a_blank_caption_frame_is_fully_transparent():
	"""Gaps in the caption track are covered by this frame, so anything drawn on
	it would sit over the slide for the whole silence."""
	assert captions.caption_image(CAP_CTX, "").getbbox() is None


def test_the_caption_pill_stays_inside_the_reserved_band():
	"""The band is what the slides give up. A pill outside it lands on the
	arXiv attribution or the progress bar."""
	ctx = dataclasses.replace(CAP_CTX)
	band = captions.band_height(ctx)
	box = captions.caption_image(ctx, "A caption of ordinary length.").getbbox()
	assert box is not None
	assert box[1] >= ctx.height - band, "pill starts above the band it was given"
	assert box[3] <= ctx.height
	assert box[0] >= 0 and box[2] <= ctx.width


def test_a_long_cue_shrinks_rather_than_overflowing():
	long_cue = "An unusually long folded caption that has to fit on one line regardless"
	box = captions.caption_image(CAP_CTX, long_cue).getbbox()
	assert box is not None and box[0] >= 0 and box[2] <= CAP_CTX.width


def test_captions_drop_a_dangling_comma():
	"""The comma is a split point, not something the viewer needs to see."""
	with_comma = captions.caption_image(CAP_CTX, "a browser to pull data,")
	without = captions.caption_image(CAP_CTX, "a browser to pull data")
	assert with_comma.tobytes() == without.tobytes()


# --- the caption band, as the slides see it ----------------------------------


def test_the_caption_band_lifts_the_chrome_out_of_the_way():
	banded = dataclasses.replace(CAP_CTX, caption_band=170, scene_total=6, scene_index=2)
	plain = dataclasses.replace(CAP_CTX, scene_total=6, scene_index=2)
	# The progress bar is the lowest thing the chrome draws.
	assert _lowest_ink(title_card(banded, "T")) < _lowest_ink(title_card(plain, "T"))


def test_the_caption_band_shortens_the_figure_card(tmp_path: Path):
	fig = tmp_path / "f1.png"
	Image.new("RGB", (400, 300), (10, 10, 10)).save(fig)
	banded = dataclasses.replace(CAP_CTX, caption_band=170)
	assert _lowest_ink(figure_slide(banded, fig, "Figure 1")) < _lowest_ink(
		figure_slide(CAP_CTX, fig, "Figure 1")
	)


def test_without_captions_the_slides_are_composed_exactly_as_before():
	"""`caption_band` defaults to zero, and every layout must then be untouched."""
	assert CAP_CTX.caption_band == 0
	assert CAP_CTX.content_height == CAP_CTX.height
	assert CAP_CTX.fitted(620) == CAP_CTX.scaled(620)


def _lowest_ink(img: Image.Image) -> int:
	"""The bottom of everything drawn, ignoring the flat background."""
	flat = Image.new("RGB", img.size, img.convert("RGB").getpixel((5, img.height // 2)))
	from PIL import ImageChops

	box = ImageChops.difference(img.convert("RGB"), flat).getbbox()
	return box[3] if box else 0


# --- Ken Burns and the caption overlay in the filtergraph --------------------


def test_a_figure_slide_gets_the_biggest_move(ctx):
	stage = RenderStage(ctx)
	ctx.config.render.motion = True
	figure = stage._video_chain("figure", 300)
	bullets = stage._video_chain("bullet_slide", 300)
	assert "zoompan" in figure and "zoompan" in bullets
	# Amplitude shows up twice: in the ramp and in its clamp.
	assert "1.0700" in figure and "1.0300" in bullets


def test_a_transition_card_is_deliberately_still(ctx):
	"""The bridge is a punctuation beat; its tinted ground already marks the
	change, and moving it as well makes the seam busy."""
	ctx.config.render.motion = True
	assert "zoompan" not in RenderStage(ctx)._video_chain("transition", 300)


def test_motion_off_restores_the_plain_scale_chain(ctx):
	ctx.config.render.motion = False
	chain = RenderStage(ctx)._video_chain("figure", 300)
	assert "zoompan" not in chain and chain.startswith("scale=")


def test_the_zoom_oversamples_so_the_crop_is_never_an_upscale(ctx):
	"""Zooming into a natively-sized still softens it. The input is scaled up by
	the amplitude first, so the tightest crop is still 1:1."""
	ctx.config.render.motion = True
	chain = RenderStage(ctx)._video_chain("figure", 300)
	w = ctx.config.render.width
	oversampled = int(chain.split("scale=")[1].split(":")[0])
	assert oversampled > w
	assert f"s={w}x{ctx.config.render.height}" in chain, "output is still frame-sized"


def test_the_zoom_emits_one_frame_per_input_frame(ctx):
	"""`d=1` is what keeps the frame count - and therefore every xfade offset
	and the A/V sync - exactly as it was without motion."""
	ctx.config.render.motion = True
	assert "d=1:" in RenderStage(ctx)._video_chain("figure", 300)


def test_captions_are_overlaid_after_the_dissolve_and_the_zoom(ctx):
	"""Overlaid earlier, a caption would fade out with the slide under it at
	every scene change and drift across the frame with the zoom."""
	graph, label = RenderStage(ctx)._overlay_captions("[v0]zoompan=x[x1]", "x1", 4)
	assert graph.index("zoompan") < graph.index("overlay")
	assert "[x1][cap]overlay" in graph
	assert label == "vout"


def test_the_caption_track_covers_every_second_of_the_part(ctx, tmp_path: Path):
	"""The concat demuxer has no notion of a hole: an uncovered stretch holds
	the previous caption over it instead of clearing."""
	cues = [captions.Cue("first", 0.0, 2.0), captions.Cue("second", 5.0, 7.0)]
	listing = RenderStage(ctx)._caption_track(CAP_CTX, cues, 10.0, tmp_path)
	durations = [
		float(ln.split()[1]) for ln in listing.read_text().splitlines() if ln.startswith("duration")
	]
	assert sum(durations) == pytest.approx(10.0)
	assert "blank.png" in listing.read_text()


def test_a_scene_with_no_narration_still_advances_the_caption_track(ctx):
	slides = [
		Slide(Path("a.png"), "title_card", "A spoken line here."),
		Slide(Path("b.png"), "figure", ""),
		Slide(Path("c.png"), "result_callout", "And the number lands."),
	]
	cues = RenderStage(ctx)._cues(slides, [4.0, 3.0, 5.0])
	assert cues[0].start == 0.0
	# The silent middle scene contributes nothing, so the last scene's captions
	# must still start at 7s rather than sliding up into the gap it left.
	assert not any(4.0 < c.start < 7.0 for c in cues)
	assert min(c.start for c in cues if c.start >= 4.0) == pytest.approx(7.0)
	assert cues[-1].end == pytest.approx(12.0)


def test_a_lone_bridge_card_does_not_warn_about_the_zoom(ctx, monkeypatch, caplog):
	"""A bridge is one transition card: it cannot cross-dissolve with itself and
	is deliberately still anyway, so warning about a lost move is noise."""
	monkeypatch.setattr("pipeline.stages.render.run_ffmpeg", lambda args, what: None)
	monkeypatch.setattr(
		RenderStage,
		"_slides_for",
		lambda self, *a, **k: [Slide(Path("t.png"), "transition", "One bridging line.")],
	)
	manifest = SceneManifest(
		arxiv_id="episode",
		scenes=[
			{
				"id": "t1",
				"narration": "One bridging line.",
				"visual": {"type": "transition", "title": "Next"},
				"est_seconds": 5.0,
			}
		],
	)
	audio = SegmentAudio(
		arxiv_id="episode",
		scenes=[SceneAudio(scene_id="t1", audio_file="t.aiff", duration_seconds=5.0, characters=8)],
	)
	(ctx.paths.stage_dir("voice") / "t.aiff").write_bytes(b"A")
	ctx.config.render.motion = True
	with caplog.at_level("WARNING"):
		RenderStage(ctx)._build_segment(manifest, audio, "bridge")
	assert "Ken Burns" not in caplog.text


# --- animated callouts: every way of declining leaves a renderable slide ------


def _callout_manifest(with_comparison: bool = True):
	comparison = (
		{
			"template": "two_bar",
			"label_a": "This paper",
			"value_a": 200,
			"label_b": "Prior rule",
			"value_b": 20,
			"note": "10x",
		}
		if with_comparison
		else None
	)
	return SceneManifest(
		arxiv_id="2608.00001",
		scenes=[
			{
				"id": f"s{i + 1}",
				"narration": f"Spoken line number {i + 1}.",
				"visual": {
					"type": "result_callout" if i == 1 else "title_card",
					"title": "T",
					"highlight": "200 tokens",
					"comparison": comparison if i == 1 else None,
				},
				"est_seconds": 6.0,
			}
			for i in range(3)
		],
	)


def _audio_for(manifest, seconds=5.0):
	from pipeline.schemas import SceneAudio

	return SegmentAudio(
		arxiv_id=manifest.arxiv_id,
		scenes=[
			SceneAudio(
				scene_id=s.id,
				audio_file=f"{manifest.arxiv_id}/{s.id}.mp3",
				duration_seconds=seconds,
				characters=40,
				est_seconds=s.est_seconds,
			)
			for s in manifest.scenes
		],
	)


def test_a_failed_animation_still_produces_a_slide(ctx, monkeypatch, tmp_path):
	"""Nothing about turning this on may be able to break a render: every way of
	declining has to leave a PNG behind (D36)."""
	monkeypatch.setattr("pipeline.render.animate.render_clip", lambda *a, **k: None)
	ctx.config.render.animated_callouts = True
	manifest = _callout_manifest()
	slides = RenderStage(ctx)._slides_for(
		manifest, _audio_for(manifest), tmp_path / "s", "seg", animate_ok=True, hold=0.4
	)
	assert len(slides) == 3
	assert all(s.path.suffix == ".png" for s in slides)
	assert not any(s.animated for s in slides)


def test_an_animated_scene_becomes_a_clip(ctx, monkeypatch, tmp_path):
	made = {}

	def fake(visual, slide_ctx, duration, out_dir, name, **kw):
		made["duration"] = duration
		out_dir.mkdir(parents=True, exist_ok=True)
		p = out_dir / f"{name}.mp4"
		p.write_bytes(b"CLIP")
		return p

	monkeypatch.setattr("pipeline.render.animate.render_clip", fake)
	ctx.config.render.animated_callouts = True
	manifest = _callout_manifest()
	slides = RenderStage(ctx)._slides_for(
		manifest, _audio_for(manifest, 5.0), tmp_path / "s", "seg", animate_ok=True, hold=0.4
	)
	assert [s.animated for s in slides] == [False, True, False]
	assert slides[1].path.suffix == ".mp4"
	# The clip must cover the scene *and* the cross-dissolve overlap, because
	# unlike a still it cannot simply be held for longer.
	assert made["duration"] == pytest.approx(5.4)


def test_nothing_animates_when_the_flag_is_off(ctx, monkeypatch, tmp_path):
	called = []
	monkeypatch.setattr(
		"pipeline.render.animate.render_clip", lambda *a, **k: called.append(1) or None
	)
	ctx.config.render.animated_callouts = False
	manifest = _callout_manifest()
	RenderStage(ctx)._slides_for(
		manifest, _audio_for(manifest), tmp_path / "s", "seg", animate_ok=True, hold=0.4
	)
	assert not called


def test_nothing_animates_on_the_hard_cut_path(ctx, monkeypatch, tmp_path):
	"""A concat script of images has nowhere to put an mp4, which is the same
	constraint the Ken Burns move has (D33)."""
	called = []
	monkeypatch.setattr(
		"pipeline.render.animate.render_clip", lambda *a, **k: called.append(1) or None
	)
	ctx.config.render.animated_callouts = True
	manifest = _callout_manifest()
	RenderStage(ctx)._slides_for(
		manifest, _audio_for(manifest), tmp_path / "s", "seg", animate_ok=False, hold=0.0
	)
	assert not called


def test_a_callout_without_a_comparison_is_never_animated(ctx, monkeypatch, tmp_path):
	called = []
	monkeypatch.setattr(
		"pipeline.render.animate.render_clip", lambda *a, **k: called.append(1) or None
	)
	ctx.config.render.animated_callouts = True
	manifest = _callout_manifest(with_comparison=False)
	RenderStage(ctx)._slides_for(
		manifest, _audio_for(manifest), tmp_path / "s", "seg", animate_ok=True, hold=0.4
	)
	assert not called


def test_an_animated_slide_never_also_zooms(ctx):
	"""It is already moving; a Ken Burns push on top reads as a wobble."""
	ctx.config.render.motion = True
	stage = RenderStage(ctx)
	assert stage._amplitude("figure", animated=False) > 0
	assert stage._amplitude("figure", animated=True) == 0.0
	assert "zoompan" not in stage._video_chain("figure", 300, animated=True)


def test_render_clip_declines_without_manim(ctx, monkeypatch, tmp_path):
	from pipeline.render import animate
	from pipeline.render.slides import SlideContext

	monkeypatch.setattr(animate, "manim_available", lambda: False)
	visual = _callout_manifest().scenes[1].visual
	# No subprocess should be attempted at all.
	monkeypatch.setattr(animate.subprocess, "run", lambda *a, **k: pytest.fail("manim was invoked"))
	assert animate.render_clip(visual, SlideContext(), 5.0, tmp_path, "x") is None


# --- generated title backdrops: an absent key must change nothing -------------


def test_no_key_means_no_backdrop_and_no_call(monkeypatch, tmp_path):
	"""The OPENAI_API_KEY line has been present but empty for four episodes, so
	"configured" and "set" are different questions (D39)."""
	from pipeline.render import backdrop

	monkeypatch.setenv("OPENAI_API_KEY", "")
	assert backdrop.available() is False
	monkeypatch.setattr(
		backdrop, "generate", backdrop.generate
	)  # unpatched: it must decline on its own
	assert backdrop.generate("anything", tmp_path) is None
	assert not list(tmp_path.glob("*.png"))


def test_a_cached_backdrop_is_not_generated_twice(monkeypatch, tmp_path):
	"""Stage 8 is re-run constantly while tuning and must not bill for it."""
	from pipeline.render import backdrop

	monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
	monkeypatch.setattr(backdrop, "available", lambda: True)
	key = backdrop.prompt_for("A subject")
	import hashlib

	h = hashlib.sha256(f"gpt-image-1|1536x1024|medium|{key}".encode()).hexdigest()[:16]
	(tmp_path / f"{h}.png").write_bytes(b"CACHED")

	def explode(*a, **k):
		pytest.fail("a cached backdrop was regenerated")

	monkeypatch.setattr(backdrop, "generate", backdrop.generate)
	import sys

	sys.modules.pop("openai", None)
	monkeypatch.setitem(sys.modules, "openai", type("m", (), {"OpenAI": explode}))
	assert backdrop.generate("A subject", tmp_path).read_bytes() == b"CACHED"


def test_an_unreadable_backdrop_falls_back_to_the_flat_ground(tmp_path):
	from pipeline.render.slides import BG, SlideContext, backdrop_ground

	bad = tmp_path / "not-an-image.png"
	bad.write_bytes(b"nope")
	ctx = SlideContext(width=320, height=180)
	assert colours(backdrop_ground(ctx, bad)) == {BG}


def test_only_the_title_card_gets_a_backdrop(ctx, tmp_path, monkeypatch):
	"""An image behind a figure would compete with the one thing the format
	exists to show."""
	from pipeline.render import backdrop as backdrop_mod
	from pipeline.render.slides import SlideContext

	asked = []
	monkeypatch.setattr(
		backdrop_mod, "generate", lambda subject, *a, **k: asked.append(subject) or None
	)
	ctx.config.render.title_backdrops = True
	stage = RenderStage(ctx)
	sctx = SlideContext(width=320, height=180, segment_label="Seg")

	from pipeline.schemas import Visual

	assert stage._backdrop_for(Visual(type="figure", title="T"), sctx) is None
	assert stage._backdrop_for(Visual(type="bullet_slide", title="T"), sctx) is None
	stage._backdrop_for(Visual(type="title_card", title="A hook"), sctx)
	assert asked == ["A hook"]


def test_backdrops_off_asks_for_nothing(ctx, monkeypatch):
	from pipeline.render import backdrop as backdrop_mod
	from pipeline.render.slides import SlideContext
	from pipeline.schemas import Visual

	monkeypatch.setattr(
		backdrop_mod, "generate", lambda *a, **k: pytest.fail("generated while switched off")
	)
	ctx.config.render.title_backdrops = False
	assert (
		RenderStage(ctx)._backdrop_for(Visual(type="title_card", title="T"), SlideContext()) is None
	)


# --- staged reveals (D42) ----------------------------------------------------


def _slide_ctx():
	return SlideContext(width=1920, height=1080, eyebrow="TEST", arxiv_id="2609.00001")


@pytest.mark.parametrize(
	"visual,expected",
	[
		({"type": "bullet_slide", "bullets": ["one", "two", "three"]}, [1, 2, 3]),
		({"type": "bullet_slide", "bullets": ["only one"]}, []),
		({"type": "result_callout", "highlight": "42%"}, [0, 1]),
		({"type": "figure", "figure_file": "f1.png", "highlight": "a caption"}, [0, 1]),
		({"type": "figure", "figure_file": "f1.png"}, []),
		({"type": "title_card", "title": "A card"}, []),
		({"type": "transition", "title": "Bridge"}, []),
	],
)
def test_which_slides_have_something_to_stage(visual, expected):
	"""A reveal needs two states. A lone bullet, a captionless figure and the
	cards have one, so they stay stills rather than becoming one-frame clips."""
	from pipeline.render import reveal
	from pipeline.schemas import Visual

	assert reveal.reveal_steps(Visual.model_validate(visual)) == expected


def test_a_callout_with_a_comparison_is_left_to_manim():
	"""Both moving paths would claim this scene; manim goes first, so the reveal
	must decline it or a chart would be rendered as fading text."""
	from pipeline.render import reveal
	from pipeline.schemas import Visual

	visual = Visual.model_validate(
		{
			"type": "result_callout",
			"highlight": "200",
			"comparison": {"template": "count_up", "label_a": "tokens", "value_a": 200},
		}
	)
	assert reveal.reveal_steps(visual) == []


def test_the_default_render_is_still_the_whole_slide():
	"""`reveal=None` is what every caller outside the reveal module passes, so it
	has to be byte-identical to the fully revealed slide."""
	from pipeline.schemas import Visual

	visual = Visual.model_validate(
		{"type": "bullet_slide", "title": "Findings", "bullets": ["one", "two", "three"]}
	)
	default = render_visual(_slide_ctx(), visual)
	full = render_visual(_slide_ctx(), visual, reveal=3)
	assert default.tobytes() == full.tobytes()


def test_a_partial_reveal_does_not_move_what_is_already_drawn():
	"""The reason states are rendered from the finished layout: if the block
	recentred per state the text would slide up the frame instead of adding to
	it. The first row must land on the same pixels in every state."""
	from pipeline.schemas import Visual

	visual = Visual.model_validate(
		{"type": "bullet_slide", "title": "Findings", "bullets": ["one", "two", "three"]}
	)
	one = render_visual(_slide_ctx(), visual, reveal=1)
	three = render_visual(_slide_ctx(), visual, reveal=3)
	# Top third holds the title and the first row in both.
	band = (0, 0, 1920, 420)
	assert one.crop(band).tobytes() == three.crop(band).tobytes()
	assert one.tobytes() != three.tobytes(), "later rows should still be missing"


# --- per-scene voice settings (D42) ------------------------------------------


def _scene(visual_type: str, sid: str = "s1"):
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2609.00001",
			"scenes": [
				{
					"id": sid,
					"narration": "A line.",
					"visual": {"type": visual_type, "title": "t", "highlight": "1"},
					"est_seconds": 4,
				}
			],
		}
	).scenes[0]


def test_a_headline_number_is_read_less_evenly_than_a_caveat(ctx):
	"""D21's monotony: every clip in Ep. 6 used the same four numbers. Lower
	stability is more variation in the read, so the number lifts and the caveat
	settles - and the caveat must not come out livelier than the number."""
	from pipeline.stages.voice import VoiceStage

	ctx.config.tts.voice_settings = {"stability": 0.40, "similarity_boost": 0.75, "style": 0.30}
	stage = VoiceStage(ctx)
	number = stage._scene_settings(_scene("result_callout"), 3, 9)
	caveat = stage._scene_settings(_scene("bullet_slide"), 7, 9)
	assert number["stability"] < caveat["stability"]
	assert number["style"] > caveat["style"]


def test_scene_settings_stay_inside_the_providers_range(ctx):
	from pipeline.stages.voice import VoiceStage

	ctx.config.tts.voice_settings = {"stability": 0.95, "similarity_boost": 0.75, "style": 0.95}
	stage = VoiceStage(ctx)
	for index, vtype in itertools.product(range(9), ["result_callout", "title_card", "figure"]):
		out = stage._scene_settings(_scene(vtype), index, 9)
		assert 0.0 <= out["stability"] <= 1.0
		assert 0.0 <= out["style"] <= 1.0


def test_variation_can_be_switched_off(ctx):
	"""Off means the client's own settings are used, not a computed copy."""
	from pipeline.stages.voice import VoiceStage

	ctx.config.tts.voice_settings = {"stability": 0.40, "style": 0.30}
	ctx.config.tts.vary_by_scene = False
	assert VoiceStage(ctx)._scene_settings(_scene("result_callout"), 0, 9) is None


def test_no_configured_settings_means_none_are_sent(ctx):
	"""An empty `voice_settings` is how a run asks for the voice's own defaults;
	varying from nothing would start sending settings that were never chosen."""
	from pipeline.stages.voice import VoiceStage

	ctx.config.tts.voice_settings = {}
	assert VoiceStage(ctx)._scene_settings(_scene("result_callout"), 0, 9) is None
