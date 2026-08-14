"""Stage 6 - turn each PaperDigest into a SceneManifest, then wrap the episode.

One call per paper plus one for the episode wrapper: segments are independent,
and writing them separately keeps each prompt focused on a single paper's digest
rather than asking one call to hold three papers in its head.

Two things are enforced here rather than merely requested in the prompt:

  - `figure_file` must name a figure Stage 3 produced. An invented path renders
    as a missing image, so it is downgraded to a bullet slide instead.
  - Narration is checked for LaTeX and markdown leakage, which a TTS voice would
    read aloud literally.
  - A transition must lead into a paper this episode actually scripted, and there
    is at most one per paper. The wrapper call returns the title and description
    too, so transitions are validated individually and a bad one is dropped on
    its own rather than failing the whole wrapper.

Alongside the JSON, this stage writes a markdown script per segment, because a
human reviewing pacing and wording should not have to read JSON (spec Phase 3).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..llm import LLMError, MeteredClient, parse_json_response
from ..paths import read_json, write_json
from ..prompts import load_prompt
from ..schemas import (
	EnrichedPaper,
	EpisodeMetadata,
	PaperDigest,
	SceneManifest,
	ScriptResult,
	Transition,
)
from ..stage import Stage, StageError

log = logging.getLogger(__name__)

# Things a TTS voice would read aloud as literal noise.
_LEAKAGE = re.compile(r"\$|\\[a-zA-Z]+|`|\*\*|#{1,6}\s|\[[^\]]*\]\([^)]*\)")

YOUTUBE_TITLE_MAX = 70


def digest_to_prompt_text(d: PaperDigest) -> str:
	"""Flatten a digest into the block the script prompt reads."""
	lines = [
		f"one_sentence_claim: {d.one_sentence_claim}",
		f"problem_context: {d.problem_context}",
		f"method_summary: {d.method_summary}",
		"headline_results:",
	]
	lines += [
		f"  - {r.statement} [source: {r.source}]"
		+ (f" [numbers: {r.numbers}]" if r.numbers else "")
		for r in d.headline_results
	]
	if d.key_figures:
		lines.append("key_figures:")
		lines += [f"  - {k.file}: {k.why_show or k.caption}" for k in d.key_figures]
	if d.honest_caveats:
		lines.append("honest_caveats:")
		lines += [f"  - {c}" for c in d.honest_caveats]
	lines.append(f"why_it_matters: {d.why_it_matters}")
	return "\n".join(lines)


def format_figures(paper: EnrichedPaper | None) -> str:
	if paper is None or not paper.usable_figures:
		return "(none available - do not use the figure visual type)"
	return "\n".join(
		f"- file: {f.file}\n  caption: {f.caption or '(no caption)'}" for f in paper.usable_figures
	)


def scenes_to_markdown(manifest: SceneManifest, title: str = "") -> str:
	"""A reviewable script. Pacing and wording are judged by reading this."""
	words = sum(len(s.narration.split()) for s in manifest.scenes)
	out = [
		f"# {title or manifest.arxiv_id}",
		"",
		f"`{manifest.arxiv_id}` · {len(manifest.scenes)} scenes · "
		f"~{manifest.est_seconds:.0f}s · {words} words",
		"",
	]
	for s in manifest.scenes:
		v = s.visual
		bits = [f"**{v.type}**"]
		if v.title:
			bits.append(f"title: {v.title}")
		if v.figure_file:
			bits.append(f"figure: `{v.figure_file}`")
		if v.highlight:
			bits.append(f"highlight: {v.highlight}")
		out += [
			f"### {s.id} · {s.est_seconds:.0f}s",
			"",
			f"> {s.narration}",
			"",
			" · ".join(bits),
		]
		if v.bullets:
			out += [""] + [f"  - {b}" for b in v.bullets]
		out.append("")
	return "\n".join(out)


class ScriptStage(Stage):
	name = "script"

	def is_complete(self) -> bool:
		return (self.paths.stage_dir("script") / "script.json").exists()

	def load(self) -> ScriptResult:
		return ScriptResult.model_validate(
			read_json(self.paths.stage_dir("script") / "script.json")
		)

	# --- validation ----------------------------------------------------------

	def _clean_manifest(
		self, manifest: SceneManifest, paper: EnrichedPaper | None
	) -> SceneManifest:
		"""Enforce what the prompt asks for: real figures, speakable narration."""
		valid = {f.file for f in (paper.usable_figures if paper else []) if f.file}

		for scene in manifest.scenes:
			v = scene.visual
			if v.figure_file and v.figure_file not in valid:
				log.warning(
					"%s/%s: invented figure %r; downgrading to a bullet slide",
					manifest.arxiv_id,
					scene.id,
					v.figure_file,
				)
				v.figure_file = None
				if v.type == "figure":
					v.type = "bullet_slide"
			if v.type == "figure" and not v.figure_file:
				v.type = "bullet_slide"

			if _LEAKAGE.search(scene.narration):
				log.warning(
					"%s/%s: narration contains LaTeX/markdown a TTS voice would read aloud: %r",
					manifest.arxiv_id,
					scene.id,
					scene.narration[:80],
				)
		return manifest

	def _write_segment(
		self, client: MeteredClient, prompt, digest: PaperDigest, paper: EnrichedPaper | None
	) -> SceneManifest | None:
		cfg = self.config.script
		spec = self.config.model_for(cfg.stage_model)
		target_words = int(cfg.target_segment_seconds / 60 * cfg.words_per_minute)
		system, user = prompt.render(
			arxiv_id=digest.arxiv_id,
			title=paper.paper.title if paper else digest.arxiv_id,
			figures=format_figures(paper),
			digest=digest_to_prompt_text(digest),
			wpm=cfg.words_per_minute,
			target_seconds=cfg.target_segment_seconds,
			target_words=target_words,
			max_scenes=cfg.max_scenes_per_segment,
			words_per_scene=int(target_words / cfg.max_scenes_per_segment),
		)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
			payload = parse_json_response(resp.text)
			if not isinstance(payload, dict):
				raise LLMError(f"expected an object, got {type(payload).__name__}")
			payload["arxiv_id"] = digest.arxiv_id
			manifest = SceneManifest.model_validate(payload)
		except Exception as e:
			log.error("%s: scripting failed (%s)", digest.arxiv_id, e)
			return None

		if not manifest.scenes:
			log.error("%s: script contained no scenes", digest.arxiv_id)
			return None

		# The segment must stand alone, so it opens with its own title card.
		if manifest.scenes[0].visual.type != "title_card":
			log.warning(
				"%s: segment does not open with a title card; it cannot be published "
				"standalone as-is",
				digest.arxiv_id,
			)
		return self._enforce_budget(self._clean_manifest(manifest, paper), target_words)

	def _enforce_budget(self, manifest: SceneManifest, target_words: int) -> SceneManifest:
		"""Hold the segment to its scene cap, keeping the shape of the argument.

		Trimming from the end would take the close, which is the one scene the
		segment must not lose (D26 note 4). Trimming from the front would take the
		title card the standalone cut needs. So the head is kept up to the cap and
		the **last two** scenes - caveat, then close - are always preserved.

		Words are only reported, never cut: there is no safe way to shorten a
		sentence here, and a segment that runs long is a prompt problem.
		"""
		cfg = self.config.script
		cap = cfg.max_scenes_per_segment
		scenes = manifest.scenes

		if len(scenes) > cap:
			kept = scenes[: max(cap - 2, 1)] + scenes[-2:]
			log.warning(
				"%s: %s scenes over the cap of %s; dropping %s from the middle and "
				"keeping the caveat and close",
				manifest.arxiv_id,
				len(scenes),
				cap,
				len(scenes) - len(kept),
			)
			manifest.scenes = kept

		words = sum(len(s.narration.split()) for s in manifest.scenes)
		if target_words and words > target_words * (1 + cfg.word_budget_tolerance):
			log.error(
				"%s: %s words against a %s budget (%+.0f%%) - the segment will run "
				"long. Tighten prompts/script/, not this code.",
				manifest.arxiv_id,
				words,
				target_words,
				(words / target_words - 1) * 100,
			)
		return manifest

	def _clean_transitions(self, raw: object, order: list[str]) -> list[Transition]:
		"""Keep at most one transition per paper, in play order.

		Validated one at a time rather than as part of `EpisodeMetadata`, because
		the title and description come back from the same call: a single
		malformed bridge line should cost that bridge and nothing else.

		A paper with no transition keeps the hard cut it has today, so a partial
		result is still an improvement over dropping the lot.
		"""
		if not isinstance(raw, list):
			log.warning("Episode wrapper returned no transitions; every seam stays a hard cut")
			return []

		by_id: dict[str, Transition] = {}
		for item in raw:
			try:
				t = Transition.model_validate(item)
			except Exception as e:
				log.warning("Dropping an unparseable transition (%s)", e)
				continue
			if t.into_arxiv_id not in order:
				log.warning(
					"Transition leads into %r, which is not in this episode; dropping",
					t.into_arxiv_id,
				)
				continue
			if t.into_arxiv_id in by_id:
				log.warning("Second transition into %s; keeping the first", t.into_arxiv_id)
				continue
			if _LEAKAGE.search(t.narration):
				log.warning(
					"Transition into %s contains LaTeX/markdown a TTS voice would read aloud: %r",
					t.into_arxiv_id,
					t.narration[:80],
				)
			by_id[t.into_arxiv_id] = t

		missing = [a for a in order if a not in by_id]
		if missing:
			log.warning("No transition into %s; that seam stays a hard cut", ", ".join(missing))
		return [by_id[a] for a in order if a in by_id]

	def _write_episode(
		self, client: MeteredClient, digests: list[PaperDigest], enriched: dict, ranking
	) -> EpisodeMetadata:
		cfg = self.config.script
		spec = self.config.model_for(cfg.stage_model)
		prompt = load_prompt("episode")
		just = {r.arxiv_id: r.justification for r in ranking.finalists + ranking.substitutes}

		blocks = []
		for d in digests:
			paper = enriched.get(d.arxiv_id)
			blocks.append(
				f"---\narxiv_id: {d.arxiv_id}\n"
				f"title: {paper.paper.title if paper else ''}\n"
				f"claim: {d.one_sentence_claim}\n"
				f"why_it_matters: {d.why_it_matters}\n"
				f"justification: {just.get(d.arxiv_id, '')}"
			)

		cold_words = int(cfg.cold_open_seconds / 60 * cfg.words_per_minute)
		system, user = prompt.render(
			n_papers=len(digests),
			papers="\n".join(blocks),
			cold_open_seconds=cfg.cold_open_seconds,
			cold_open_words=cold_words,
			transition_seconds=cfg.transition_seconds,
			transition_words=int(cfg.transition_seconds / 60 * cfg.words_per_minute),
			wpm=cfg.words_per_minute,
		)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
			payload = parse_json_response(resp.text)
			if not isinstance(payload, dict):
				raise LLMError(f"expected an object, got {type(payload).__name__}")
			# Held back from the model_validate so one bad bridge cannot take the
			# title and description down with it.
			raw_transitions = payload.pop("transitions", None)
			episode = EpisodeMetadata.model_validate(payload)
			episode.transitions = self._clean_transitions(
				raw_transitions, [d.arxiv_id for d in digests]
			)
		except Exception as e:
			# The wrapper is not worth failing an episode over - the segments are
			# the substance, and a title can be written by hand.
			log.error("Episode wrapper failed (%s); segments are unaffected", e)
			return EpisodeMetadata(title="", description="", thumbnail_text_options=[])

		if len(episode.title) > YOUTUBE_TITLE_MAX:
			log.warning(
				"Episode title is %s chars, over the %s limit: %r",
				len(episode.title),
				YOUTUBE_TITLE_MAX,
				episode.title,
			)
		for extra in (episode.cold_open, episode.outro):
			if extra is not None:
				self._clean_manifest(extra, None)
		return episode

	# --- stage ---------------------------------------------------------------

	def run(self) -> ScriptResult:
		from .enrich import EnrichStage
		from .extract import ExtractStage
		from .rank import RankStage

		digests = ExtractStage(self.ctx).load().digests
		if not digests:
			raise StageError("No digests to script; run the extract stage first.")
		enriched = {p.arxiv_id: p for p in EnrichStage(self.ctx).load().papers}
		ranking = RankStage(self.ctx).load()

		if self.ctx.paper_filter:
			digests = [d for d in digests if d.arxiv_id == self.ctx.paper_filter]
			if not digests:
				raise StageError(f"Paper {self.ctx.paper_filter!r} has no digest.")

		cfg = self.config.script
		spec = self.config.model_for(cfg.stage_model)
		prompt = load_prompt("script")
		client = MeteredClient.for_stage(spec, self.tracker, self.name)

		log.info(
			"Scripting %s segment(s) with %s/%s (prompt %s)",
			len(digests),
			spec.provider,
			spec.model,
			prompt.name,
		)

		with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
			futures = [
				pool.submit(self._write_segment, client, prompt, d, enriched.get(d.arxiv_id))
				for d in digests
			]
			results = [f.result() for f in futures]

		segments = [m for m in results if m is not None]
		if not segments:
			raise StageError(
				"Every segment failed to script. Check prompts/script/ and the model's max_tokens."
			)
		if len(segments) < len(digests):
			missing = [d.arxiv_id for d, m in zip(digests, results, strict=True) if m is None]
			log.warning("%s segment(s) failed: %s", len(missing), ", ".join(missing))

		episode = self._write_episode(
			client, [d for d, m in zip(digests, results, strict=True) if m], enriched, ranking
		)

		result = ScriptResult(generated_at=datetime.now(UTC), segments=segments, episode=episode)
		out = self.paths.stage_dir("script")
		write_json(out / "script.json", json.loads(result.model_dump_json()))

		# Reviewable markdown alongside the JSON.
		for m in segments:
			paper = enriched.get(m.arxiv_id)
			title = paper.paper.title if paper else m.arxiv_id
			(out / f"segment_{m.arxiv_id}.md").write_text(
				scenes_to_markdown(m, title), encoding="utf-8"
			)
		(out / "episode.md").write_text(self._episode_markdown(result, enriched), encoding="utf-8")

		log.info(
			"Scripted %s segment(s) and %s/%s transition(s), ~%.0fs total ($%.3f)",
			len(segments),
			len(episode.transitions),
			len(segments),
			result.est_seconds,
			self.tracker.stage_total(self.name),
		)
		return result

	def _episode_markdown(self, result: ScriptResult, enriched: dict) -> str:
		ep = result.episode
		out = [
			"# Episode",
			"",
			f"**Title** ({len(ep.title)}/{YOUTUBE_TITLE_MAX} chars): {ep.title or '_(none)_'}",
			"",
			f"**Estimated runtime:** {result.est_seconds / 60:.1f} min "
			f"({result.est_seconds:.0f}s across {len(result.segments)} segments)",
			"",
			"**Thumbnail options:** "
			+ (", ".join(f"`{t}`" for t in ep.thumbnail_text_options) or "_(none)_"),
			"",
			"## Description",
			"",
			ep.description or "_(none)_",
			"",
		]
		if ep.cold_open:
			out += ["", scenes_to_markdown(ep.cold_open, "Cold open")]
		# Bridges are read in the order they play, so they are interleaved here
		# rather than listed separately - the seam is the thing being reviewed.
		bridges = {t.into_arxiv_id: (i, t) for i, t in enumerate(ep.transitions)}
		for m in result.segments:
			paper = enriched.get(m.arxiv_id)
			title = paper.paper.title if paper else m.arxiv_id
			if m.arxiv_id in bridges:
				i, t = bridges[m.arxiv_id]
				out += [
					"",
					scenes_to_markdown(t.manifest(i, len(ep.transitions)), f"Transition → {title}"),
				]
			out += ["", scenes_to_markdown(m, title)]
		if ep.outro:
			out += ["", scenes_to_markdown(ep.outro, "Outro")]
		return "\n".join(out)
