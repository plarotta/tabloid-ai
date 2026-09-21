"""Reproducible motion preview. All example claims and values are illustrative."""

from __future__ import annotations

import json
import wave
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .config import load_config
from .editorial import pacing_report
from .llm.cost import CostTracker, Pricing
from .paths import RunPaths, write_json
from .render.editorial import EditorialScene
from .render.slides import ACCENT, BASE, SlideContext, bold, regular
from .schemas import EpisodeMetadata, Scene, SceneAudio, SceneManifest, ScriptResult, SegmentAudio
from .stage import StageContext, StageError
from .stages.render import RenderStage
from .stages.script import scenes_to_markdown
from .stages.voice import VoiceStage
from .tts.providers import TTSError, build_tts_client

DISCLOSURE = "STYLE PREVIEW · Illustrative content, not paper results"


def demo_manifest() -> SceneManifest:
	data = [
		(
			"hook",
			4.5,
			"A simulation can look convincing and still predict the wrong thing.",
			{
				"type": "title_card",
				"title": "Looks right. Gets it wrong.",
				"highlight": "Appearance is only half the story.",
			},
		),
		(
			"context",
			6.0,
			"One test asks whether a scene looks real. "
			"Another asks whether its outcomes match reality.",
			{
				"type": "contrast",
				"title": "Two very different tests",
				"bullets": ["Looks plausible", "Predicts correctly"],
				"highlight": "Visual realism does not establish predictive accuracy.",
			},
		),
		(
			"mechanism",
			7.0,
			"Start with the same scene, simulate it repeatedly, then compare "
			"the predicted outcomes with reference observations.",
			{
				"type": "process",
				"title": "Test the outcomes",
				"bullets": ["Observe a scene", "Simulate outcomes", "Compare distributions"],
				"highlight": "One plausible future is not the whole distribution.",
			},
		),
		(
			"evidence",
			6.0,
			"This illustrative experiment keeps the starting conditions fixed, "
			"so differences in the outcomes can be compared.",
			{
				"type": "figure",
				"title": "Same start. Different outcomes.",
				"figure_file": "demo.png",
				"highlight": "Read the distribution, not a single sample.",
			},
		),
		(
			"evidence",
			7.0,
			"In this made-up example, the model has forty units of error, "
			"against ten for the reference samples.",
			{
				"type": "result_callout",
				"title": "The gap becomes visible",
				"highlight": "40 vs 10",
				"comparison": {
					"template": "two_bar",
					"label_a": "Model predictions",
					"value_a": 40,
					"label_b": "Reference samples",
					"value_b": 10,
					"unit": "error units · lower is better",
					"note": "Illustrative values",
				},
			},
		),
		(
			"caveat",
			5.5,
			"The example covers a controlled setting. It cannot establish "
			"reliability across tasks or in deployment.",
			{
				"type": "bullet_slide",
				"title": "Keep the claim in bounds",
				"bullets": [
					"Controlled environment",
					"One evaluation setting",
					"No deployment conclusion",
				],
			},
		),
		(
			"payoff",
			4.5,
			"The useful question is whether a simulated world gets the possibilities right.",
			{
				"type": "result_callout",
				"title": "What changes",
				"highlight": "Test possibilities.\nNot just pixels.",
			},
		),
	]
	return SceneManifest(
		arxiv_id="episode",
		scenes=[
			Scene.model_validate(
				{
					"id": f"s{i + 1}",
					"beat": beat,
					"est_seconds": duration,
					"narration": narration,
					"pause_after": 0.2,
					"visual": {**visual, "source": DISCLOSURE},
				}
			)
			for i, (beat, duration, narration, visual) in enumerate(data)
		],
	)


