"""Staged reveals: a slide that arrives in pieces instead of all at once.

Stage 8's unit has always been one still per scene, held for as long as the
narration for it runs. Measured on Ep. 6 that is 9.3 seconds a slide, median,
with fourteen of thirty-two slides on screen for more than ten - and only three
scenes in the whole episode moving at all, because motion was gated to the three
`comparison` templates manim draws.

This module widens that without a new dependency and without manim, which has
never rendered on the Linux runner (D37). The idea is deliberately small: render
the *same* slide two or more times with one element withheld, then dissolve
between the states. Bullets land one at a time; a headline number arrives on a
rule that is already there; a figure gets its caption a beat after the figure.

Two properties make it safe. Layout is measured from the finished slide in every
state, so nothing reflows as pieces appear - that is what the `reveal` argument
in `slides.py` buys. And the clip measures exactly the duration it was asked
for, because Stage 8 builds its timeline from measured audio (D20) and a clip
that chose its own length would break the only sync guarantee the render has.

Anything that fails here returns None and the caller renders the still it would
have rendered anyway, exactly as the manim path does.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .slides import SlideContext, render_visual

log = logging.getLogger(__name__)

# Under this, a state is on screen too briefly to read as anything but a flicker,
# so the slide is better still. Measured against the *shortest* state.
MIN_STATE_SECONDS = 1.1
# The dissolve between two states. Shorter than the one between scenes (0.4):
# this is one slide continuing, not a cut to a new idea.
STATE_FADE = 0.28
MAX_BULLET_STATES = 5


def reveal_steps(visual) -> list[int]:
	"""The `reveal` values to render for this visual, in order.

	Fewer than two states means there is nothing to stage, and the caller falls
	back to the still.
	"""
	vtype = visual.type
	if vtype == "bullet_slide":
		rows = len([b for b in visual.bullets if b and b.strip()][:MAX_BULLET_STATES])
		return list(range(1, rows + 1)) if rows >= 2 else []
	if vtype == "result_callout":
		# A callout with a comparison belongs to manim; this is the static one.
		if visual.comparison is None and (visual.highlight or visual.title):
			return [0, 1]
		return []
	if vtype == "figure":
		# Only worth staging when there is a caption to bring in after the figure.
		return [0, 1] if (visual.figure_file and visual.highlight) else []
	return []


def wants_reveal(visual) -> bool:
	return len(reveal_steps(visual)) >= 2


def _xfade_chain(n: int, hold: float, fade: float) -> tuple[str, str]:
	"""Chain n equal-length states with dissolves. Returns (filtergraph, label).

	The same arithmetic Stage 8 uses between scenes: each input runs `hold+fade`
	and every xfade consumes exactly that overlap, so n states of `hold` come out
	as `n*hold` rather than accumulating or losing the fades.
	"""
	parts, prev, cum = [], "s0", 0.0
	for i in range(1, n):
		cum += hold
		parts.append(
			f"[{prev}][s{i}]xfade=transition=fade:duration={fade:.3f}:"
			f"offset={cum - fade:.3f}[x{i}]"
		)
		prev = f"x{i}"
	return ";".join(parts), prev


def render_clip(
	visual,
	ctx: SlideContext,
	duration: float,
	out_dir: Path,
	name: str,
	fps: int = 30,
	backdrop: Path | None = None,
	timeout: int = 120,
) -> Path | None:
	"""Render one staged-reveal clip, or None to fall back to the still.

	`duration` is what the clip must measure, cross-dissolve overlap included.
	"""
	steps = reveal_steps(visual)
	n = len(steps)
	if n < 2:
		return None
	if shutil.which("ffmpeg") is None:
		return None

	hold = duration / n
	if hold < MIN_STATE_SECONDS:
		# A four-bullet slide inside a six-second scene is a flicker, not a
		# reveal. Drop states rather than the whole effect: showing two rows and
		# then the rest still beats cutting to all four at once.
		keep = max(int(duration // MIN_STATE_SECONDS), 1)
		if keep < 2:
			return None
		steps = steps[-keep:] if visual.type == "bullet_slide" else steps[:keep]
		n = len(steps)
		hold = duration / n

	fade = min(STATE_FADE, hold * 0.5)
	out_dir.mkdir(parents=True, exist_ok=True)

	frames: list[Path] = []
	for i, step in enumerate(steps):
		img = render_visual(ctx, visual, "", backdrop, reveal=step)
		path = out_dir / f"{name}_r{i}.png"
		img.save(path, "PNG")
		frames.append(path)

	out = out_dir / f"{name}.mp4"
	args: list[str] = []
	for path in frames:
		args += ["-loop", "1", "-t", f"{hold + fade:.4f}", "-i", str(path)]
	pre = ";".join(f"[{i}:v]format=yuv420p,fps={fps}[s{i}]" for i in range(n))
	chain, last = _xfade_chain(n, hold, fade)
	graph = f"{pre};{chain}" if chain else pre

	cmd = [
		"ffmpeg",
		"-hide_banner",
		"-loglevel",
		"error",
		"-y",
		*args,
		"-filter_complex",
		graph,
		"-map",
		f"[{last}]",
		"-c:v",
		"libx264",
		"-preset",
		"medium",
		"-crf",
		"20",
		"-pix_fmt",
		"yuv420p",
		"-r",
		str(fps),
		str(out),
	]
	try:
		subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
	except (subprocess.SubprocessError, OSError) as e:
		detail = getattr(e, "stderr", "") or ""
		log.warning(
			"Reveal for %s failed (%s); rendering it still", name, detail.strip()[:200] or e
		)
		return None
	if not out.exists() or out.stat().st_size == 0:
		return None
	log.info("  %s: staged reveal (%s states, %.1fs)", name, n, duration)
	return out
