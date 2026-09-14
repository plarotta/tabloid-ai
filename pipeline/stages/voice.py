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
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..paths import read_json, write_json
from ..schemas import SceneAudio, SceneManifest, SegmentAudio, VoiceResult
from ..stage import Stage, StageError
from ..tts.providers import TTSError, build_tts_client, measure_duration

log = logging.getLogger(__name__)


def append_silence(path: Path, seconds: float, reported: float) -> float:
	"""Pad a clip with trailing silence. Returns the duration to record.

	The pause lives here, in the audio file, rather than in the render timeline,
	because Stage 8 builds both of its tracks from whatever Stage 7 says a clip
	measures. Padding the file means the gap flows through the timeline, the
	chapter marks and the runtime with no further arithmetic anywhere.

	Doing it after synthesis rather than asking the engine for it also makes the
	pacing deterministic: ElevenLabs honours `<break>` only above roughly half a
	second, and not identically across models.

	`reported` is what the provider measured before padding. It is what comes
	back if padding fails, so a missing or unhappy ffmpeg costs the pause and
	nothing else - re-measuring an unmodified file would only risk turning a
	cosmetic failure into a stage failure.
	"""
	if seconds <= 0:
		return reported
	# Encode by extension rather than forcing one codec: the local engine writes
	# AIFF and the hosted ones write MP3.
	padded = path.with_name(f"{path.stem}__padded{path.suffix}")
	try:
		subprocess.run(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel",
				"error",
				"-y",
				"-i",
				str(path),
				"-af",
				f"apad=pad_dur={seconds:.3f}",
				str(padded),
			],
			capture_output=True,
			text=True,
			timeout=120,
			check=True,
		)
		padded.replace(path)
	except (subprocess.SubprocessError, OSError) as e:
		log.warning("Could not pad %s (%s); using it unpadded", path.name, e)
		padded.unlink(missing_ok=True)
		return reported
	return measure_duration(path)


class VoiceStage(Stage):
	name = "voice"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("voice") / "voice.json").exists()

	def load(self) -> VoiceResult:
		return VoiceResult.model_validate(read_json(self.paths.stage_dir("voice") / "voice.json"))

	def _scene_settings(self, scene, index: int, total: int) -> dict | None:
		"""Nudge the voice settings for what this scene is doing.

		D21 named the monotony and its two levers; this is the Stage 7 one. Every
		clip in Ep. 6 was synthesized with the same four numbers, so a headline
		number and an honest caveat were read identically across five minutes.

		The moves are small on purpose. `stability` down is more variation in the
		read, so it goes down where the line should lift - the opening, a number -
		and up where it should settle, on the caveat the segment turns on. Large
		swings here do not read as expression, they read as a different person.
		"""
		base = self.config.tts.voice_settings
		if not base or not self.config.tts.vary_by_scene:
			return None

		def clamp(x: float) -> float:
			return round(max(0.0, min(1.0, x)), 3)

		stability = base.get("stability", 0.4)
		style = base.get("style", 0.3)
		vtype = getattr(scene.visual, "type", "")

		if index == 0 or vtype == "title_card":
			stability, style = stability - 0.05, style + 0.10
		elif vtype == "result_callout":
			stability, style = stability - 0.10, style + 0.15
		elif total >= 3 and index == total - 2:
			# The caveat scene, by the script prompt's own structure rule.
			stability, style = stability + 0.15, style - 0.10
		else:
			return dict(base)
		return {**base, "stability": clamp(stability), "style": clamp(style)}

	def _narrate(
		self, client, manifest: SceneManifest, label: str, gap: float | None = None
	) -> SegmentAudio:
		out_dir = self.paths.stage_dir("voice") / label
		scenes: list[SceneAudio] = []

		total = len(manifest.scenes)
		for index, scene in enumerate(manifest.scenes):
			text = scene.narration.strip()
			if not text:
				log.warning("%s/%s: empty narration; skipping", label, scene.id)
				continue
			result = client.synthesize(
				text,
				out_dir / scene.id,
				voice=self.config.tts.voice or None,
				settings=self._scene_settings(scene, index, total),
			)
			# A beat between scenes. The slide changes here too, so the pause is
			# a visual rest as much as an audible one. Bridges pass a longer one:
			# a signpost line followed by silence is the whole seam now (D35).
			duration = result.duration_seconds
			pause = self.config.tts.scene_gap_seconds if gap is None else gap
			if pause > 0 and shutil.which("ffmpeg"):
				duration = append_silence(result.audio_path, pause, duration)
			elif pause > 0:
				log.warning("ffmpeg not found; scene gaps are disabled for this run")

			rel = result.audio_path.relative_to(self.paths.stage_dir("voice"))
			scenes.append(
				SceneAudio(
					scene_id=scene.id,
					audio_file=str(rel),
					duration_seconds=duration,
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
			client = build_tts_client(cfg.provider, cfg.model, cfg.voice, cfg.voice_settings)
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
			# Transitions are part of it: they belong to the episode cut, never to
			# the standalone segment.
			cold = outro = None
			bridges: list[SegmentAudio] = []
			if not self.ctx.paper_filter:
				if script.episode.cold_open:
					cold = self._narrate(client, script.episode.cold_open, "cold_open")
				total = len(script.episode.transitions)
				for i, t in enumerate(script.episode.transitions):
					label = f"transition_{t.into_arxiv_id.replace('/', '_')}"
					audio = self._narrate(
						client, t.manifest(i, total), label, self.config.tts.bridge_pause_seconds
					)
					# `manifest()` reports "episode"; re-key to the paper it leads
					# into so Stages 8 and 9 can place it.
					bridges.append(SegmentAudio(arxiv_id=t.into_arxiv_id, scenes=audio.scenes))
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
			transitions=bridges,
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
			"Narrated %s scenes across %s segment(s) plus %s bridge(s); episode audio "
			"%.0fs (%.1f min)",
			len(all_scenes),
			len(rendered),
			len(bridges),
			result.duration_seconds,
			result.duration_seconds / 60,
		)
		return result
