"""Stage 9 - assemble the upload-ready bundle.

Everything a human needs to publish the episode ends up in one directory, so
publishing is a file-picker operation rather than a hunt through `runs/`:

    output/episode.mp4          the stitched episode
    output/segment_<id>.mp4     each paper, publishable standalone
    output/thumbnail.png        generated from the template + the best figure
    output/metadata.json        title, description, chapter timestamps
    output/cost_report.json     what the run spent

Chapter timestamps are computed from the **measured** clip durations, the same
source Stage 8 used to build the timeline, so the chapters land on the cuts.

No image model is involved (spec Stage 9): the thumbnail is composed from the
same Pillow primitives as the slides.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..paths import read_json, write_json
from ..render.slides import SlideContext, thumbnail
from ..schemas import PackageResult
from ..stage import Stage, StageError

log = logging.getLogger(__name__)

# YouTube's thumbnail guidance: 1280x720, under 2MB.
THUMB_W, THUMB_H = 1280, 720


def timestamp(seconds: float) -> str:
	"""`H:MM:SS` when the episode runs past an hour, else `M:SS` - the format
	YouTube parses into clickable chapters."""
	total = int(seconds)
	h, rem = divmod(total, 3600)
	m, s = divmod(rem, 60)
	return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapter_marks(voice, titles: dict[str, str]) -> list[dict]:
	"""Chapter offsets, walking the episode in the order Stage 8 stitched it.

	Bridges are part of that order, so they have to be counted here too - miss
	them and every chapter after the first lands late by the length of the
	bridges before it.

	A bridge introduces the paper it names, so it opens that paper's chapter
	rather than closing the previous one: clicking a chapter should land on the
	sentence that sets the paper up.

	The seam cross-fade shifts nothing. Each part is padded by exactly what the
	dissolve consumes, so these measured offsets stay true of the finished file.
	"""
	bridges = {t.arxiv_id: t.duration_seconds for t in voice.transitions}
	chapters: list[dict] = []
	elapsed = 0.0

	if voice.cold_open is not None:
		chapters.append({"time": timestamp(0), "seconds": 0.0, "label": "Intro"})
		elapsed += voice.cold_open.duration_seconds

	for seg in voice.segments:
		chapters.append(
			{
				"time": timestamp(elapsed),
				"seconds": round(elapsed, 3),
				"label": titles.get(seg.arxiv_id, seg.arxiv_id),
				"arxiv_id": seg.arxiv_id,
			}
		)
		elapsed += bridges.get(seg.arxiv_id, 0.0) + seg.duration_seconds

	if voice.outro is not None:
		chapters.append(
			{"time": timestamp(elapsed), "seconds": round(elapsed, 3), "label": "Outro"}
		)

	# YouTube only renders chapters when the first one starts at 0:00.
	if chapters and chapters[0]["seconds"] != 0.0:
		log.warning("First chapter is not at 0:00; YouTube will ignore the chapter list")
	return chapters


class PackageStage(Stage):
	name = "package"

	def is_complete(self) -> bool:
		return (self.paths.output_dir / "metadata.json").exists()

	def load(self) -> PackageResult:
		return PackageResult.model_validate(read_json(self.paths.output_dir / "metadata.json"))

	def _thumbnail(self, script, enriched: dict, out: Path) -> str | None:
		"""Template + the top-ranked paper's best figure."""
		text = ""
		if script.episode.thumbnail_text_options:
			# The model is asked for three; the first is its own preferred pick.
			text = script.episode.thumbnail_text_options[0]
		text = text or script.episode.title or "New AI papers"

		figure = None
		if script.segments:
			paper = enriched.get(script.segments[0].arxiv_id)
			if paper is not None:
				usable = paper.usable_figures
				if usable:
					candidate = (
						self.paths.enriched_dir / paper.arxiv_id.replace("/", "_") / usable[0].file
					)
					if candidate.exists():
						figure = candidate

		ctx = SlideContext(width=THUMB_W, height=THUMB_H)
		img = thumbnail(ctx, text, figure, kicker="arXiv this week")
		path = out / "thumbnail.png"
		img.save(path, "PNG")

		size_kb = path.stat().st_size / 1024
		if size_kb > 2048:
			log.warning("Thumbnail is %.0f KB, over YouTube's 2 MB limit", size_kb)
		log.info("Thumbnail: %s (%.0f KB)%s", path.name, size_kb, "" if figure else " [no figure]")
		return path.name

	def run(self) -> PackageResult:
		from .enrich import EnrichStage
		from .render import RenderStage
		from .script import ScriptStage
		from .voice import VoiceStage

		script = ScriptStage(self.ctx).load()
		voice = VoiceStage(self.ctx).load()
		render = RenderStage(self.ctx).load()
		enriched = {p.arxiv_id: p for p in EnrichStage(self.ctx).load().papers}

		out = self.paths.output_dir
		out.mkdir(parents=True, exist_ok=True)
		render_dir = self.paths.stage_dir("render")

		# --- videos ----------------------------------------------------------
		copied: list[str] = []
		episode_name = None
		if render.episode_file:
			src = render_dir / render.episode_file
			if src.exists():
				shutil.copy2(src, out / src.name)
				episode_name = src.name
		if episode_name is None:
			raise StageError(
				"No stitched episode to package. Run the render stage for a whole "
				"episode (without --paper) first."
			)
		for seg in render.segments:
			src = render_dir / seg.video_file
			if src.exists():
				shutil.copy2(src, out / src.name)
				copied.append(src.name)

		# --- chapters, from measured durations --------------------------------
		titles = {a: p.paper.title for a, p in enriched.items()}
		chapters = chapter_marks(voice, titles)
		elapsed = voice.duration_seconds

		# --- description, with the chapter list appended ----------------------
		description = script.episode.description or ""
		if chapters:
			lines = "\n".join(f"{c['time']} {c['label']}" for c in chapters)
			description = f"{description}\n\nChapters\n{lines}".strip()

		# --- cost -------------------------------------------------------------
		report = self.tracker.write_report(self.paths.cost_report_json)
		shutil.copy2(self.paths.cost_report_json, out / "cost_report.json")

		result = PackageResult(
			generated_at=datetime.now(UTC),
			title=script.episode.title,
			description=description,
			thumbnail_text_options=list(script.episode.thumbnail_text_options),
			episode_file=episode_name,
			segment_files=copied,
			thumbnail_file=self._thumbnail(script, enriched, out),
			duration_seconds=round(elapsed, 3),
			chapters=chapters,
			total_cost_usd=report.total_usd,
		)
		write_json(out / "metadata.json", json.loads(result.model_dump_json()))

		log.info(
			"Packaged %s + %s segment(s), %.1f min, %s chapters -> %s",
			episode_name,
			len(copied),
			elapsed / 60,
			len(chapters),
			out,
		)
		if not result.title:
			log.warning("No episode title; the wrapper prompt returned nothing usable")
		return result
