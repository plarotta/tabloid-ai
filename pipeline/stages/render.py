"""Stage 8 - compose slides and mux them against the narration.

Engine choice (spec DECIDE, recorded in DECISIONS.md D20): Pillow for slide
composition plus ffmpeg invoked directly, rather than Remotion or moviepy.

The assembly method is what keeps audio and video in sync. Both tracks are built
from the *same* list of measured durations:

  - the audio track is a concat of the per-scene clips, in order
  - the video track is a concat-demuxer list where each slide is held for exactly
    that scene's measured duration

Because both derive from one source of truth, drift cannot accumulate across a
segment the way it does when a still is held for an estimated time.

`ffmpeg` is required and its absence is reported as a clear StageError rather
than a traceback.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..paths import REPO_ROOT, read_json, write_json
from ..render.slides import BG as SLIDE_BG
from ..render.slides import SlideContext, render_visual
from ..schemas import RenderedSegment, RenderResult, SceneManifest, SegmentAudio
from ..stage import Stage, StageError

log = logging.getLogger(__name__)


def have_ffmpeg() -> bool:
	return shutil.which("ffmpeg") is not None


def run_ffmpeg(args: list[str], what: str) -> None:
	"""Invoke ffmpeg, surfacing its stderr on failure.

	ffmpeg reports the actual reason on stderr and exits non-zero; without this
	the caller sees only a return code.
	"""
	cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
	try:
		subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=True)
	except subprocess.CalledProcessError as e:
		raise StageError(f"ffmpeg failed while {what}: {e.stderr.strip()[:600]}") from e
	except subprocess.SubprocessError as e:
		raise StageError(f"ffmpeg failed while {what}: {e}") from e


def concat_list(paths: list[Path], durations: list[float] | None = None) -> str:
	"""A concat-demuxer script. With durations it becomes a slideshow timeline.

	The last entry is repeated without a duration, which the demuxer requires for
	the final image to be held rather than dropped.
	"""
	lines = []
	for i, p in enumerate(paths):
		lines.append(f"file '{p.as_posix()}'")
		if durations:
			lines.append(f"duration {durations[i]:.4f}")
	if durations and paths:
		lines.append(f"file '{paths[-1].as_posix()}'")
	return "\n".join(lines) + "\n"


class RenderStage(Stage):
	name = "render"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("render") / "render.json").exists()

	def load(self) -> RenderResult:
		return RenderResult.model_validate(
			read_json(self.paths.stage_dir("render") / "render.json")
		)

	def _slides_for(
		self,
		manifest: SceneManifest,
		audio: SegmentAudio,
		out_dir: Path,
		label: str,
		eyebrow: str = "",
		paper_index: int | None = None,
	) -> list[Path]:
		"""One PNG per narrated scene, in narration order."""
		cfg = self.config.render
		by_id = {s.id: s for s in manifest.scenes}
		figures_dir = self.paths.enriched_dir / manifest.arxiv_id.replace("/", "_") / "figures"
		ctx = SlideContext(
			width=cfg.width,
			height=cfg.height,
			arxiv_id=manifest.arxiv_id if manifest.arxiv_id != "episode" else "",
			figures_dir=figures_dir if figures_dir.exists() else None,
			segment_label=label,
			eyebrow=eyebrow,
			scene_total=len(audio.scenes),
			paper_index=paper_index,
		)

		out_dir.mkdir(parents=True, exist_ok=True)
		paths = []
		for i, clip in enumerate(audio.scenes):
			scene = by_id.get(clip.scene_id)
			if scene is None:
				log.warning("%s: audio for unknown scene %s; skipping", label, clip.scene_id)
				continue
			ctx.scene_index = i  # drives the progress bar
			img = render_visual(ctx, scene.visual, scene.id)
			path = out_dir / f"{i:03d}_{scene.id}.png"
			img.save(path, "PNG")
			paths.append(path)
		return paths

	def _xfade_filter(self, n: int, durations: list[float], fade: float) -> tuple[str, str]:
		"""Chain n inputs with cross-dissolves. Returns (filtergraph, out_label).

		Consumes streams labelled `[v0]..[vn-1]`, each *fade* seconds longer than
		the duration it carries. Every xfade eats exactly that overlap, so the
		result still matches the narration length. Offsets are cumulative on the
		un-padded durations for the same reason.

		Used at two scales: between the slides of one segment, and between the
		parts of the episode. The arithmetic is identical; only what is padded
		differs (see `_build_segment` and `_stitch_episode`).
		"""
		# xfade output length is `offset + len(second input)`, so with each clip
		# padded to d+fade the offset must be the *cumulative* elapsed time minus
		# one fade. Accumulating `d - fade` per step instead subtracts the fade
		# once per transition and silently shortens the segment - which truncated
		# the last two seconds of narration before this was fixed.
		parts, prev, cum = [], "v0", 0.0
		for i in range(1, n):
			cum += durations[i - 1]
			offset = cum - fade
			out = f"x{i}"
			parts.append(
				f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:"
				f"offset={offset:.3f}[{out}]"
			)
			prev = out
		return ";".join(parts), prev

	def _music_bed(self, work: Path, seconds: float, db: float) -> Path | None:
		"""The background music bed, looped/trimmed to `seconds` and levelled.

		Prefers a real track at `assets/music/bed.*` and falls back to a
		synthesised pad. The spec asks for a royalty-free track shipped in-repo;
		none could be sourced here, and a synthesised sine drone is honest about
		licensing but sounds like a hum once it is loud enough to notice - which
		is why `render.background_music` defaults to false. Drop a licensed track
		at that path and flip the flag.
		"""
		track = None
		assets = REPO_ROOT / "assets" / "music"
		if assets.is_dir():
			for cand in sorted(assets.iterdir()):
				if cand.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}:
					track = cand
					break

		if track is not None:
			out = work / "bed.m4a"
			fade = min(2.5, max(seconds / 8, 0.5))
			try:
				run_ffmpeg(
					[
						"-stream_loop",
						"-1",
						"-i",
						str(track),
						"-t",
						f"{seconds:.3f}",
						"-af",
						f"afade=t=in:st=0:d={fade:.2f},"
						f"afade=t=out:st={max(seconds - fade, 0):.2f}:d={fade:.2f},"
						f"volume={db}dB",
						"-c:a",
						"aac",
						"-b:a",
						"128k",
						str(out),
					],
					f"preparing the music bed from {track.name}",
				)
				log.info("Music bed: %s at %.0f dB", track.name, db)
				return out if out.exists() else None
			except StageError as e:
				log.warning("Could not use %s (%s); falling back to a generated pad", track.name, e)

		out = work / "bed.m4a"
		# A low triad through a steep lowpass, faded at both ends. No modulation:
		# ffmpeg's tremolo bottoms out at 0.1 Hz, and anything audible enough to
		# notice reads as a wobble rather than as ambience.
		fade = min(2.5, max(seconds / 8, 0.5))
		graph = (
			"sine=frequency=110:sample_rate=44100[a];"
			"sine=frequency=164.81:sample_rate=44100[b];"
			"sine=frequency=220:sample_rate=44100[c];"
			"[a][b][c]amix=inputs=3:duration=longest,"
			"lowpass=f=300,"
			f"afade=t=in:st=0:d={fade:.2f},"
			f"afade=t=out:st={max(seconds - fade, 0):.2f}:d={fade:.2f},"
			f"volume={db}dB[out]"
		)
		try:
			run_ffmpeg(
				[
					"-f",
					"lavfi",
					"-i",
					f"anullsrc=r=44100:cl=stereo:d={seconds:.3f}",
					"-filter_complex",
					graph,
					"-map",
					"[out]",
					"-t",
					f"{seconds:.3f}",
					"-c:a",
					"aac",
					"-b:a",
					"128k",
					str(out),
				],
				"generating the music bed",
			)
		except StageError as e:
			log.warning("Music bed unavailable (%s); continuing without it", e)
			return None
		return out if out.exists() else None

	def _build_segment(
		self,
		manifest: SceneManifest,
		audio: SegmentAudio,
		label: str,
		eyebrow: str = "",
		out_name: str = "",
		paper_index: int | None = None,
	) -> tuple[Path, float] | None:
		"""Render one part of the episode to mp4. Returns (path, duration).

		"Segment" in the loose sense: a paper segment, the cold open, the outro or
		a bridge. They differ only in what is on the slides.
		"""
		cfg = self.config.render
		out_root = self.paths.stage_dir("render")
		work = out_root / "_work" / label
		work.mkdir(parents=True, exist_ok=True)
		voice_root = self.paths.stage_dir("voice")

		clips = [c for c in audio.scenes if (voice_root / c.audio_file).exists()]
		if not clips:
			log.error("%s: no audio files found; cannot render", label)
			return None

		slides = self._slides_for(manifest, audio, work / "slides", label, eyebrow, paper_index)
		if not slides:
			# Every scene id in the audio was absent from the manifest, so there is
			# nothing to show. ffmpeg would fail on an empty input list.
			log.error("%s: no slide matched a narrated scene; cannot render", label)
			return None
		if len(slides) != len(clips):
			log.warning(
				"%s: %s slides for %s audio clips; rendering the common prefix",
				label,
				len(slides),
				len(clips),
			)
			n = min(len(slides), len(clips))
			slides, clips = slides[:n], clips[:n]
		durations = [c.duration_seconds for c in clips]

		# 1. Audio track: concat the per-scene clips in order.
		audio_list = work / "audio.txt"
		audio_list.write_text(
			concat_list([voice_root / c.audio_file for c in clips]), encoding="utf-8"
		)
		audio_track = work / "narration.m4a"
		run_ffmpeg(
			[
				"-f",
				"concat",
				"-safe",
				"0",
				"-i",
				str(audio_list),
				"-c:a",
				"aac",
				"-b:a",
				"192k",
				str(audio_track),
			],
			f"building the {label} audio track",
		)

		total = sum(durations)

		# 2. Optional ambient bed, mixed under the narration.
		final_audio = audio_track
		if cfg.background_music:
			bed = self._music_bed(work, total, cfg.music_db)
			if bed is not None:
				mixed = work / "mixed.m4a"
				run_ffmpeg(
					[
						"-i",
						str(audio_track),
						"-i",
						str(bed),
						"-filter_complex",
						"[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
						"-map",
						"[a]",
						"-c:a",
						"aac",
						"-b:a",
						"192k",
						str(mixed),
					],
					f"mixing the {label} music bed",
				)
				final_audio = mixed

		# 3. Video track, synced to the measured durations.
		out = out_root / f"{out_name or f'segment_{label}'}.mp4"
		# Letterbox bars must match the slide ground, or a non-16:9 render frames
		# every slide in a colour the visual system never uses.
		ground = "".join(f"{c:02x}" for c in SLIDE_BG)
		scale = (
			f"scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=decrease,"
			f"pad={cfg.width}:{cfg.height}:(ow-iw)/2:(oh-ih)/2:color=0x{ground},"
			f"fps={cfg.fps},format=yuv420p"
		)
		fade = cfg.crossfade_seconds

		if fade > 0 and len(slides) > 1 and min(durations) > fade * 2:
			# Cross-dissolve. Each still is held for its scene *plus* the fade, and
			# every xfade consumes exactly that overlap, so runtime is preserved.
			args: list[str] = []
			for path, dur in zip(slides, durations, strict=True):
				args += ["-loop", "1", "-t", f"{dur + fade:.4f}", "-i", str(path)]
			args += ["-i", str(final_audio)]
			chain, last = self._xfade_filter(len(slides), durations, fade)
			pre = ";".join(f"[{i}:v]{scale}[v{i}]" for i in range(len(slides)))
			graph = f"{pre};{chain}" if chain else pre
			run_ffmpeg(
				[
					*args,
					"-filter_complex",
					graph,
					"-map",
					f"[{last}]",
					"-map",
					f"{len(slides)}:a",
					"-c:v",
					"libx264",
					"-preset",
					"medium",
					"-crf",
					"20",
					"-pix_fmt",
					"yuv420p",
					"-r",
					str(cfg.fps),
					"-c:a",
					"aac",
					"-b:a",
					"192k",
					"-shortest",
					"-movflags",
					"+faststart",
					str(out),
				],
				f"cross-fading the {label} segment",
			)
		else:
			# Hard cuts: the concat demuxer holds each slide for its own duration.
			video_list = work / "video.txt"
			video_list.write_text(concat_list(slides, durations), encoding="utf-8")
			run_ffmpeg(
				[
					"-f",
					"concat",
					"-safe",
					"0",
					"-i",
					str(video_list),
					"-i",
					str(final_audio),
					"-c:v",
					"libx264",
					"-preset",
					"medium",
					"-crf",
					"20",
					"-pix_fmt",
					"yuv420p",
					"-r",
					str(cfg.fps),
					"-vf",
					scale,
					"-c:a",
					"copy",
					"-shortest",
					"-movflags",
					"+faststart",
					str(out),
				],
				f"muxing the {label} segment",
			)
		return out, total

	def _stitch_episode(self, parts: list[tuple[Path, float]], out: Path, work: Path) -> None:
		"""Join the episode's parts, cross-dissolving the seams between them.

		Concatenating the parts is a stream copy and free, but it puts a hard cut
		at every join: the cold open's last frame is replaced by the next frame in
		a single frame's time. Dissolving them needs a filtergraph, and a
		filtergraph rules out the copy - so this re-encodes. That is the whole
		cost of the feature, and `episode_crossfade_seconds: 0` opts out of it.

		Sync is the part that is easy to get wrong. The audio here is a plain
		concat, so nothing may move on the audio timeline; the video therefore has
		to reach each seam at exactly the un-faded elapsed time. Padding every
		part except the first with `fade` seconds of its own frozen *first* frame
		buys back precisely what the xfade consumes, so part i's real content
		still begins at `sum(durations[:i])`. Padding the *end* instead - the
		obvious move, and what the within-segment path does because its inputs are
		stills - would slide every part `fade` seconds early against its own
		narration.

		The trailing pad is slack, not timing: a part's encoded length can land a
		frame short of the measured audio it was built from, and a frozen frame is
		a better way to cover that than whatever ffmpeg does when an xfade offset
		lands past the end of its input. `-shortest` trims it back off.
		"""
		cfg = self.config.render
		fade = cfg.episode_crossfade_seconds
		durations = [d for _, d in parts]

		if fade > 0 and len(parts) > 1 and min(durations) > fade * 2:
			args: list[str] = []
			for path, _ in parts:
				args += ["-i", str(path)]
			pre = []
			for i in range(len(parts)):
				pad = f"stop_mode=clone:stop_duration={fade:.3f}"
				if i:
					pad = f"start_mode=clone:start_duration={fade:.3f}:{pad}"
				pre.append(f"[{i}:v]tpad={pad},fps={cfg.fps},format=yuv420p,setsar=1[v{i}]")
			chain, last = self._xfade_filter(len(parts), durations, fade)
			mixed = "".join(f"[{i}:a]" for i in range(len(parts)))
			graph = ";".join([*pre, chain, f"{mixed}concat=n={len(parts)}:v=0:a=1[a]"])
			try:
				run_ffmpeg(
					[
						*args,
						"-filter_complex",
						graph,
						"-map",
						f"[{last}]",
						"-map",
						"[a]",
						"-c:v",
						"libx264",
						"-preset",
						"medium",
						"-crf",
						"20",
						"-pix_fmt",
						"yuv420p",
						"-r",
						str(cfg.fps),
						"-c:a",
						"aac",
						"-b:a",
						"192k",
						"-shortest",
						"-movflags",
						"+faststart",
						str(out),
					],
					"cross-fading the episode seams",
				)
				log.info("Episode seams: %s cross-dissolve(s) at %.2fs", len(parts) - 1, fade)
				return
			except StageError as e:
				# A hard-cut episode beats no episode. The parts themselves are
				# already on disk and unaffected.
				log.warning("Seam cross-fade failed (%s); falling back to hard cuts", e)
		elif fade > 0 and len(parts) > 1:
			log.warning(
				"A part is shorter than %.2fs of cross-fade; stitching with hard cuts",
				fade * 2,
			)

		listing = work / "episode.txt"
		listing.parent.mkdir(parents=True, exist_ok=True)
		listing.write_text(concat_list([p for p, _ in parts]), encoding="utf-8")
		# Stream copy: every part came from this pipeline with identical codec
		# settings, which is exactly when concat can avoid a re-encode.
		run_ffmpeg(
			[
				"-f",
				"concat",
				"-safe",
				"0",
				"-i",
				str(listing),
				"-c",
				"copy",
				"-movflags",
				"+faststart",
				str(out),
			],
			"stitching the episode",
		)

	def run(self) -> RenderResult:
		from .script import ScriptStage
		from .voice import VoiceStage

		if not have_ffmpeg():
			raise StageError(
				"ffmpeg is not installed and Stage 8 cannot render without it. "
				"Install it (`brew install ffmpeg`) and re-run."
			)

		script = ScriptStage(self.ctx).load()
		voice = VoiceStage(self.ctx).load()
		manifests = {m.arxiv_id: m for m in script.segments}
		# Paper titles drive the eyebrow, so a viewer joining mid-segment knows
		# which paper they are looking at.
		titles: dict[str, str] = {}
		enrich_path = self.paths.enriched_dir / "enrich.json"
		if enrich_path.exists():
			from ..schemas import EnrichResult

			for p in EnrichResult.model_validate(read_json(enrich_path)).papers:
				titles[p.arxiv_id] = p.paper.title
		targets = list(voice.segments)
		if self.ctx.paper_filter:
			targets = [s for s in targets if s.arxiv_id == self.ctx.paper_filter]
			if not targets:
				raise StageError(f"Paper {self.ctx.paper_filter!r} has no narrated segment.")

		cfg = self.config.render
		out_root = self.paths.stage_dir("render")
		log.info(
			"Rendering %s segment(s) at %sx%s %sfps",
			len(targets),
			cfg.width,
			cfg.height,
			cfg.fps,
		)

		# The wrapper is only rendered for a whole episode, not a --paper run.
		# Bridges belong to it too: a standalone segment must not open mid-thought.
		wrapper: dict[str, tuple] = {}
		bridges: dict[str, tuple] = {}
		# Running order drives the per-paper accent, so a segment and the bridge
		# that introduces it wear the same colour. A --paper rebuild still needs
		# the same index the full episode would have given it, or the standalone
		# file comes out a different colour from the one inside the episode.
		order = {s.arxiv_id: i for i, s in enumerate(voice.segments)}
		if not self.ctx.paper_filter:
			for name, audio in (("cold_open", voice.cold_open), ("outro", voice.outro)):
				manifest = getattr(script.episode, name, None)
				if audio is None or manifest is None:
					continue
				built = self._build_segment(manifest, audio, name)
				if built is not None:
					wrapper[name] = built
					log.info("  %s -> %s (%.0fs)", name, built[0].name, built[1])

			narrated = {b.arxiv_id: b for b in voice.transitions}
			total = len(script.episode.transitions)
			for i, t in enumerate(script.episode.transitions):
				audio = narrated.get(t.into_arxiv_id)
				if audio is None:
					log.warning(
						"Transition into %s was scripted but not narrated; that seam "
						"stays a hard cut",
						t.into_arxiv_id,
					)
					continue
				slug = t.into_arxiv_id.replace("/", "_")
				built = self._build_segment(
					t.manifest(i, total),
					audio,
					f"transition_{slug}",
					out_name=f"bridge_{slug}",
					paper_index=order.get(t.into_arxiv_id),
				)
				if built is not None:
					bridges[t.into_arxiv_id] = built
					log.info("  bridge -> %s (%.0fs)", built[0].name, built[1])

		rendered: list[RenderedSegment] = []
		for seg in targets:
			manifest = manifests.get(seg.arxiv_id)
			if manifest is None:
				log.warning("%s: narrated but has no script manifest; skipping", seg.arxiv_id)
				continue
			built = self._build_segment(
				manifest,
				seg,
				seg.arxiv_id,
				titles.get(seg.arxiv_id, ""),
				paper_index=order.get(seg.arxiv_id),
			)
			if built is None:
				continue
			path, dur = built
			rendered.append(
				RenderedSegment(
					arxiv_id=seg.arxiv_id,
					video_file=path.name,
					duration_seconds=dur,
					scenes=len(seg.scenes),
				)
			)
			log.info("  %s -> %s (%.0fs)", seg.arxiv_id, path.name, dur)

		if not rendered:
			raise StageError("No segment could be rendered.")

		# Stitch cold-open -> bridge -> seg1 -> bridge -> seg2 ... -> outro
		# (spec Stage 8, plus the bridges added for the seam review).
		episode_file = None
		episode_seconds = 0.0
		if not self.ctx.paper_filter:
			order: list[tuple[Path, float]] = []
			if "cold_open" in wrapper:
				order.append(wrapper["cold_open"])
			for r in rendered:
				# The bridge into a paper plays before it, so it reads as that
				# paper's opening rather than the previous one's tail.
				if r.arxiv_id in bridges:
					order.append(bridges[r.arxiv_id])
				order.append((out_root / r.video_file, r.duration_seconds))
			if "outro" in wrapper:
				order.append(wrapper["outro"])

			if len(order) > 1:
				episode = out_root / "episode.mp4"
				self._stitch_episode(order, episode, out_root / "_work")
				episode_file = episode.name
				episode_seconds = sum(d for _, d in order)
				log.info(
					"Episode: %s (%.0fs, %.1f min) from %s parts",
					episode.name,
					episode_seconds,
					episode_seconds / 60,
					len(order),
				)

		result = RenderResult(
			generated_at=datetime.now(UTC),
			segments=rendered,
			episode_file=episode_file,
			duration_seconds=episode_seconds or sum(r.duration_seconds for r in rendered),
			width=cfg.width,
			height=cfg.height,
			fps=cfg.fps,
		)
		write_json(
			self.paths.stage_dir("render") / "render.json", json.loads(result.model_dump_json())
		)
		log.info(
			"Rendered %s segment(s), %.0fs total",
			len(rendered),
			result.duration_seconds,
		)
		return result
