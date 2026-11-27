"""A camera over a paper figure: show it whole, then go to the panel being discussed.

The figure slide was the most-used slide in the show and the least guided. A
dense multi-panel figure sat on screen for ten seconds while the narration
talked about one corner of it, and the viewer had to find that corner alone -
the split attention that Mayer's signaling principle measures at a median
d ~ 0.4-0.7 (D50). A documentary editor solves it with a push-in; 3Blue1Brown
does it by dimming everything that is not the subject. This does both:

    0.0s    the whole figure, as now - orientation, so the zoom has a context
    ~1.1s   the camera eases into panel (b) while everything else dims
    ...     a slow drift on (b), so a held shot is never a frozen frame
    then    on to (c), if the narration names a second panel

Rendered frame by frame in Pillow from a 2x supersample, so the move is
sub-pixel smooth (ffmpeg's zoompan rounds its crop to whole pixels and judders),
and piped to ffmpeg as raw frames. The zoom is capped by the figure's own
resolution: a 600-pixel figure is not blown up into porridge to fill the frame.

Returns None on anything unexpected and the caller shows the still, exactly like
every other moving slide (D36).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from .panels import Panel, lettered_panels
from .slides import SlideContext, figure_slide, mix

log = logging.getLogger(__name__)

SUPERSAMPLE = 2
# The furthest the camera will magnify the source figure's own pixels. Past
# ~1.8x native a raster figure is visibly soft on a 1080p frame.
MAX_NATIVE_ZOOM = 1.8
# Never zoom so far that a panel fills the frame edge to edge: the dimmed
# surround is what tells the viewer where in the figure they are.
PANEL_FILL = 0.78
ORIENT_SECONDS = 1.1
MOVE_SECONDS = 0.9
DRIFT = 0.025  # how much tighter the shot gets across a hold
DIM = 0.62  # how far the rest of the figure is pushed toward the ground
MAX_STOPS = 3


def _ease(t: float) -> float:
	"""Ease in-out (cubic). A linear zoom starts and stops with a jolt."""
	t = min(max(t, 0.0), 1.0)
	return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _lerp_box(a, b, t):
	return tuple(x + (y - x) * t for x, y in zip(a, b, strict=True))


def _to_slide(panel: Panel, place: dict) -> tuple[float, float, float, float]:
	ox, oy = place["origin"]
	tx, ty = place["trim"]
	s = place["scale"]
	x0, y0, x1, y1 = panel.box
	return (ox + (x0 - tx) * s, oy + (y0 - ty) * s, ox + (x1 - tx) * s, oy + (y1 - ty) * s)


def _shot(rect, frame_w: int, frame_h: int, min_w: float) -> tuple[float, float, float, float]:
	"""The 16:9 camera box that frames `rect`, clamped to the slide."""
	x0, y0, x1, y1 = rect
	w = max((x1 - x0) / PANEL_FILL, (y1 - y0) / PANEL_FILL * frame_w / frame_h, min_w)
	w = min(w, frame_w)
	h = w * frame_h / frame_w
	cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
	left = min(max(cx - w / 2, 0), frame_w - w)
	top = min(max(cy - h / 2, 0), frame_h - h)
	return (left, top, left + w, top + h)


def _tighten(box, amount: float, frame_w: int, frame_h: int):
	x0, y0, x1, y1 = box
	dw, dh = (x1 - x0) * amount / 2, (y1 - y0) * amount / 2
	return (x0 + dw, y0 + dh, x1 - dw, y1 - dh)


def _spotlight(base: Image.Image, rect, accent, ground, radius: int, line: int) -> Image.Image:
	"""The slide with everything outside `rect` pushed toward the ground."""
	dimmed = Image.blend(base, Image.new("RGB", base.size, ground), DIM)
	mask = Image.new("L", base.size, 0)
	ImageDraw.Draw(mask).rounded_rectangle([int(v) for v in rect], radius=radius, fill=255)
	out = Image.composite(base, dimmed, mask)
	ImageDraw.Draw(out).rounded_rectangle(
		[int(v) for v in rect], radius=radius, outline=accent, width=line
	)
	return out


def caption_for(figures_dir: Path, figure_file: str) -> str:
	"""The caption Stage 3 stored for a figure; panels are matched against it."""
	try:
		entries = json.loads((figures_dir / "captions.json").read_text())
	except (OSError, ValueError):
		return ""
	name = Path(figure_file).name
	for e in entries:
		if Path(e.get("file") or "").name == name:
			return e.get("caption") or ""
	return ""


def plan(focus: list[str], panels: dict[str, Panel]) -> list[Panel]:
	"""The panels to visit, in the order the script named them, unknowns dropped."""
	stops: list[Panel] = []
	for ref in focus[:MAX_STOPS]:
		p = panels.get(str(ref).strip().lower().strip("()"))
		if p is not None and p not in stops:
			stops.append(p)
	return stops


def render_tour(
	ctx: SlideContext,
	figure_path: Path,
	caption: str,
	focus: list[str],
	attribution: str,
	title: str,
	highlight: str,
	duration: float,
	out: Path,
	fps: int = 30,
	timeout: int = 120,
) -> Path | None:
	"""Render the figure tour clip at exactly `duration`, or None for the still."""
	if not focus or shutil.which("ffmpeg") is None or duration < ORIENT_SECONDS + MOVE_SECONDS:
		return None
	try:
		src = Image.open(figure_path).convert("RGB")
	except Exception as e:
		log.warning("Tour: cannot open %s: %s", figure_path, e)
		return None
	stops = plan(focus, lettered_panels(src, caption))
	if not stops:
		return None

	big = replace(ctx, width=ctx.width * SUPERSAMPLE, height=ctx.height * SUPERSAMPLE)
	place: dict = {}
	base = figure_slide(big, figure_path, attribution, title, highlight, placement=place)
	if "origin" not in place:
		return None
	W, H = base.size
	# How wide a shot may get before the figure's own pixels are magnified past
	# MAX_NATIVE_ZOOM on the output frame.
	min_w = ctx.width * place["scale"] / MAX_NATIVE_ZOOM
	pad = big.scaled(14)
	accent = ctx.accent
	ground = mix((22, 26, 33), (0, 0, 0), 0.2)

	full = (0.0, 0.0, float(W), float(H))
	shots, lit = [], []
	for p in stops:
		x0, y0, x1, y1 = _to_slide(p, place)
		rect = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
		shots.append(_shot(rect, W, H, min_w))
		lit.append(_spotlight(base, rect, accent, ground, big.scaled(14), big.scaled(4)))

	# Timeline: orient, then one leg per stop - a move and a hold with drift.
	orient = min(ORIENT_SECONDS, duration * 0.2)
	leg = (duration - orient) / len(stops)
	move = min(MOVE_SECONDS, leg * 0.45)
	if leg < move + 0.6:
		stops, shots, lit = stops[:1], shots[:1], lit[:1]
		leg = duration - orient
		move = min(MOVE_SECONDS, leg * 0.45)

	frames = max(round(duration * fps), 1)
	out.parent.mkdir(parents=True, exist_ok=True)
	cmd = [
		"ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
		"-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ctx.width}x{ctx.height}",
		"-r", str(fps), "-i", "-",
		"-frames:v", str(frames),
		"-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
		str(out),
	]  # fmt: skip
	try:
		proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
	except OSError as e:
		log.warning("Tour: ffmpeg would not start: %s", e)
		return None

	blend_cache: dict[tuple[int, int], Image.Image] = {}

	def state(i_from: int, i_to: int, a: float) -> Image.Image:
		"""-1 is the plain slide; 0.. are the spotlit ones. Blends are quantised."""
		src_img = base if i_from < 0 else lit[i_from]
		dst_img = lit[i_to]
		q = round(a * 24)
		if q <= 0:
			return src_img
		if q >= 24:
			return dst_img
		key = (i_from * 100 + i_to, q)
		if key not in blend_cache:
			blend_cache[key] = Image.blend(src_img, dst_img, q / 24)
		return blend_cache[key]

	try:
		for f in range(frames):
			t = f / fps
			if t < orient:
				box, img = full, base
			else:
				k = min(int((t - orient) // leg), len(stops) - 1)
				u = t - orient - k * leg
				prev_box = full if k == 0 else _tighten(shots[k - 1], DRIFT, W, H)
				if u < move:
					a = _ease(u / move)
					box = _lerp_box(prev_box, shots[k], a)
					img = state(k - 1, k, a)
				else:
					hold = max(leg - move, 1e-6)
					box = _tighten(shots[k], DRIFT * _ease((u - move) / hold), W, H)
					img = lit[k]
			frame = img.resize((ctx.width, ctx.height), Image.BICUBIC, box=box)
			proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
		proc.stdin.close()  # type: ignore[union-attr]
		proc.wait(timeout=timeout)
	except (OSError, subprocess.SubprocessError, ValueError) as e:
		proc.kill()
		log.warning("Tour for %s failed: %s; rendering it still", out.name, e)
		return None
	if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
		err = proc.stderr.read().decode(errors="ignore")[:200] if proc.stderr else ""
		log.warning("Tour for %s failed: %s", out.name, err)
		return None
	log.info("  %s: figure tour (%s, %.1fs)", out.stem, ", ".join(focus[: len(stops)]), duration)
	return out