def _demo_figure(path: Path) -> None:
	"""A deliberately schematic example, with no pretend paper attribution."""
	image = Image.new("RGB", (1600, 520), (250, 250, 248))
	d = ImageDraw.Draw(image)
	d.text((60, 30), "ILLUSTRATIVE EXPERIMENT", fill=(55, 65, 82), font=bold(28))
	for panel, (label, weights, color) in enumerate(
		[
			("Reference outcomes", (2, 6, 10, 6, 2), BASE),
			("Model outcomes", (1, 2, 3, 10, 8), ACCENT),
		]
	):
		x = 60 + panel * 800
		d.text((x, 104), label, fill=(22, 26, 33), font=bold(36))
		for i, value in enumerate(weights):
			left, height = x + i * 120, value * 21
			d.rectangle((left, 413 - height, left + 86, 413), fill=color)
			d.text((left + 28, 434), str(i + 1), fill=(55, 65, 82), font=regular(27))
		d.line((x, 414, x + 620, 414), fill=(120, 125, 130), width=2)
	image.save(path)


def render_demo(output: Path, height: int = 720, narrate: bool = False) -> Path:
	output.mkdir(parents=True, exist_ok=True)
	cfg = load_config()
	cfg.render.width, cfg.render.height = round(height * 16 / 9), height
	cfg.render.visual_style = "editorial"
	paths = RunPaths("preview", runs_dir=output)
	tracker = CostTracker(
		run_id="preview",
		calls_path=paths.calls_jsonl,
		pricing=Pricing.load(),
		ceiling_usd=cfg.budget.ceiling_usd,
		stage_targets=cfg.budget.stage_targets_usd,
	)
	ctx = StageContext(config=cfg, paths=paths, tracker=tracker)
	manifest = demo_manifest()
	figures = paths.enriched_dir / "episode" / "figures"
	figures.mkdir(parents=True, exist_ok=True)
	_demo_figure(figures / "demo.png")
	script = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(title="Editorial style preview", description=DISCLOSURE),
	)
	write_json(paths.stage_dir("script") / "script.json", json.loads(script.model_dump_json()))
	(output / "script.md").write_text(
		scenes_to_markdown(manifest, "Illustrative style preview"), encoding="utf-8"
	)
	if narrate:
		try:
			client = build_tts_client(
				cfg.tts.provider, cfg.tts.model, cfg.tts.voice, cfg.tts.voice_settings
			)
			audio = VoiceStage(ctx)._narrate(client, manifest, "demo")
		except TTSError as e:
			raise StageError(str(e)) from e
	else:
		clips = []
		for scene in manifest.scenes:
			path = paths.stage_dir("voice") / f"{scene.id}.wav"
			with wave.open(str(path), "wb") as wav:
				wav.setnchannels(1)
				wav.setsampwidth(2)
				wav.setframerate(44100)
				wav.writeframes(b"\x00\x00" * round(scene.est_seconds * 44100))
			clips.append(
				SceneAudio(
					scene_id=scene.id,
					audio_file=path.name,
					duration_seconds=scene.est_seconds,
					characters=len(scene.narration),
				)
			)
		audio = SegmentAudio(arxiv_id="episode", scenes=clips)
	built = RenderStage(ctx)._build_segment(manifest, audio, "demo", out_name="editorial-preview")
	if built is None:
		raise StageError("The preview could not be rendered.")
	import shutil

	final = output / "editorial-preview.mp4"
	shutil.copy2(built[0], final)
	storyboard = Image.new("RGB", (1280, 360 * 4), (12, 16, 24))
	for i, scene in enumerate(manifest.scenes):
		poster = EditorialScene(
			scene,
			SlideContext(
				width=640,
				height=360,
				figures_dir=figures,
				scene_index=i,
				scene_total=len(manifest.scenes),
			),
			scene.est_seconds,
		).poster()
		storyboard.paste(poster, ((i % 2) * 640, (i // 2) * 360))
	storyboard.save(output / "storyboard.png")
	write_json(output / "pacing.json", pacing_report(script))
	tracker.write_report(paths.cost_report_json)
	return final
