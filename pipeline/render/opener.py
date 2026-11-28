"""The series opener: the first ~2-3 seconds, as a moving title rather than a slide.

The cold open's first scene says the series name and nothing else (D35). It was
a title card - the same dark ground and type as every other card - so the most
important seconds of the episode looked like slide one of a deck (owner,
2026-11-27: "still too boring ... needs to be original and captivating").

It is now a title sequence over one piece of series artwork, generated once and
kept in `assets/opener/` (D51), the way a show has one opening rather than a new
one each week:

    0.00  black, fading up onto the artwork as the camera starts to push in
    0.35  "ML Papers" lands - rises 24px and fades in over 0.35s
    0.65  "of the Day" lands
    1.00  the accent rule wipes in under it, then the kicker
    ...   the slow push continues to the end of the narration

Rendered frame by frame in Pillow like the figure camera (D50), exact duration,
and None on anything unexpected so the caller draws the title card it always did.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from ..paths import REPO_ROOT
from .slides import BG, FG, SERIES_ACCENT, SlideContext, _draw_tracked, bold, mix

log = logging.getLogger(__name__)

ARTWORK = REPO_ROOT / "assets" / "opener" / "opener.png"
PUSH = 0.07  # how much tighter the frame is at the end than at the start
FADE_IN = 0.45


def _ease_out(t: float) -> float:
	t = min(max(t, 0.0), 1.0)
	return 1 - (1 - t) ** 3


def _ground(ctx: SlideContext, art_path: Path) -> Image.Image:
	"""The artwork covering a frame with PUSH of headroom, a scrim under the type."""
	art = Image.open(art_path).convert("RGB")
	w, h = int(ctx.width * (1 + PUSH)), int(ctx.height * (1 + PUSH))
	scale = max(w / art.width, h / art.height)
	art = art.resize((int(art.width * scale + 0.5), int(art.height * scale + 0.5)), Image.LANCZOS)
	left, top = (art.width - w) // 2, (art.height - h) // 2
	art = art.crop((left, top, left + w, top + h))
	# Only the left of the frame carries type; darken it there and leave the
	# light on the right alone.
	row = Image.new("L", (w, 1))
	row.putdata([int(255 * max(0.0, 0.78 - 1.25 * (x / w))) for x in range(w)])
	return Image.composite(Image.new("RGB", art.size, BG), art, row.resize((w, h)))


def render_opener(
	ctx: SlideContext,
	title: str,
	kicker: str,
	duration: float,
	out: Path,
	fps: int = 30,
	art: Path | None = None,
	timeout: int = 120,
) -> Path | None:
	art = art or ARTWORK
	if not art.exists() or shutil.which("ffmpeg") is None or duration < 1.0:
		return None
	try:
		ground = _ground(ctx, art)
	except Exception as e:
		log.warning("Opener artwork unusable (%s); drawing the title card", e)
		return None

	words = title.split()
	# Two lines: "ML Papers" / "of the Day". Split before "of" when there is
	# one, so a renamed series still breaks somewhere sensible.
	cut = words.index("of") if "of" in words[1:] else max(len(words) // 2, 1)
	lines = [" ".join(words[:cut]), " ".join(words[cut:])]
	m = ctx.scaled(120)
	font = bold(ctx.scaled(150))
	lead = int(font.size * 1.05)
	block = lead * len(lines)
	y0 = (ctx.height - block) // 2 - ctx.scaled(30)
	kf = bold(ctx.scaled(30))
	accent = SERIES_ACCENT

	frames = max(round(duration * fps), 1)
	out.parent.mkdir(parents=True, exist_ok=True)
	cmd = [
		"ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
		"-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ctx.width}x{ctx.height}",
		"-r", str(fps), "-i", "-", "-frames:v", str(frames),
		"-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
		str(out),
	]  # fmt: skip
	try:
		proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
	except OSError:
		return None

	gw, gh = ground.size
	try:
		for f in range(frames):
			t = f / fps
			z = PUSH * _ease_out(t / max(duration, 1e-6)) * 0.9 + PUSH * 0.1 * (t / duration)
			cw, ch = gw / (1 + z) * 1.0, gh / (1 + z)
			cw, ch = min(cw, gw), min(ch, gh)
			# Push toward the light on the right of the artwork, not the centre.
			cx, cy = gw * (0.5 + 0.06 * z / PUSH), gh * 0.5
			box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
			box = (
				max(0, min(box[0], gw - cw)),
				max(0, min(box[1], gh - ch)),
				max(0, min(box[0], gw - cw)) + cw,
				max(0, min(box[1], gh - ch)) + ch,
			)
			frame = ground.resize((ctx.width, ctx.height), Image.BICUBIC, box=box)
			if t < FADE_IN:
				frame = ImageEnhance.Brightness(frame).enhance(_ease_out(t / FADE_IN))

			d = ImageDraw.Draw(frame)
			for i, line in enumerate(lines):
				a = _ease_out((t - 0.35 - 0.3 * i) / 0.35)
				if a <= 0:
					continue
				y = y0 + i * lead + int(ctx.scaled(24) * (1 - a))
				d.text((m, y), line, font=font, fill=mix(BG, FG, a))
			r = _ease_out((t - 1.0) / 0.4)
			if r > 0:
				ry = y0 + block + ctx.scaled(26)
				d.rectangle([m, ry, m + int(ctx.scaled(220) * r), ry + ctx.scaled(10)], fill=accent)
				if kicker:
					_draw_tracked(
						d, kicker.upper(), kf, m, ry + ctx.scaled(40),
						mix(BG, accent, r), ctx.scaled(6),
					)  # fmt: skip
			proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
		proc.stdin.close()  # type: ignore[union-attr]
		proc.wait(timeout=timeout)
	except (OSError, subprocess.SubprocessError, ValueError) as e:
		proc.kill()
		log.warning("Opener failed (%s); drawing the title card", e)
		return None
	if proc.returncode != 0 or not out.exists():
		return None
	log.info("  opener: title sequence (%.1fs)", duration)
	return out
