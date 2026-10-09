"""Review the edit before paying for voice, then measure it after voice.

These are actionable editing notes, not a prediction of audience retention.
Estimated rows are explicitly distinguished from measured audio durations.
"""

from __future__ import annotations

from statistics import mean

from .render.timing import cue_preflight, reveal_cues
from .schemas import ScriptResult, VoiceResult


def pacing_report(
	script: ScriptResult,
	voice: VoiceResult | None = None,
	words_per_second: float = 2.4,
	scene_gap: float = 0.14,
	bridge_gap: float = 0.3,
) -> dict:
	parts = []
	if script.episode.cold_open:
		parts.append(("cold_open", script.episode.cold_open, scene_gap))
	transitions = {t.into_arxiv_id: t for t in script.episode.transitions}
	for segment in script.segments:
		if segment.arxiv_id in transitions:
			t = transitions[segment.arxiv_id]
			parts.append((f"bridge_{segment.arxiv_id}", script.transition_manifest(t), bridge_gap))
		parts.append((segment.arxiv_id, segment, scene_gap))
	if script.episode.outro:
		parts.append(("outro", script.episode.outro, scene_gap))

	measured = {}
	if voice:
		for seg in voice.segments:
			measured[seg.arxiv_id] = {s.scene_id: s for s in seg.scenes}
		for name in ("cold_open", "outro"):
			if seg := getattr(voice, name):
				measured[name] = {s.scene_id: s for s in seg.scenes}
		for seg in voice.transitions:
			measured[f"bridge_{seg.arxiv_id}"] = {s.scene_id: s for s in seg.scenes}

	rows, issues = [], []
	elapsed, measured_count = 0.0, 0
	for name, manifest, gap in parts:
		streak, previous = 0, None
		seen_goals = set()
		for scene in manifest.scenes:
			words = len(scene.narration.split())
			pause = (
				gap
				if name.startswith("bridge_") or scene.pause_after is None
				else scene.pause_after
			)
			clip = measured.get(name, {}).get(scene.id)
			actual = clip.duration_seconds if clip is not None else None
			requests, preflight_warnings = cue_preflight(scene)
			cues, cue_warnings = reveal_cues(scene, clip) if clip else ({}, preflight_warnings)
			duration = actual if actual is not None else words / words_per_second + pause
			measured_count += actual is not None
			v = scene.visual
			row = {
				"part": name,
				"scene": scene.id,
				"beat": scene.beat,
				"teaches": scene.teaches,
				"diagram": v.diagram.id if v.diagram else None,
				"visual": v.type,
				"start_seconds": round(elapsed, 3),
				"duration_seconds": round(duration, 3),
				"timing": "measured" if actual is not None else "estimated",
				"words": words,
				"reveal_phrases": requests,
				"reveal_cues_seconds": cues,
				"reveal_warnings": cue_warnings,
			}
			rows.append(row)

			def note(code: str, message: str, part=name, scene_id=scene.id) -> None:
				issues.append({"part": part, "scene": scene_id, "code": code, "message": message})

			for warning in cue_warnings:
				note("unaligned_reveal" if clip else "invalid_reveal_phrase", warning)
			goal = " ".join(scene.teaches.casefold().split()).rstrip(".")
			if not goal and not name.startswith("bridge_"):
				note("missing_learning_goal", "State the one thing this scene teaches.")
			elif goal and goal in seen_goals:
				note(
					"repeated_learning_goal",
					"Same learning goal: check whether this scene adds anything.",
				)
			seen_goals.add(goal)
			if v.diagram and any(n.state == "removed" for n in v.diagram.nodes) and not v.source:
				note("unsourced_ablation", "A removed component needs a source for the ablation.")
			if duration > 12:
				note("long_scene", "Over 12 seconds: split the idea or simplify the explanation.")
			if scene.beat == "hook" and duration > 7:
				note("slow_hook", "The hook takes over 7 seconds to land.")
			if v.type == "figure" and not (v.highlight or v.title):
				note("unguided_figure", "Add a headline or reading cue to guide this paper figure.")
			layout = (v.type, v.diagram.id if v.diagram else None)
			streak = streak + 1 if layout == previous else 1
			if streak == 3 and not v.diagram:
				note(
					"repeated_layout",
					"Three identical layouts in a row: vary how the idea is shown.",
				)
			if not scene.narration.strip():
				note("empty_narration", "This scene will be skipped by the voice stage.")
			previous = layout
			elapsed += duration
		if (
			name == "cold_open"
			and sum(r["duration_seconds"] for r in rows if r["part"] == name) > 12
		):
			issues.append(
				{
					"part": name,
					"scene": "",
					"code": "long_open",
					"message": "The opening takes over 12 seconds before the first paper.",
				}
			)
		if (
			manifest.arxiv_id != "episode"
			and manifest.scenes
			and manifest.scenes[-1].beat == "caveat"
		):
			issues.append(
				{
					"part": name,
					"scene": manifest.scenes[-1].id,
					"code": "missing_payoff",
					"message": "The paper ends on its limitation.",
				}
			)

	timing = (
		"estimated"
		if measured_count == 0
		else "measured"
		if measured_count == len(rows)
		else "mixed"
	)
	return {
		"timing": timing,
		"duration_seconds": round(elapsed, 3),
		"scene_count": len(rows),
		"average_scene_seconds": round(mean(r["duration_seconds"] for r in rows), 2) if rows else 0,
		"note": "Editing heuristics, not an audience-retention score. "
		f"Estimated timing uses {words_per_second:g} words/s.",
		"scenes": rows,
		"issues": issues,
	}
