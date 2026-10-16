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
from .schemas import (
	EpisodeMetadata,
	Scene,
	SceneAudio,
	SceneManifest,
	ScriptResult,
	SegmentAudio,
	VoiceResult,
)
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


def continuity_manifest() -> SceneManifest:
	"""A toy memory task demonstrates continuity, not a research result."""
	beats = [
		(
			"hook",
			"Where did BLUE go?",
			"Remember BLUE. Recall it after a delay.",
			"A toy recall task needs information to survive a delay.",
			"input",
			False,
		),
		(
			"mechanism",
			"Keep the word available",
			"Memory stores BLUE across the delay.",
			"The memory component retains information after the input disappears.",
			"memory",
			False,
		),
		(
			"evidence",
			"Same task, memory intact",
			"Recall reads BLUE from memory.",
			"An intact memory supplies the stored word to recall.",
			"recall",
			False,
		),
		(
			"evidence",
			"Remove just the memory",
			"No stored word remains to recall.",
			"Removing the only storage in this toy system makes recall impossible.",
			"",
			True,
		),
		(
			"caveat",
			"A toy example has limits",
			"A schematic is not experimental evidence.",
			"Real research claims require reported experiments and their conditions.",
			"",
			False,
		),
		(
			"payoff",
			"The delay needs memory",
			"BLUE survives only while its state survives.",
			"The opening failure is explained by loss of the stored state.",
			"memory",
			False,
		),
	]
	scenes = []
	for i, (beat, title, highlight, teaches, focus, removed) in enumerate(beats):
		visual = {
			"type": "process",
			"title": title,
			"highlight": highlight,
			"source": DISCLOSURE,
			"bullets": ["Input", "Memory", "Recall"],
			"diagram": {
				"id": "recall-task",
				"nodes": [
					{
						"id": key,
						"label": label,
						"state": "removed"
						if removed and key == "memory"
						else ("focus" if key == focus else "normal"),
					}
					for key, label in [
						("input", "Input"),
						("memory", "Memory"),
						("recall", "Recall"),
					]
				],
			},
		}
		if beat == "caveat":
			visual = {
				"type": "contrast",
				"title": title,
				"highlight": highlight,
				"source": DISCLOSURE,
				"bullets": ["Toy mechanism", "Measured evidence"],
			}
		scenes.append(
			Scene.model_validate(
				{
					"id": f"c{i + 1}",
					"beat": beat,
					"teaches": teaches,
					"narration": teaches,
					"est_seconds": 5.5,
					"pause_after": 0.2,
					"visual": visual,
				}
			)
		)
	for i, scene in enumerate(scenes):
		if diagram := scene.visual.diagram:
			values = ["BLUE", "BLUE" if i != 3 else "EMPTY", "BLUE" if i >= 2 and i != 3 else "—"]
			if i == 3:
				values[-1] = "UNAVAILABLE"
			for node, value in zip(diagram.nodes, values, strict=True):
				node.value = value
			# Unique phrases are resolved only when the TTS provider returns alignment.
			diagram.nodes[-1].reveal_phrase = scene.narration.split(".")[0]
	return SceneManifest(arxiv_id="episode", scenes=scenes)


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


def render_demo(
	output: Path,
	height: int = 720,
	narrate: bool = False,
	continuity: bool = False,
	manifest: SceneManifest | None = None,
	reuse_audio: bool = False,
) -> Path:
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
	manifest = manifest or (continuity_manifest() if continuity else demo_manifest())
	figures = paths.enriched_dir / manifest.arxiv_id / "figures"
	figures.mkdir(parents=True, exist_ok=True)
	_demo_figure(figures / "demo.png")
	script = ScriptResult(
		generated_at=datetime.now(UTC),
		segments=[manifest],
		episode=EpisodeMetadata(
			title="Editorial style preview"
			if manifest.arxiv_id == "episode"
			else "Research segment review",
			description=DISCLOSURE
			if manifest.arxiv_id == "episode"
			else "Sourced research review; see SOURCES.md.",
		),
	)
	cached_audio = None
	if reuse_audio:
		if narrate:
			raise StageError("Choose either new narration or cached audio.")
		try:
			old = ScriptResult.model_validate_json(
				(paths.stage_dir("script") / "script.json").read_text()
			)
			old_voice = VoiceResult.model_validate_json(
				(paths.stage_dir("voice") / "voice.json").read_text()
			)
			prior = old.segments[0]
			if prior.arxiv_id != manifest.arxiv_id or [
				(s.id, s.narration) for s in prior.scenes
			] != [(s.id, s.narration) for s in manifest.scenes]:
				raise ValueError("Narration differs from the cached script")
			cached_audio = old_voice.segments[0]
			if cached_audio.arxiv_id != manifest.arxiv_id or any(
				not (paths.stage_dir("voice") / s.audio_file).is_file() for s in cached_audio.scenes
			):
				raise ValueError("Cached audio is missing or belongs to another paper")
		except (OSError, ValueError, IndexError) as e:
			raise StageError(f"Cannot reuse preview audio: {e}") from e
	write_json(paths.stage_dir("script") / "script.json", json.loads(script.model_dump_json()))
	(output / "script.md").write_text(
		scenes_to_markdown(
			manifest,
			"Illustrative style preview"
			if manifest.arxiv_id == "episode"
			else "Research segment review",
		),
		encoding="utf-8",
	)
	if cached_audio is not None:
		audio = cached_audio
	elif narrate:
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
		audio = SegmentAudio(arxiv_id=manifest.arxiv_id, scenes=clips)
	voice = VoiceResult(
		generated_at=datetime.now(UTC),
		provider=old_voice.provider if reuse_audio else (cfg.tts.provider if narrate else "silent"),
		model=old_voice.model if reuse_audio else (cfg.tts.model if narrate else "preview"),
		segments=[audio],
	)
	write_json(paths.stage_dir("voice") / "voice.json", json.loads(voice.model_dump_json()))
	built = RenderStage(ctx)._build_segment(manifest, audio, "demo", out_name="editorial-preview")
	if built is None:
		raise StageError("The preview could not be rendered.")
	import shutil

	final = output / "editorial-preview.mp4"
	shutil.copy2(built[0], final)
	storyboard = Image.new("RGB", (1280, 360 * ((len(manifest.scenes) + 1) // 2)), (12, 16, 24))
	phone = Image.new("RGB", (390, 220 * len(manifest.scenes)), (12, 16, 24))
	for i, scene in enumerate(manifest.scenes):
		poster = EditorialScene(
			scene,
			SlideContext(
				width=640,
				height=360,
				figures_dir=figures,
				arxiv_id=manifest.arxiv_id if manifest.arxiv_id != "episode" else "",
				scene_index=i,
				scene_total=len(manifest.scenes),
			),
			scene.est_seconds,
		).poster()
		storyboard.paste(poster, ((i % 2) * 640, (i // 2) * 360))
		phone.paste(poster.resize((390, 220), Image.Resampling.LANCZOS), (0, i * 220))
	storyboard.save(output / "storyboard.png")
	phone.save(output / "phone-review.png")
	write_json(output / "pacing.json", pacing_report(script, voice))
	tracker.write_report(paths.cost_report_json)
	return final
