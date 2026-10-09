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

from ..llm import LLMError, MeteredClient, ProviderUnavailable, parse_json_response
from ..paths import read_json, write_json
from ..prompts import load_prompt
from ..schemas import (
	Comparison,
	EnrichedPaper,
	EpisodeMetadata,
	PaperDigest,
	Scene,
	SceneManifest,
	ScriptResult,
	Transition,
)
from ..stage import Stage, StageError

log = logging.getLogger(__name__)

# Things a TTS voice would read aloud as literal noise.
_LEAKAGE = re.compile(r"\$|\\[a-zA-Z]+|`|\*\*|#{1,6}\s|\[[^\]]*\]\([^)]*\)")

# What the scenes of each part are for. The condense pass rewrites narration to a
# word count and needs to be told what it must not rewrite away.
_SEGMENT_SHAPE = (
	"The first scene states the finding. The second-to-last gives the honest "
	"caveat. The last says what changes if this holds. A scene that lands a "
	"number still lands that number."
)
_COLD_OPEN_SHAPE = (
	"Two scenes: first a concrete tension supported by the papers, then a brief "
	"promise of what the episode explains. Do not repeat the first paper's opening "
	"finding or add a spoken series introduction."
)

YOUTUBE_TITLE_MAX = 70


# A value the model put in a comparison, matched against the digest it came
# from. Bounded by non-digits so "20" does not match inside "1200", but a
# trailing decimal is allowed so a digest's "80.4" satisfies a claimed "80".
def _detach_comparisons(payload: object) -> dict[str, object]:
	"""Lift every `comparison` out of a raw scene payload, keyed by scene id."""
	held: dict[str, object] = {}
	if not isinstance(payload, dict):
		return held
	for scene in payload.get("scenes") or []:
		if not isinstance(scene, dict):
			continue
		visual = scene.get("visual")
		if isinstance(visual, dict) and visual.get("comparison") is not None:
			held[str(scene.get("id"))] = visual.pop("comparison")
	return held


def _states(value: float, text: str) -> bool:
	return re.search(rf"(?<!\d){re.escape(f'{value:g}')}(?!\d)", text) is not None


def _detach_direction(payload: dict) -> dict[str, dict]:
	"""Optional direction must never cost the narration when a model mistypes it."""
	held = {}
	for scene in payload.get("scenes") or []:
		if isinstance(scene, dict):
			held[str(scene.get("id"))] = {
				key: scene.pop(key) for key in ("beat", "pause_after", "teaches") if key in scene
			}
			visual = scene.get("visual")
			if isinstance(visual, dict) and "note" in visual:
				# A non-rendered model annotation must not discard the spoken scene.
				visual.pop("note")
				log.warning("%s: ignoring unsupported visual note", scene.get("id"))
			if isinstance(visual, dict):
				for key in ("diagram", "reveal_phrase", "cue_phrases"):
					if key in visual:
						held[str(scene.get("id"))][key] = visual.pop(key)
	return held


def _attach_direction(manifest: SceneManifest, held: dict[str, dict]) -> None:
	for scene in manifest.scenes:
		for key, value in held.get(scene.id, {}).items():
			try:
				if key in ("diagram", "reveal_phrase", "cue_phrases"):
					candidate = manifest.model_dump()
					index = manifest.scenes.index(scene)
					candidate["scenes"][index]["visual"][key] = value
					checked_manifest = SceneManifest.model_validate(candidate)
					setattr(scene.visual, key, getattr(checked_manifest.scenes[index].visual, key))
				else:
					checked = Scene.model_validate({**scene.model_dump(), key: value})
					setattr(scene, key, getattr(checked, key))
			except ValueError:
				log.warning("%s/%s: ignoring invalid %s", manifest.arxiv_id, scene.id, key)


