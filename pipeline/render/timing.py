"""Resolve exact spoken phrases against provider timing, never guessed word timing."""

import re

from ..schemas import Scene, SceneAudio


def cue_requests(scene: Scene) -> tuple[dict[str, str], list[str]]:
	"""Return only slots the current layout actually renders."""
	v = scene.visual
	supported = set()
	if v.diagram:
		supported.update(f"node:{n.id}" for n in v.diagram.nodes)
	elif v.type in ("process", "contrast", "bullet_slide"):
		count = min(len(v.bullets), 2 if v.type == "contrast" else 4)
		supported.update(f"item:{i}" for i in range(count))
	if v.type == "result_callout" and v.comparison:
		supported.add("value:a")
		if v.comparison.template == "two_bar":
			supported.add("value:b")
		# A split reveals the entire population together; it cannot hide the
		# remainder without distorting the chart's proportions.
	elif v.highlight and v.type != "transition":
		if v.type != "bullet_slide" or not v.bullets:
			supported.add("highlight")
	if v.type == "figure":
		supported.add("figure")
	requested = dict(v.cue_phrases)
	warnings = []
	legacy = {}
	if v.reveal_phrase:
		legacy["highlight"] = v.reveal_phrase
	if v.diagram:
		legacy.update({f"node:{n.id}": n.reveal_phrase for n in v.diagram.nodes if n.reveal_phrase})
	for target, phrase in legacy.items():
		if target in requested and requested[target] != phrase:
			warnings.append(f"{target}: conflicting cues; using its existing reveal_phrase.")
		requested[target] = phrase
	for target in list(requested):
		if target not in supported:
			warnings.append(f"{target}: no rendered target in this {v.type} layout; cue ignored.")
			del requested[target]
	return requested, warnings


def _matches(words: list[str], phrase: str) -> list[int]:
	wanted = re.findall(r"\w+", phrase.casefold())
	return [
		i
		for i in range(len(words) - len(wanted) + 1)
		if wanted and words[i : i + len(wanted)] == wanted
	]


def cue_preflight(scene: Scene) -> tuple[dict[str, str], list[str]]:
	"""Catch missing/ambiguous phrases before paying for speech; infer no times."""
	requested, warnings = cue_requests(scene)
	words = re.findall(r"\w+", scene.narration.casefold())
	for key, phrase in list(requested.items()):
		if len(_matches(words, phrase)) != 1:
			warnings.append(f"{key}: phrase absent or ambiguous; using scene-relative reveal.")
			del requested[key]
	return requested, warnings


def reveal_cues(scene: Scene, audio: SceneAudio) -> tuple[dict[str, float], list[str]]:
	requested, warnings = cue_preflight(scene)
	if not requested:
		return {}, warnings
	alignment = audio.alignment
	if alignment is None:
		return {}, [*warnings, "No character alignment; using scene-relative reveals."]
	spoken = "".join(alignment.characters)
	tokens = list(re.finditer(r"\w+", spoken.casefold()))
	# Casefold can change character offsets (e.g. sharp s); decline safely
	# instead of indexing timestamps with changed offsets.
	if len(spoken.casefold()) != len(spoken):
		return {}, [
			*warnings,
			"Alignment normalization changes offsets; using scene-relative reveals.",
		]
	words = [m.group() for m in tokens]
	if words != re.findall(r"\w+", scene.narration.casefold()):
		return {}, [
			*warnings,
			"Alignment text differs from narration; using scene-relative reveals.",
		]
	if alignment.character_end_times_seconds[-1] > audio.duration_seconds:
		return {}, [*warnings, "Alignment exceeds audio duration; using scene-relative reveals."]
	cues = {}
	for key, phrase in requested.items():
		matches = _matches(words, phrase)
		if len(matches) != 1:
			warnings.append(f"{key}: phrase absent or ambiguous; using scene-relative reveal.")
			continue
		cues[key] = alignment.character_start_times_seconds[tokens[matches[0]].start()]
	return cues, warnings
