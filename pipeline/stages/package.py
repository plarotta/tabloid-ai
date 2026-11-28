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
from ..render.slides import SlideContext
from ..schemas import PackageResult
from ..stage import Stage, StageError
from .script import SERIES_NAME, YOUTUBE_TITLE_MAX

# Words that cannot end a trimmed title without sounding like a dropped call.
_DANGLING = {
	"a",
	"an",
	"and",
	"as",
	"at",
	"by",
	"for",
	"from",
	"in",
	"into",
	"its",
	"of",
	"on",
	"or",
	"the",
	"their",
	"these",
	"this",
	"to",
	"with",
	"without",
}

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

	def _lead_figure(self, script, enriched: dict) -> tuple[Path | None, tuple | None]:
		"""The figure the episode itself featured, and the panel it zoomed into.

		Was `usable_figures[0]`: the paper's first figure, which is usually its
		dense overview page and at feed size reads as grey texture. A figure the
		script chose - and better, a panel it pointed the camera at - is the one
		image the episode already decided was worth looking at.
		"""
		from ..render.camera import caption_for
		from ..render.panels import lettered_panels
		from ..render.thumbs import load_figure

		for seg in script.segments:
			root = self.paths.enriched_dir / seg.arxiv_id.replace("/", "_")
			figs = [
				s.visual for s in seg.scenes if s.visual.type == "figure" and s.visual.figure_file
			]
			figs.sort(key=lambda v: not v.focus)
			for v in figs:
				path = root / v.figure_file
				if not path.exists():
					continue
				if v.focus:
					img = load_figure(path)
					panels = (
						lettered_panels(img, caption_for(root / "figures", v.figure_file))
						if img
						else {}
					)
					if v.focus[0] in panels:
						return path, panels[v.focus[0]].box
				return path, None
		if script.segments:
			paper = enriched.get(script.segments[0].arxiv_id)
			if paper is not None and paper.usable_figures:
				root = self.paths.enriched_dir / paper.arxiv_id.replace("/", "_")
				path = root / paper.usable_figures[0].file
				if path.exists():
					return path, None
		return None, None

	def _concepts(self, script) -> list[dict]:
		"""Thumbnail concepts - text plus a visual metaphor - from the papers (D52).

		Draft, check, revise: the model proposes five; each is checked for length,
		for numbers the digests back (D49) and for meaning (the D50 fact check);
		if fewer than the target survive, the rejected ones go back once with the
		reason, and the replacements face the same checks. Two or three calls on
		the script model, ~2 cents. [] on any failure.
		"""
		from ..llm import MeteredClient
		from ..numbers import Digest
		from ..prompts import load_prompt
		from .extract import ExtractStage
		from .script import digest_to_prompt_text

		want = self.config.package.thumbnail_concepts
		try:
			digests = ExtractStage(self.ctx).load().digests
			wanted = {m.arxiv_id for m in script.segments}
			digests = [d for d in digests if d.arxiv_id in wanted] or digests
			texts = {d.arxiv_id: digest_to_prompt_text(d) for d in digests}
			spec = self.config.model_for(self.config.script.stage_model)
			client = MeteredClient.for_stage(spec, self.tracker, self.name)
			system, user = load_prompt("thumbnail").render(
				title=script.episode.title,
				digests="\n\n".join(f"---\narxiv_id: {k}\n{v}" for k, v in texts.items()),
				existing_options="\n".join(f"- {t}" for t in script.episode.thumbnail_text_options)
				or "(none)",
			)
		except Exception as e:
			log.warning("Thumbnail concepts failed (%s); using the text options", e)
			return []

		backing = Digest("\n".join(texts.values()))
		digest_text = "\n\n".join(texts.values())
		kept, rejected = self._screen(self._ask(client, system, user), backing, digest_text, client)
		if len(kept) < want:
			# Short either way - concepts rejected, or the model returned fewer than
			# asked (it does, at this temperature) - so ask once more.
			feedback = "\n".join(f"- {t!r} was rejected: {why}" for t, why in rejected)
			kept_list = "\n".join(f"- {c['text']!r} (kept)" for c in kept)
			retry = (
				f"{user}\n\nWrite {want - len(kept) + 2} more concepts, different from "
				f"these:\n{kept_list}\n{feedback}\nFix any rejection reason given."
			)
			more, _ = self._screen(self._ask(client, system, retry), backing, digest_text, client)
			kept += more
		return kept[:want]

	def _ask(self, client, system: str, user: str) -> list[dict]:
		from ..llm import parse_json_response

		try:
			resp = client.complete(user, system=system, max_tokens=2048, temperature=0.7)
			payload = parse_json_response(resp.text)
			return payload.get("concepts", []) if isinstance(payload, dict) else []
		except Exception as e:
			log.warning("Thumbnail concept call failed (%s)", e)
			return []

	def _screen(
		self, raw: list[dict], backing, digest_text: str, client
	) -> tuple[list[dict], list[tuple[str, str]]]:
		"""(passing concepts, [(text, why rejected)]) - length, numbers, meaning."""
		from ..numbers import unbacked
		from ..render.thumbs import parse_hook

		passing, rejected = [], []
		for c in raw:
			text, scene = str(c.get("text", "")).strip(), str(c.get("scene", "")).strip()
			if not text or not scene:
				continue
			words = [w for w in parse_hook(text)[1].split() if not any(ch.isdigit() for ch in w)]
			if len(words) > 4:
				rejected.append((text, "more than four words besides the number"))
				continue
			bad = unbacked(text.replace("*", ""), backing)
			if bad:
				rejected.append((text, f"{', '.join(bad)} is not in any paper's digest"))
				continue
			passing.append({"text": text, "scene": scene, "arxiv_id": c.get("arxiv_id", "")})
		flags = self._check_meaning(passing, digest_text, client)
		for text, why in flags:
			rejected.append((text, why))
		passing = [c for c in passing if c["text"] not in {t for t, _ in flags}]
		for text, why in rejected:
			log.warning("Thumbnail concept %r rejected: %s", text, why[:160])
		return passing, rejected

	def _check_meaning(self, concepts: list[dict], digest_text: str, client) -> list[tuple]:
		"""The script's second reader (D50), on the thumbnail text (D52). Returns
		[(text, problem)] for each concept it flags.

		The number check passes "80% lie about work done" - 80 is in the digest -
		but the digest's 80% is of *incomplete* runs. A thumbnail has no narration
		beside it to correct it, so a flagged concept is never used.
		"""
		from ..llm import parse_json_response
		from ..prompts import load_prompt

		if not concepts or not self.config.script.fact_check:
			return []
		try:
			system, user = load_prompt("factcheck").render(
				digest=digest_text,
				scenes="\n".join(
					f"t{i + 1}: (thumbnail text, read with no context) {c['text'].replace('*', '')}"
					for i, c in enumerate(concepts)
				),
			)
			resp = client.complete(user, system=system, max_tokens=1024, temperature=0.0)
			payload = parse_json_response(resp.text)
			flags = payload.get("flags", []) if isinstance(payload, dict) else []
		except Exception as e:
			log.warning("Thumbnail fact check did not run (%s)", e)
			return []
		by_tag = {f"t{i + 1}": c["text"] for i, c in enumerate(concepts)}
		return [
			(by_tag[str(f.get("scene"))], str(f.get("problem", "")))
			for f in flags
			if str(f.get("scene")) in by_tag
		]

	def _thumbnail(
		self, script, enriched: dict, out: Path
	) -> tuple[str | None, list[str], str | None]:
		"""Up to three thumbnails plus a preview sheet at feed sizes.

		Illustrated when it can be (D52): each concept's scene drawn by the image
		model, its text over it. Any concept whose art fails - and every one, when
		there is no key - falls back to the D50 hero layout over the lead figure.
		"""
		from ..render import backdrop
		from ..render.slides import PAPER_ACCENTS
		from ..render.thumbs import THUMB_STYLE, hero, illustrated, preview_sheet

		cfg = self.config.package
		art_dir = self.paths.stage_dir("package") / "_thumbnail_art"
		cache = art_dir / "concepts.json"
		concepts: list[dict] = []
		if cfg.thumbnail_art and backdrop.available():
			# Cached per run, like every image: re-packaging is free, and deleting
			# this file is how to ask for new concepts.
			if cache.exists():
				concepts = read_json(cache)
			else:
				concepts = self._concepts(script)
				if concepts:
					write_json(cache, concepts)
		options = [c["text"] for c in concepts] or [
			t for t in script.episode.thumbnail_text_options if t.strip()
		][:3]
		if not options:
			options = [script.episode.title or "New AI papers"]
		figure, panel = self._lead_figure(script, enriched)
		ctx = SlideContext(width=THUMB_W, height=THUMB_H)

		names, images = [], []
		for i, text in enumerate(options):
			accent = PAPER_ACCENTS[i % len(PAPER_ACCENTS)]
			img = None
			if i < len(concepts):
				art = backdrop.generate_image(
					f"{concepts[i]['scene']} {THUMB_STYLE}",
					art_dir,
					tracker=self.tracker,
					stage=self.name,
					model=self.config.render.backdrop_model,
					quality=cfg.thumbnail_quality,
					label=f"thumbnail: {text}",
				)
				img = illustrated(ctx, text, art, accent=accent) if art else None
			if img is None:
				img = hero(ctx, text, figure, accent=accent, panel=panel)
			name = "thumbnail.png" if i == 0 else f"thumbnail_{i + 1}.png"
			path = out / name
			img.save(path, "PNG")
			if path.stat().st_size > 2 * 1024 * 1024:
				# YouTube's limit. A painterly illustration can exceed it as PNG.
				jpg = path.with_suffix(".jpg")
				img.convert("RGB").save(jpg, "JPEG", quality=92)
				path.unlink()
				name = jpg.name
			names.append(name)
			images.append(img)
		preview = out / "thumbnail_preview.png"
		preview_sheet(images, [n.rsplit(".", 1)[0] for n in names]).save(preview)
		log.info(
			"Thumbnails: %s (%s illustrated); compare them at feed size in %s",
			", ".join(names),
			min(len(concepts), len(names)),
			preview.name,
		)
		return names[0], names[1:], preview.name

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

		number = self._episode_number(out)
		thumb, variants, preview = self._thumbnail(script, enriched, out)
		result = PackageResult(
			generated_at=datetime.now(UTC),
			episode_number=number,
			title=self._numbered_title(script.episode.title, number),
			description=description,
			thumbnail_text_options=list(script.episode.thumbnail_text_options),
			episode_file=episode_name,
			segment_files=copied,
			thumbnail_file=thumb,
			thumbnail_variants=variants,
			thumbnail_preview=preview,
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

	def _episode_number(self, out: Path) -> int:
		"""This run's episode number, stamped once and then kept.

		Re-packaging must not renumber a finished episode, so an existing
		`metadata.json` wins over the live counter. The counter itself is only
		advanced by a run that moved the window marker, which is what stops a
		re-render burning the next episode's number.
		"""
		from ..state import episode_number

		existing = out / "metadata.json"
		if existing.exists():
			try:
				recorded = read_json(existing).get("episode_number")
				if isinstance(recorded, int) and recorded > 0:
					return recorded
			except Exception:  # a corrupt manifest is not worth failing a package over
				pass
		return episode_number()

	def _numbered_title(self, written: str, number: int) -> str:
		"""`ML Papers of the Day Ep. N: <title>`.

		Built here rather than asked of the model, because a series prefix is
		bookkeeping and a model that has to remember which episode this is will
		eventually get it wrong. The number comes from `state.json`, which is also
		where the fetch window lives - both advance on a packaged episode.

		The 70-character YouTube ceiling covers the whole string, so the prompt
		asks for a title that fits what is left. Over the limit it is the written
		half that is trimmed, never the series name: a viewer scanning a sidebar
		reads the prefix first and a truncated episode number is worse than a
		truncated sentence.
		"""
		if not written:
			return ""
		prefix = f"{SERIES_NAME} Ep. {number}: "
		room = YOUTUBE_TITLE_MAX - len(prefix)
		if len(written) > room:
			# Cut back to a word boundary. "...theorems on their\u2026" stops mid-phrase
			# and reads as a bug; "...theorems\u2026" reads as an abbreviation. Falls
			# back to a hard cut only if the first word alone will not fit.
			clipped = written[: max(room - 1, 0)]
			if " " in clipped:
				clipped = clipped[: clipped.rindex(" ")]
			# A trailing preposition or article dangles: "...theorems on\u2026" still
			# reads as a sentence that was cut off, where "...theorems\u2026" reads as
			# a title that was shortened.
			words = clipped.split()
			while len(words) > 1 and words[-1].lower() in _DANGLING:
				words.pop()
			clipped = " ".join(words)
			log.warning(
				"Episode title is %s characters against the %s left by %r; trimmed to "
				"%r. Tighten prompts/episode/, which asks for a shorter one.",
				len(written),
				room,
				prefix,
				clipped,
			)
			written = clipped.rstrip(" ,;:-") + "\u2026"
		return prefix + written