# `est_seconds` is the script's own budget arithmetic - words at
# `script.words_per_minute`, with no silence between scenes. It is the right
# number to hold the writing to, and the wrong one to predict a runtime with: it
# read 229s for an episode that would land at 4:32. These two constants are what
# the finished file actually does, measured across three shipped episodes - the
# narration reads at about 2.4 spoken words a second, and Stage 7 appends
# `tts.scene_gap_seconds` of silence to every clip. Measured across four runs the
# rate lands between 2.33 and 2.45 depending on how long the words are, so treat
# a projection as +/-5%: 4:45 means "between about 4:31 and 4:59" (D34).
MEASURED_WORDS_PER_SECOND = 2.40


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


def _mmss(seconds: float) -> str:
	return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


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
		if s.teaches:
			bits.append(f"teaches: {s.teaches}")
		if v.diagram:
			bits.append(
				"diagram: "
				+ v.diagram.id
				+ " / "
				+ ", ".join(f"{n.label} ({n.state})" for n in v.diagram.nodes)
			)
		if s.beat:
			bits.append(f"beat: {s.beat}")
		if s.pause_after is not None:
			bits.append(f"pause: {s.pause_after:.2f}s")
		if v.source:
			bits.append(f"source: {v.source}")
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
		from ..render.timing import cue_preflight

		requests, warnings = cue_preflight(s)
		if requests:
			out += ["", "Spoken cues (timing measured after narration):"]
			out += [f"- `{target}` → “{phrase}”" for target, phrase in requests.items()]
		if warnings:
			out += [""] + [f"- Review cue: {warning}" for warning in warnings]
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

	def _attach_comparisons(self, manifest: SceneManifest, held: dict[str, object]) -> None:
		"""Validate each held comparison on its own and attach the ones that pass.

		A rejection is logged rather than raised: the scene keeps its narration,
		its visual and its place in the segment, and simply renders as the static
		callout it would have been without this feature at all.
		"""
		if not held:
			return
		by_id = {s.id: s for s in manifest.scenes}
		for scene_id, raw in held.items():
			scene = by_id.get(scene_id)
			if scene is None:
				continue
			try:
				scene.visual.comparison = Comparison.model_validate(raw)
			except Exception as e:
				reason = str(e).split("Value error, ")[-1].split(" [type=")[0].strip()
				log.warning(
					"%s/%s: dropping the animated callout (%s); it renders as a static one",
					manifest.arxiv_id,
					scene_id,
					reason[:160],
				)

	def _check_comparisons(self, manifest: SceneManifest, digest_text: str) -> SceneManifest:
		"""Drop any animated callout whose numbers are not in the digest.

		The animation is the most credible thing on screen - a bar at a tenth the
		length of another *is* the claim - so a fabricated baseline is worse here
		than anywhere else in the pipeline. D17 found a model citing a real table
		and inventing its contents; this is the same failure with a chart drawn on
		top of it.

		Stricter than `extract.unverifiable_numbers`, which ignores integers under
		three digits because they appear all over a full paper. A digest is a few
		hundred words and a two-digit baseline is exactly what gets invented, so
		every value is checked. Dropping the comparison costs the animation and
		keeps the scene, which is the cheap direction to be wrong in.
		"""
		for scene in manifest.scenes:
			if scene.visual.diagram:
				for node in scene.visual.diagram.nodes:
					if any(
						not _states(float(n), digest_text)
						for n in re.findall(r"\d+(?:\.\d+)?", node.value)
					):
						log.warning(
							"%s/%s: dropping unverified node outcome", manifest.arxiv_id, scene.id
						)
						node.value = ""
			c = scene.visual.comparison
			if c is None:
				continue
			missing = [
				f"{v:g}"
				for v in (c.value_a, c.value_b)
				if v is not None and not _states(v, digest_text)
			]
			if missing:
				log.warning(
					"%s/%s: comparison claims %s, which the digest does not contain; "
					"rendering it as a static callout",
					manifest.arxiv_id,
					scene.id,
					" and ".join(missing),
				)
				scene.visual.comparison = None
		return manifest

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
		# Per-scene words come off the middle of the scene range, not the cap:
		# divided by the cap they describe a segment only at its longest, and a
		# model that writes the minimum number of scenes then comes in short.
		mid_scenes = (cfg.min_scenes_per_segment + cfg.max_scenes_per_segment) / 2
		system, user = prompt.render(
			arxiv_id=digest.arxiv_id,
			title=paper.paper.title if paper else digest.arxiv_id,
			figures=format_figures(paper),
			digest=digest_to_prompt_text(digest),
			wpm=cfg.words_per_minute,
			target_seconds=cfg.target_segment_seconds,
			target_words=target_words,
			min_scenes=cfg.min_scenes_per_segment,
			max_scenes=cfg.max_scenes_per_segment,
			words_per_scene=round(target_words / mid_scenes),
		)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
			payload = parse_json_response(resp.text)
			if not isinstance(payload, dict):
				raise LLMError(f"expected an object, got {type(payload).__name__}")
			payload["arxiv_id"] = digest.arxiv_id
			# Held back from the model_validate for the same reason transitions are
			# held back from the wrapper's: a comparison is an optional extra on one
			# scene, and a malformed one must cost that animation and nothing else.
			# Left inline, a single bad `value_b` fails the whole SceneManifest and
			# the segment is lost - which is what happened on the first live run of
			# prompt v6 (D36).
			detached = _detach_comparisons(payload)
			direction = _detach_direction(payload)
			manifest = SceneManifest.model_validate(payload)
			self._attach_comparisons(manifest, detached)
			_attach_direction(manifest, direction)
		except ProviderUnavailable:
			raise
		except Exception as e:
			log.error("%s: scripting failed (%s)", digest.arxiv_id, e)
			return None

		if not manifest.scenes:
			log.error("%s: script contained no scenes", digest.arxiv_id)
			return None

		# A concrete visual can open a standalone segment; it still needs a headline.
		if not manifest.scenes[0].visual.title:
			log.warning("%s: opening visual needs a standalone headline", digest.arxiv_id)
		manifest = self._check_comparisons(
			self._clean_manifest(manifest, paper), digest_to_prompt_text(digest)
		)
		return self._enforce_budget(manifest, target_words, client)

	def _enforce_budget(
		self, manifest: SceneManifest, target_words: int, client: MeteredClient | None = None
	) -> SceneManifest:
		"""Hold the segment to its scene cap and its word budget, in that order.

		Trimming from the end would take the close, which is the one scene the
		segment must not lose (D26 note 4). Trimming from the front would take the
		title card the standalone cut needs. So the head is kept up to the cap and
		the **last two** scenes - caveat, then close - are always preserved.

		The cap runs first so the condense pass is not paid for scenes that are
		about to be dropped. Words are still never cut *by code* - `_condense`
		spends a call asking for a shorter draft, and what is left over after it
		is reported, because at that point the prompt is the thing to fix.
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

		if client is not None:
			manifest = self._condense(client, manifest, target_words)

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

	def _condense(
		self,
		client: MeteredClient,
		manifest: SceneManifest,
		target_words: int,
		part: str = "segment",
		structure: str = _SEGMENT_SHAPE,
	) -> SceneManifest:
		"""Rewrite an over-budget part to length, in up to `condense_max_passes`
		calls.

		D29 enforced the scene *cap* in code and left the word budget as a request,
		on the reasoning that no code can shorten a sentence safely. True - but a
		model can, and asking it to do only that is a different job from asking for
		brevity while also asking for structure, register and accuracy. Across four
		prompt versions the narration has come back at 30-33 words per scene
		whatever number the prompt named, which makes the scene count the only
		thing that ever set the length (D34).

		Structure is not negotiable here, so the rewrite is accepted only if it
		returns every scene it was given, and the visuals are carried over from the
		original rather than re-sent - the slides are already chosen. If the model
		drops a scene, or comes back no shorter, the original stands.

		It repeats because one pass is not reliably enough: on the 2026-08-13
		digests two segments landed inside the budget first time and the third came
		down 279 -> 242 against 175, still over. A pass that made progress and fell
		short has more to give; a pass that was rejected does not, so the loop stops
		on the first one that fails rather than paying for the same refusal twice.
		"""
		cfg = self.config.script
		for _ in range(max(cfg.condense_max_passes, 0)):
			shorter = self._condense_once(client, manifest, target_words, part, structure)
			if shorter is None:
				break
			manifest = shorter
		return manifest

	def _condense_once(
		self,
		client: MeteredClient,
		manifest: SceneManifest,
		target_words: int,
		part: str,
		structure: str,
	) -> SceneManifest | None:
		"""One tightening pass. None means nothing usable came back - either the
		part was already inside its budget, or the rewrite was rejected."""
		cfg = self.config.script
		words = sum(len(s.narration.split()) for s in manifest.scenes)
		if not target_words or words <= target_words * (1 + cfg.condense_tolerance):
			return None

		spec = self.config.model_for(cfg.stage_model)
		system, user = load_prompt("condense").render(
			arxiv_id=manifest.arxiv_id,
			part=part,
			structure=structure,
			scenes="\n\n".join(
				f"{s.id} ({len(s.narration.split())} words): {s.narration}" for s in manifest.scenes
			),
			current_words=words,
			target_words=target_words,
			words_per_scene=round(target_words / max(len(manifest.scenes), 1)),
			wpm=cfg.words_per_minute,
		)
		try:
			resp = client.complete(
				user, system=system, max_tokens=spec.max_tokens, temperature=spec.temperature
			)
			payload = parse_json_response(resp.text)
			rewritten = {
				str(sc["id"]): str(sc["narration"]).strip()
				for sc in payload["scenes"]
				if str(sc.get("narration", "")).strip()
			}
		except ProviderUnavailable:
			raise
		except Exception as e:
			log.warning(
				"%s: could not tighten (%s); keeping the long version", manifest.arxiv_id, e
			)
			return None

		missing = [sc.id for sc in manifest.scenes if sc.id not in rewritten]
		if missing:
			log.warning(
				"%s: the tightened %s is missing scene(s) %s; keeping the long version",
				manifest.arxiv_id,
				part,
				", ".join(missing),
			)
			return None

		after = sum(len(rewritten[sc.id].split()) for sc in manifest.scenes)
		if after >= words:
			log.warning(
				"%s: the tightened %s came back at %s words against %s; keeping the original",
				manifest.arxiv_id,
				part,
				after,
				words,
			)
			return None

		for sc in manifest.scenes:
			sc.narration = rewritten[sc.id]
			sc.est_seconds = len(sc.narration.split()) / cfg.words_per_minute * 60
			# Re-checked here because `_clean_manifest` saw the draft, not this.
			if _LEAKAGE.search(sc.narration):
				log.warning(
					"%s/%s: tightened narration contains LaTeX/markdown a TTS voice "
					"would read aloud: %r",
					manifest.arxiv_id,
					sc.id,
					sc.narration[:80],
				)
		log.info(
			"%s: tightened %s from %s words to %s (budget %s)",
			manifest.arxiv_id,
			part,
			words,
			after,
			target_words,
		)
		return manifest

	def _clean_transitions(
		self, raw: object, order: list[str], first_optional: bool = False
	) -> list[Transition]:
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

		missing = [a for a in (order[1:] if first_optional else order) if a not in by_id]
		if missing:
			log.warning("No transition into %s; that seam stays a hard cut", ", ".join(missing))
		return [by_id[a] for a in order if a in by_id]

	def _write_episode(
		self,
		client: MeteredClient,
		digests: list[PaperDigest],
		enriched: dict,
		ranking,
		segments: list[SceneManifest] | None = None,
	) -> EpisodeMetadata:
		cfg = self.config.script
		spec = self.config.model_for(cfg.stage_model)
		prompt = load_prompt("episode")
		just = {r.arxiv_id: r.justification for r in ranking.finalists + ranking.substitutes}
		written = {m.arxiv_id: m for m in segments or []}

		blocks = []
		for d in digests:
			paper = enriched.get(d.arxiv_id)
			segment = written.get(d.arxiv_id)
			edges = ""
			if segment and segment.scenes:
				edges = (
					f"\nactual_opening: {segment.scenes[0].narration}"
					f"\nactual_close: {segment.scenes[-1].narration}"
				)
			blocks.append(
				f"---\narxiv_id: {d.arxiv_id}\n"
				f"title: {paper.paper.title if paper else ''}\n"
				f"claim: {d.one_sentence_claim}\n"
				f"why_it_matters: {d.why_it_matters}\n"
				f"justification: {just.get(d.arxiv_id, '')}\n"
				f"verified_digest:\n{digest_to_prompt_text(d)}{edges}"
			)

		# One scene to name the series and the thread, then one tease per paper.
		# The teases get the bulk of the budget; the framing scene takes what is
		# left, which is the ordering the cold open reads in.
		cold_words = int(cfg.cold_open_seconds / 60 * cfg.words_per_minute)
		tease_words = round(cold_words * 0.7 / max(len(digests), 1))
		trans_words = int(cfg.transition_seconds / 60 * cfg.words_per_minute)
		outro_words = int(cfg.outro_seconds / 60 * cfg.words_per_minute)
		system, user = prompt.render(
			n_papers=len(digests),
			papers="\n".join(blocks),
			cold_open_seconds=cfg.cold_open_seconds,
			cold_open_words=cold_words,
			cold_open_scenes=2,
			framing_words=cold_words - tease_words * len(digests),
			tease_words=tease_words,
			transition_seconds=cfg.transition_seconds,
			transition_words=trans_words,
			# A ceiling rather than a target: "about twelve words" read as a
			# suggestion and came back as nineteen (D34).
			transition_words_max=round(trans_words * 1.25),
			outro_seconds=cfg.outro_seconds,
			outro_words=outro_words,
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
			wrapper_direction = {}
			for key in ("cold_open", "outro"):
				if isinstance(payload.get(key), dict):
					wrapper_direction[key] = _detach_direction(payload[key])
			episode = EpisodeMetadata.model_validate(payload)
			for key, direction in wrapper_direction.items():
				if part := getattr(episode, key):
					_attach_direction(part, direction)
			episode.transitions = self._clean_transitions(
				raw_transitions, [d.arxiv_id for d in digests], first_optional=True
			)
		except ProviderUnavailable:
			# The degraded wrapper below exists for a bad JSON parse. This is not
			# that: every remaining call fails the same way, so the episode would
			# be narrated and rendered without a title, which is what happened on
			# 2026-08-19. Fail at the stage boundary, before Stage 7 spends.
			raise
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
				# No digest reaches the wrapper call, so nothing here can be checked
				# against one - and a hook of a dozen words is too short to build a
				# chart in regardless.
				for scene in extra.scenes:
					scene.visual.comparison = None
		# The cold open is the one part of the wrapper with enough words in it to
		# be worth a second call: it ran 34s against an 18s budget on 2026-08-13,
		# where a bridge over budget costs two seconds (D34).
		if episode.cold_open is not None:
			episode.cold_open = self._condense(
				client, episode.cold_open, cold_words, "cold open", _COLD_OPEN_SHAPE
			)
		# Measured against the ceiling the prompt states, not the budget behind it:
		# the prompt says "no more than N", so N is what was asked for.
		self._report_wrapper_budget(episode, cold_words, round(trans_words * 1.25), outro_words)
		return episode

	def _report_wrapper_budget(
		self, episode: EpisodeMetadata, cold_words: int, trans_words: int, outro_words: int
	) -> None:
		"""Measure the wrapper against its budget, in words, at the stage that
		writes it.

		Nothing is cut. The cold open cannot lose a scene without losing a paper's
		tease, and a bridge is one sentence that is either there or not - so unlike
		a segment, there is no trim here that keeps the shape. What there is to fix
		is the prompt, and what was missing was knowing before the render that it
		needed fixing: the 2026-08-13 wrapper ran 66s against a 46s budget and that
		only became visible as a 5:16 episode two stages later (D34).
		"""
		cfg = self.config.script
		parts: list[tuple[str, int, int]] = []
		if episode.cold_open:
			parts.append(
				(
					"cold open",
					sum(len(s.narration.split()) for s in episode.cold_open.scenes),
					cold_words,
				)
			)
		if episode.transitions:
			parts.append(
				(
					"transitions",
					sum(len(t.narration.split()) for t in episode.transitions),
					trans_words * len(episode.transitions),
				)
			)
		if episode.outro:
			parts.append(
				("outro", sum(len(s.narration.split()) for s in episode.outro.scenes), outro_words)
			)

		total, budget = sum(p[1] for p in parts), sum(p[2] for p in parts)
		detail = ", ".join(f"{name} {words}/{want}" for name, words, want in parts)
		log.info(
			"Wrapper: %s words against %s (%s), ~%.0fs of narration",
			total,
			budget,
			detail,
			total / cfg.words_per_minute * 60,
		)
		over = [(n, w, b) for n, w, b in parts if w > b * (1 + cfg.word_budget_tolerance)]
		for name, words, want in over:
			log.error(
				"Wrapper %s is %s words against a %s budget (%+.0f%%) - that time comes "
				"out of the papers. Tighten prompts/episode/, not this code.",
				name,
				words,
				want,
				(words / want - 1) * 100,
			)

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
			client,
			[d for d, m in zip(digests, results, strict=True) if m],
			enriched,
			ranking,
			segments,
		)

		result = ScriptResult(generated_at=datetime.now(UTC), segments=segments, episode=episode)
		out = self.paths.stage_dir("script")
		write_json(out / "script.json", json.loads(result.model_dump_json()))
		from ..editorial import pacing_report

		write_json(
			out / "pacing.json",
			pacing_report(
				result,
				scene_gap=self.config.tts.scene_gap_seconds,
				bridge_gap=self.config.tts.bridge_pause_seconds,
			),
		)

		# Reviewable markdown alongside the JSON.
		for m in segments:
			paper = enriched.get(m.arxiv_id)
			title = paper.paper.title if paper else m.arxiv_id
			(out / f"segment_{m.arxiv_id}.md").write_text(
				scenes_to_markdown(m, title), encoding="utf-8"
			)
		(out / "episode.md").write_text(self._episode_markdown(result, enriched), encoding="utf-8")

		projected = self._projected_seconds(result)
		log.info(
			"Scripted %s segment(s) and %s/%s transition(s), ~%s of finished episode ($%.3f)",
			len(segments),
			len(episode.transitions),
			len(segments),
			_mmss(projected),
			self.tracker.stage_total(self.name),
		)
		return result

	def _projected_seconds(self, result: ScriptResult) -> float:
		"""What the finished episode will run to, from the words written.

		Stage 8 is where a long episode used to become visible, which is two
		stages and a narration bill too late (D34).
		"""
		gap = self.config.tts.scene_gap_seconds
		words, silence = 0, 0.0
		for m in (*result.segments, result.episode.cold_open, result.episode.outro):
			if m is None:
				continue
			words += sum(len(sc.narration.split()) for sc in m.scenes)
			silence += sum(sc.pause_after if sc.pause_after is not None else gap for sc in m.scenes)
		# A bridge is one clip and takes the longer beat, not the scene gap.
		for t in result.episode.transitions:
			words += len(t.narration.split())
			silence += self.config.tts.bridge_pause_seconds
		return words / MEASURED_WORDS_PER_SECOND + silence

	def _episode_markdown(self, result: ScriptResult, enriched: dict) -> str:
		ep = result.episode
		out = [
			"# Episode",
			"",
			f"**Title** ({len(ep.title)}/{YOUTUBE_TITLE_MAX} chars): {ep.title or '_(none)_'}",
			"",
			f"**Projected runtime:** {_mmss(self._projected_seconds(result))} "
			f"across {len(result.segments)} segments "
			f"(script budget {result.est_seconds:.0f}s, before scene gaps)",
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
		bridges = {t.into_arxiv_id: t for t in ep.transitions}
		for m in result.segments:
			paper = enriched.get(m.arxiv_id)
			title = paper.paper.title if paper else m.arxiv_id
			if m.arxiv_id in bridges:
				t = bridges[m.arxiv_id]
				out += [
					"",
					scenes_to_markdown(result.transition_manifest(t), f"Transition → {title}"),
				]
			out += ["", scenes_to_markdown(m, title)]
		if ep.outro:
			out += ["", scenes_to_markdown(ep.outro, "Outro")]
		return "\n".join(out)
