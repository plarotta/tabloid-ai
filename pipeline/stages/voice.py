"""Stage 7 - narrate every scene and measure what came out.

The whole point of this stage, beyond producing audio, is the **measured**
duration of each clip. Stage 8 builds its timeline from those numbers; the
script's `est_seconds` is only ever a pacing aid (spec Stage 7).

Drift between estimate and reality is logged, because it is the feedback signal
for tuning the script prompt's words-per-minute budget.

Synthesis runs one scene at a time by default. The local engine is CPU-bound and
already fast; a hosted engine would be rate-limited, and neither benefits much
from concurrency at this scale (~25 scenes per episode).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..paths import read_json, write_json
from ..schemas import SceneAudio, SceneManifest, SegmentAudio, VoiceResult
from ..stage import Stage, StageError
from ..tts.providers import TTSError, build_tts_client

log = logging.getLogger(__name__)


class VoiceStage(Stage):
	name = "voice"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("voice") / "voice.json").exists()

	def load(self) -> VoiceResult:
		return VoiceResult.model_validate(read_json(self.paths.stage_dir("voice") / "voice.json"))

	def _narrate(self, client, manifest: SceneManifest, label: str) -> SegmentAudio:
		out_dir = self.paths.stage_dir("voice") / label
		scenes: list[SceneAudio] = []

		for scene in manifest.scenes:
			text = scene.narration.strip()
			if not text:
				log.warning("%s/%s: empty narration; skipping", label, scene.id)
				continue
			result = client.synthesize(
				text, out_dir / scene.id, voice=self.config.tts.voice or None
			)
			rel = result.audio_path.relative_to(self.paths.stage_dir("voice"))
			scenes.append(
				SceneAudio(
					scene_id=scene.id,
					audio_file=str(rel),
					duration_seconds=result.duration_seconds,
					characters=result.characters,
					est_seconds=scene.est_seconds,
				)
			)
			# Audio is billed per character by hosted engines; the local one is
			# free but still recorded so the report shows the stage ran.
			self.tracker.record(
				stage=self.name,
				provider=result.provider,
				model=result.model,
				input_units=result.characters,
				output_units=0,
				kind="audio",
			)

		if not scenes:
			raise StageError(f"{label}: every scene had empty narration.")
		return SegmentAudio(arxiv_id=manifest.arxiv_id, scenes=scenes)

	def run(self) -> VoiceResult:
		from .script import ScriptStage

		script = ScriptStage(self.ctx).load()
		segments = list(script.segments)
		if self.ctx.paper_filter:
			segments = [s for s in segments if s.arxiv_id == self.ctx.paper_filter]
			if not segments:
				raise StageError(f"Paper {self.ctx.paper_filter!r} has no script segment.")

		cfg = self.config.tts
		try:
			client = build_tts_client(cfg.provider, cfg.model, cfg.voice)
		except TTSError as e:
			raise StageError(str(e)) from e

		log.info(
			"Narrating %s segment(s) with %s/%s%s",
			len(segments),
			cfg.provider,
			cfg.model or "default",
			f" voice={cfg.voice}" if cfg.voice else "",
		)

		try:
			rendered = [self._narrate(client, m, m.arxiv_id) for m in segments]
			# The wrapper is only narrated for a full episode, not a --paper run.
			cold = outro = None
			if not self.ctx.paper_filter:
				if script.episode.cold_open:
					cold = self._narrate(client, script.episode.cold_open, "cold_open")
				if script.episode.outro:
					outro = self._narrate(client, script.episode.outro, "outro")
		except TTSError as e:
			raise StageError(f"Narration failed: {e}") from e

		result = VoiceResult(
			generated_at=datetime.now(UTC),
			provider=cfg.provider or "",
			model=cfg.model or "",
			voice=cfg.voice,
			segments=rendered,
			cold_open=cold,
			outro=outro,
		)
		write_json(
			self.paths.stage_dir("voice") / "voice.json", json.loads(result.model_dump_json())
		)

		# Drift is the feedback signal for the script prompt's pacing rules.
		all_scenes = [s for seg in rendered for s in seg.scenes]
		est = sum(s.est_seconds for s in all_scenes)
		real = sum(s.duration_seconds for s in all_scenes)
		if est > 0:
			log.info(
				"Scripted estimate %.0fs vs measured %.0fs (%+.0f%%). Stage 8 uses the "
				"measured figures.",
				est,
				real,
				(real / est - 1) * 100,
			)
		log.info(
			"Narrated %s scenes across %s segment(s); episode audio %.0fs (%.1f min)",
			len(all_scenes),
			len(rendered),
			result.duration_seconds,
			result.duration_seconds / 60,
		)
		return result
