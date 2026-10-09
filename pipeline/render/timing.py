"""Resolve exact spoken phrases against provider timing, never guessed word timing."""

import re

from ..schemas import Scene, SceneAudio


def reveal_cues(scene: Scene, audio: SceneAudio) -> tuple[dict[str, float], list[str]]:
	if not scene.visual.diagram:
		return {}, (
			["Phrase cues require a persistent diagram."] if scene.visual.reveal_phrase else []
		)
	requested = {}
	if scene.visual.reveal_phrase:
		requested["highlight"] = scene.visual.reveal_phrase
	if scene.visual.diagram:
		requested.update(
			{f"node:{n.id}": n.reveal_phrase for n in scene.visual.diagram.nodes if n.reveal_phrase}
		)
	if not requested:
		return {}, []
	alignment = audio.alignment
	if alignment is None:
		return {}, ["No character alignment; using scene-relative reveals."]
	spoken = "".join(alignment.characters)
	tokens = list(re.finditer(r"\w+", spoken.casefold()))
	# Casefold can change character offsets (e.g. sharp s); decline safely
	# instead of indexing timestamps with changed offsets.
	if len(spoken.casefold()) != len(spoken):
		return {}, ["Alignment normalization changes offsets; using scene-relative reveals."]
	words = [m.group() for m in tokens]
	if words != re.findall(r"\w+", scene.narration.casefold()):
		return {}, ["Alignment text differs from narration; using scene-relative reveals."]
	if alignment.character_end_times_seconds[-1] > audio.duration_seconds:
		return {}, ["Alignment exceeds audio duration; using scene-relative reveals."]
	cues, warnings = {}, []
	for key, phrase in requested.items():
		wanted = re.findall(r"\w+", phrase.casefold())
		matches = [
			i
			for i in range(len(words) - len(wanted) + 1)
			if wanted and words[i : i + len(wanted)] == wanted
		]
		if len(matches) != 1:
			warnings.append(f"{key}: phrase absent or ambiguous; using scene-relative reveal.")
			continue
		cues[key] = alignment.character_start_times_seconds[tokens[matches[0]].start()]
	return cues, warnings
