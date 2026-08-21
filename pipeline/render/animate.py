"""Rendering an animated callout, and deciding when not to.

Stage 8 asks for a clip and gets back a path or `None`. `None` is not an error:
it means this scene stays the static callout it has always been. Every reason to
decline lives here - manim not installed, no comparison on the visual, the
feature switched off, a render that failed or ran long - so the render stage has
one branch rather than six.

manim runs in a **subprocess**, for three reasons: importing it costs seconds and
mutates global config that the rest of Stage 8 would inherit; a scene that raises
would otherwise take the episode down with it; and a timeout is only enforceable
across a process boundary. See DECISIONS.md D36.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..schemas import Comparison, Visual
from .slides import BG, FAINT, SlideContext

log = logging.getLogger(__name__)

SCENE_MODULE = Path(__file__).with_name("manim_scenes.py")
SCENE_CLASS = "Clip"


def manim_available() -> bool:
	"""Whether manim can be imported, without paying to import it."""
	try:
		return importlib.util.find_spec("manim") is not None
	except (ImportError, ValueError):  # a broken install resolves to neither
		return False


def wants_animation(visual: Visual) -> bool:
	"""A callout the model supplied template parameters for."""
	return visual.type == "result_callout" and visual.comparison is not None


def build_spec(comparison: Comparison, ctx: SlideContext, duration: float, fps: int) -> dict:
	"""Everything `manim_scenes` needs, flattened to JSON.

	The chrome fields are carried across so an animated slide sits in the same
	frame as the stills either side of it - same eyebrow, same attribution, same
	progress bar position.
	"""
	spec = comparison.model_dump()
	spec.update(
		duration=round(duration, 4),
		fps=fps,
		bg=list(BG),
		brace=list(FAINT),
		eyebrow=ctx.eyebrow,
		arxiv_id=ctx.arxiv_id,
		scene_index=ctx.scene_index,
		scene_total=ctx.scene_total,
	)
	return spec


def render_clip(
	visual: Visual,
	ctx: SlideContext,
	duration: float,
	out_dir: Path,
	name: str,
	fps: int = 30,
	timeout: int = 120,
) -> Path | None:
	"""Render one animated callout, or return None to fall back to the still.

	`duration` is what the clip must measure, including any cross-dissolve
	overlap the caller intends to consume. Stage 8 builds its timeline from
	measured audio (D20), so a clip that decided its own length would break the
	only sync guarantee the render has.
	"""
	if visual.comparison is None:
		return None
	if not manim_available():
		log.warning(
			"Animated callouts are on but manim is not installed "
			"(`uv pip install -e '.[manim]'`); rendering %s as a still",
			name,
		)
		return None

	out_dir.mkdir(parents=True, exist_ok=True)
	spec = build_spec(visual.comparison, ctx, duration, fps)

	with tempfile.TemporaryDirectory(prefix="tabloid-manim-") as tmp:
		env = {
			**os.environ,
			"TABLOID_CLIP_SPEC_JSON": json.dumps(spec),
			# manim's own progress bars are noise inside a stage that already logs.
			"MANIM_DISABLE_CACHING": "1",
		}
		cmd = [
			sys.executable,
			"-m",
			"manim",
			"render",
			str(SCENE_MODULE),
			SCENE_CLASS,
			"-r",
			f"{ctx.width},{ctx.height}",
			"--fps",
			str(fps),
			"--format=mp4",
			"--media_dir",
			tmp,
			"--output_file",
			name,
			"-v",
			"ERROR",
			"--disable_caching",
		]
		try:
			proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
		except subprocess.TimeoutExpired:
			log.warning(
				"%s: the animated callout took longer than %ss; rendering it as a still",
				name,
				timeout,
			)
			return None
		except OSError as e:
			log.warning("%s: could not start manim (%s); rendering it as a still", name, e)
			return None

		if proc.returncode != 0:
			log.warning(
				"%s: the animated callout failed to render; falling back to the still. %s",
				name,
				(proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or "",
			)
			return None

		produced = sorted(Path(tmp).rglob(f"{name}.mp4"))
		if not produced:
			log.warning("%s: manim reported success but produced no file; using the still", name)
			return None

		final = out_dir / f"{name}.mp4"
		final.write_bytes(produced[0].read_bytes())
		log.info("%s: animated callout (%s, %.2fs)", name, visual.comparison.template, duration)
		return final
