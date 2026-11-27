"""D50: the figure camera, chapter colour, hero thumbnails, and the script's second reader."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

from pipeline.llm import MeteredClient
from pipeline.llm.base import LLMClient, LLMResponse
from pipeline.numbers import Digest, unbacked
from pipeline.render import camera
from pipeline.render.panels import Panel, caption_letters, find_panels, fit_to, lettered_panels
from pipeline.render.slides import SlideContext
from pipeline.render.thumbs import hero, parse_hook, preview_sheet
from pipeline.schemas import SceneManifest, Visual


def _figure(cols: int = 3, rows: int = 1, w: int = 900, h: int = 300) -> Image.Image:
	"""White page, `cols x rows` inked panels separated by clear gutters."""
	img = Image.new("RGB", (w, h), "white")
	d = ImageDraw.Draw(img)
	pw, ph = w // cols, h // rows
	for r in range(rows):
		for c in range(cols):
			x0, y0 = c * pw + 20, r * ph + 20
			d.rectangle([x0, y0, x0 + pw - 40, y0 + ph - 40], outline="black", width=3)
			d.line([x0 + 10, y0 + ph - 60, x0 + pw - 60, y0 + 30], fill=(200, 40, 40), width=4)
	return img


# --- panels ------------------------------------------------------------------


def test_caption_letters_need_a_run_from_a():
	assert caption_letters("(a) setup. (b) results. (c) ablation.") == ["a", "b", "c"]
	assert caption_letters("Results (b) only") == []
	assert caption_letters("thought (T), action (A)") == []  # one letter is not a set


def test_three_side_by_side_panels_are_found_in_reading_order():
	panels = find_panels(_figure(3))
	assert len(panels) == 3
	assert [p.box[0] for p in panels] == sorted(p.box[0] for p in panels)


def test_a_figure_is_lettered_only_when_caption_and_detection_agree():
	fig = _figure(3)
	assert sorted(lettered_panels(fig, "(a) x (b) y (c) z")) == ["a", "b", "c"]
	assert lettered_panels(fig, "(a) x (b) y") == {}, "3 panels, 2 letters: no unique merge"


def test_an_ambiguous_merge_is_refused_rather_than_guessed():
	"""A 2x2 grid that should be three panels merges four ways; only one is right."""
	grid = [
		Panel((0, 0, 100, 100)),
		Panel((110, 0, 210, 100)),
		Panel((0, 110, 100, 210)),
		Panel((110, 110, 210, 210)),
	]
	assert fit_to(grid, 3, (210, 210)) is None
	assert fit_to(grid, 4, (210, 210)) is not None


# --- camera ------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_tour_measures_exactly_its_duration(tmp_path):
	path = tmp_path / "fig.png"
	_figure(3).save(path)
	ctx = SlideContext(width=640, height=360, arxiv_id="x", paper_index=1)
	out = camera.render_tour(
		ctx,
		path,
		"(a) one (b) two (c) three",
		["b"],
		"Figure 1",
		"",
		"",
		3.0,
		tmp_path / "tour.mp4",
		fps=10,
	)
	assert out is not None
	frames = subprocess.run(
		[
			"ffprobe",
			"-v",
			"error",
			"-count_frames",
			"-select_streams",
			"v:0",
			"-show_entries",
			"stream=nb_read_frames",
			"-of",
			"csv=p=0",
			str(out),
		],
		capture_output=True,
		text=True,
		check=True,
	).stdout.strip()
	assert int(frames) == 30


def test_an_unmatched_focus_falls_back_to_the_still(tmp_path):
	path = tmp_path / "fig.png"
	_figure(3).save(path)
	ctx = SlideContext(width=640, height=360)
	assert (
		camera.render_tour(ctx, path, "(a) (b) (c)", ["e"], "F", "", "", 3.0, tmp_path / "t.mp4")
		is None
	)


def test_focus_written_as_null_or_a_string_is_accepted():
	assert Visual.model_validate({"type": "figure", "focus": None}).focus == []
	assert Visual.model_validate({"type": "figure", "focus": "b"}).focus == ["b"]


# --- thumbnails --------------------------------------------------------------


def test_a_hook_splits_into_its_number_words_and_highlight():
	assert parse_hook("80% *never* read the files") == ("80%", "never read the files", {0})
	assert parse_hook("Agents *lie* about work") == ("", "Agents lie about work", {1})
	assert parse_hook("1.8x more missed bugs")[0] == "1.8x"


def test_the_hero_thumbnail_never_shows_the_asterisks_and_fills_the_frame(tmp_path):
	ctx = SlideContext(width=1280, height=720)
	fig = tmp_path / "f.png"
	_figure(2).save(fig)
	img = hero(ctx, "80% *never* read the files", fig, accent=(247, 126, 156))
	assert img.size == (1280, 720)
	assert (247, 126, 156) in {c for _, c in img.getcolors(1 << 20)}
	sheet = preview_sheet([img, img], ["a", "b"])
	assert sheet.width > 700 and sheet.height > 400


# --- script: offered focus, thumbnails, fact check ---------------------------


def test_a_thumbnail_with_an_unbacked_number_is_dropped(ctx, monkeypatch):
	import pipeline.stages.script as script_mod

	text = "Agents failed to read all files in 68% of runs; 80% of those were misleading."
	monkeypatch.setattr(script_mod, "digest_to_prompt_text", lambda d: text)
	kept = script_mod.ScriptStage(ctx)._check_thumbnails(
		["80% *never* read", "92% *lie*", "Agents *fake* work"], [object()]
	)
	assert kept == ["80% *never* read", "Agents *fake* work"]


def test_half_is_held_to_the_digest():
	d = Digest("It read 65 of 100 files. 68% of runs were incomplete.")
	assert unbacked("it never opened half of them", d, spoken=True) == ["50%"]
	assert unbacked("two thirds were incomplete", d, spoken=True) == []


class _Checker(LLMClient):
	provider = "anthropic"

	def __init__(self, reply):
		super().__init__("claude-sonnet-4-5-20250929", api_key="x")
		self.reply = reply

	def complete(self, prompt, system=None, max_tokens=4096, temperature=0.0):
		return LLMResponse(
			text=self.reply,
			model=self.model,
			provider=self.provider,
			input_tokens=10,
			output_tokens=10,
		)


def _manifest():
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2609.00001",
			"scenes": [
				{
					"id": "s1",
					"narration": "It read sixty-five files.",
					"est_seconds": 3,
					"visual": {"type": "title_card", "title": "x"},
				},
				{
					"id": "s2",
					"narration": "It skimmed thirty-five.",
					"est_seconds": 3,
					"visual": {"type": "title_card", "title": "y"},
				},
			],
		}
	)


def test_the_fact_check_flags_and_never_rewrites(ctx):
	from pipeline.stages.script import ScriptStage, scenes_to_markdown

	reply = json.dumps(
		{
			"flags": [
				{
					"scene": "s2",
					"quote": "skimmed thirty-five",
					"problem": "it read sixty-five and skipped thirty-five",
				}
			]
		}
	)
	client = MeteredClient(_Checker(reply), ctx.tracker, "script")
	out = ScriptStage(ctx)._fact_check(_manifest(), "digest", client)
	assert out.scenes[0].fact_flags == []
	assert out.scenes[1].fact_flags and "skipped" in out.scenes[1].fact_flags[0]
	assert out.scenes[1].narration == "It skimmed thirty-five.", "flagged, never rewritten"
	assert "fact check" in scenes_to_markdown(out)


def test_a_broken_fact_check_costs_the_check_not_the_segment(ctx):
	from pipeline.stages.script import ScriptStage

	client = MeteredClient(_Checker("not json at all"), ctx.tracker, "script")
	out = ScriptStage(ctx)._fact_check(_manifest(), "digest", client)
	assert [s.fact_flags for s in out.scenes] == [[], []]


# --- voice -------------------------------------------------------------------


def test_the_story_beat_steers_the_read(ctx):
	from pipeline.stages.voice import VoiceStage

	ctx.config.tts.voice_settings = {"stability": 0.4, "style": 0.3}
	ctx.config.tts.vary_by_scene = True
	stage = VoiceStage(ctx)
	m = _manifest()
	m.scenes[1].beat = "caveat"
	m.scenes[0].beat = "hook"
	hook = stage._scene_settings(m.scenes[0], 3, 10)
	caveat = stage._scene_settings(m.scenes[1], 4, 10)
	assert hook["stability"] < 0.4 < caveat["stability"]


def test_two_segments_opening_on_the_same_title_are_flagged():
	from pipeline.stages.script import _flag_repeated_titles

	a, b = _manifest(), _manifest()
	b.arxiv_id = "2609.00002"
	_flag_repeated_titles([a, b])
	assert a.scenes[0].fact_flags == []
	assert "repeats 2609.00001" in b.scenes[0].fact_flags[0]


def test_a_callout_number_lands_after_a_beat_not_half_the_scene():
	from pipeline.render.reveal import LEAD_IN, _holds

	callout = Visual.model_validate({"type": "result_callout", "highlight": "80%"})
	assert _holds(callout, [0, 1], 8.0) == [LEAD_IN, 8.0 - LEAD_IN]
	bullets = Visual.model_validate({"type": "bullet_slide", "bullets": ["a", "b"]})
	assert _holds(bullets, [1, 2], 8.0) == [4.0, 4.0]


def test_a_chapter_card_never_asks_for_a_backdrop(ctx, monkeypatch):
	from pipeline.render import backdrop
	from pipeline.stages.render import RenderStage

	monkeypatch.setattr(backdrop, "generate", lambda *a, **k: pytest.fail("billed for a backdrop"))
	v = Visual.model_validate({"type": "title_card", "title": "A hook"})
	assert RenderStage(ctx)._backdrop_for(v, SlideContext(paper_index=0)) is None


def test_the_opener_is_a_title_sequence_of_exact_length(tmp_path):
	if shutil.which("ffmpeg") is None:
		pytest.skip("needs ffmpeg")
	from pipeline.render.opener import render_opener

	art = tmp_path / "art.png"
	Image.new("RGB", (300, 200), (40, 60, 120)).save(art)
	out = render_opener(
		SlideContext(width=320, height=180),
		"ML Papers of the Day",
		"Three new papers",
		2.0,
		tmp_path / "o.mp4",
		fps=10,
		art=art,
	)
	assert out is not None
	frames = subprocess.run(
		[
			"ffprobe",
			"-v",
			"error",
			"-count_frames",
			"-select_streams",
			"v:0",
			"-show_entries",
			"stream=nb_read_frames",
			"-of",
			"csv=p=0",
			str(out),
		],
		capture_output=True,
		text=True,
		check=True,
	).stdout.strip()
	assert int(frames) == 20
	assert (
		render_opener(SlideContext(), "x", "", 2.0, tmp_path / "n.mp4", art=tmp_path / "no.png")
		is None
	)
