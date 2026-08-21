"""Thumbnail layouts.

A thumbnail is not a slide. A slide is read at full size for ten seconds; a
thumbnail is read at roughly 320x180 in a crowded feed, in under a second. That
changes what "good" means:

- **Fill the frame.** The Stage 9 thumbnail letterboxes the figure into a card,
  so a wide figure leaves half the card empty and the frame reads as mostly
  background. Every layout here uses `cover` (resize to fill, crop the overflow)
  instead of `fit`, which is what CSS `object-fit: cover` does and what every
  thumbnail that works does.
- **Fewer, larger words.** Type starts far larger than on a slide and shrinks
  only as far as it must.
- **Contrast under the text.** Type over an image needs a scrim or it dies
  against a light patch of figure.

Each layout is `(ctx, text, figure, kicker) -> Image`, the same signature as the
existing `slides.thumbnail`, so any of them can drop into Stage 9. `LAYOUTS` maps
a config name to one.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .slides import (
	_BOLD,
	ACCENT,
	BG,
	CARD,
	DIM,
	FG,
	SlideContext,
	block_height,
	bold,
	draw_lines,
	fit_text,
)

log = logging.getLogger(__name__)


# --- image helpers -----------------------------------------------------------


def load_figure(path: Path | None) -> Image.Image | None:
	"""Open a figure as RGB, flattening transparency onto white.

	Paper figures are drawn for white paper; compositing alpha onto black turns
	axes and labels into a black-on-black smear.
	"""
	if path is None or not Path(path).exists():
		return None
	try:
		fig = Image.open(path)
		if fig.mode in ("RGBA", "LA", "P"):
			fig = fig.convert("RGBA")
			flat = Image.new("RGB", fig.size, CARD)
			flat.paste(fig, mask=fig.split()[-1])
			return flat
		return fig.convert("RGB")
	except Exception as e:
		log.warning("Thumbnail figure %s unusable: %s", path, e)
		return None


def focal_point(img: Image.Image, grid: int = 64) -> tuple[float, float]:
	"""Where the content of a figure actually is, as (x, y) fractions.

	A blind centre-crop is wrong for paper figures often enough to matter: the
	first attempt at these layouts cropped the annotation off a robotics figure -
	the annotation being the entire subject of the paper - and beheaded the person
	in the frame. Both are the same bug, which is assuming the interesting part is
	in the middle.

	The heuristic is edge energy: run an edge filter over a small copy and take the
	centroid of what lights up. Plots concentrate energy on the data rather than
	the whitespace, photos on the subject rather than the wall. A uniformly busy
	image lands back at the centre, which is the right default anyway.
	"""
	try:
		small = img.convert("L").resize((grid, grid), Image.BILINEAR)
		# The filter fires on the frame border itself; drop it before measuring.
		edges = small.filter(ImageFilter.FIND_EDGES).crop((1, 1, grid - 1, grid - 1))
	except Exception:
		return 0.5, 0.5

	px = list(edges.getdata())
	side = grid - 2
	total = sum(px)
	if total <= 0:
		return 0.5, 0.5
	sx = sum(v * (i % side) for i, v in enumerate(px))
	sy = sum(v * (i // side) for i, v in enumerate(px))
	return (sx / total) / (side - 1), (sy / total) / (side - 1)


def cover(
	img: Image.Image, w: int, h: int, focus: tuple[float, float] | None = None
) -> Image.Image:
	"""Resize and crop so the image completely fills `w x h`.

	The opposite of `Image.thumbnail`, which fits and leaves gaps. The crop window
	is centred on `focus` (defaulting to the detected focal point) and clamped to
	the image, so the part worth seeing survives.
	"""
	w, h = max(int(w), 1), max(int(h), 1)
	if img.width < 1 or img.height < 1:
		return Image.new("RGB", (w, h), CARD)

	fx, fy = focus if focus is not None else focal_point(img)
	scale = max(w / img.width, h / img.height)
	resized = img.resize(
		(max(int(img.width * scale + 0.5), w), max(int(img.height * scale + 0.5), h)),
		Image.LANCZOS,
	)
	left = int(min(max(fx * resized.width - w / 2, 0), resized.width - w))
	top = int(min(max(fy * resized.height - h / 2, 0), resized.height - h))
	return resized.crop((left, top, left + w, top + h))


def retained_fraction(img: Image.Image, w: int, h: int) -> float:
	"""How much of `img` would survive a `cover` into `w x h`."""
	if img.width < 1 or img.height < 1:
		return 0.0
	scale = max(w / img.width, h / img.height)
	return min((w * h) / (scale * scale * img.width * img.height), 1.0)


def place(img: Image.Image, w: int, h: int, min_retained: float = 0.45) -> Image.Image:
	"""Fill `w x h` with `img`, cropping only when cropping is not destructive.

	`cover` is right for a photo or a single plot. It is badly wrong for the
	figures papers are actually full of: a tall multi-panel comic cropped to a
	wide banner becomes three severed speech bubbles, which tells a viewer
	nothing. When the crop would throw away more than `min_retained` of the
	source, fall back to fitting the whole figure on a light card - the same card
	the slides use, and the same reason: paper figures are drawn for white paper.
	"""
	w, h = max(int(w), 1), max(int(h), 1)
	if retained_fraction(img, w, h) >= min_retained:
		return cover(img, w, h)

	panel = Image.new("RGB", (w, h), CARD)
	fitted = img.copy()
	pad = int(min(w, h) * 0.04)
	fitted.thumbnail((max(w - 2 * pad, 1), max(h - 2 * pad, 1)), Image.LANCZOS)
	panel.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
	return panel


def scrim(
	img: Image.Image,
	box: tuple[int, int, int, int],
	*,
	horizontal: bool,
	start: int = 240,
	end: int = 0,
) -> None:
	"""Paint a linear fade of the background colour over `box`, in place.

	This is what makes type survive over a figure: without it a headline lands on
	whatever the figure happens to have there, which for a plot is white.
	"""
	x1, y1, x2, y2 = box
	w, h = max(x2 - x1, 1), max(y2 - y1, 1)
	steps = w if horizontal else h
	mask = Image.new("L", (w, h))
	md = ImageDraw.Draw(mask)
	for i in range(steps):
		alpha = int(start + (end - start) * (i / max(steps - 1, 1)))
		if horizontal:
			md.line([(i, 0), (i, h)], fill=alpha)
		else:
			md.line([(0, i), (w, i)], fill=alpha)
	img.paste(Image.new("RGB", (w, h), BG), (x1, y1), mask)


def _kicker(d: ImageDraw.ImageDraw, ctx: SlideContext, text: str, x: int, y: int) -> int:
	if not text:
		return y
	kf = bold(ctx.scaled(34))
	d.text((x, y), text.upper(), font=kf, fill=ACCENT)
	return y + ctx.scaled(66)


def _headline(
	d: ImageDraw.ImageDraw,
	ctx: SlideContext,
	text: str,
	x: int,
	y: int,
	max_w: int,
	max_h: int,
	start: int = 168,
) -> int:
	"""Largest type that fits, drawn tight. Leading is deliberately below the
	slide value: a two-line thumbnail headline should read as one block."""
	font, lines = fit_text(d, text or "", _BOLD, max_w, max_h, start=ctx.scaled(start))
	return draw_lines(d, lines, font, x, y, FG, 1.08)


def _measure(d: ImageDraw.ImageDraw, ctx: SlideContext, text: str, max_w, max_h, start=168):
	font, lines = fit_text(d, text or "", _BOLD, max_w, max_h, start=ctx.scaled(start))
	return font, lines, block_height(lines, font, 1.08)


# --- layouts -----------------------------------------------------------------


def split(
	ctx: SlideContext, text: str, figure: Path | None = None, kicker: str = ""
) -> Image.Image:
	"""Text left, figure right - the current Stage 9 shape, but the figure fills
	its panel instead of floating in white space."""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	m = ctx.scaled(64)

	fig = load_figure(figure)
	text_w = ctx.width - 2 * m
	if fig is not None:
		panel_w = int(ctx.width * 0.46)
		x0 = ctx.width - panel_w
		img.paste(place(fig, panel_w, ctx.height), (x0, 0))
		# A hard accent edge where the panel meets the text side; without it the
		# figure's white bleeds into the dark half as a soft grey smudge.
		ImageDraw.Draw(img).rectangle([x0 - ctx.scaled(6), 0, x0, ctx.height], fill=ACCENT)
		text_w = x0 - m - ctx.scaled(48)

	d = ImageDraw.Draw(img)
	y = _kicker(d, ctx, kicker, m, m + ctx.scaled(18))
	_, _, block = _measure(d, ctx, text, text_w, ctx.height - y - m)
	_headline(d, ctx, text, m, max(y, int((ctx.height - block) / 2)), text_w, ctx.height - y - m)
	return img


def full_bleed(
	ctx: SlideContext, text: str, figure: Path | None = None, kicker: str = ""
) -> Image.Image:
	"""Figure across the whole frame, headline over a fade on the left."""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	m = ctx.scaled(64)

	fig = load_figure(figure)
	if fig is not None:
		if retained_fraction(fig, ctx.width, ctx.height) < 0.45:
			# Nothing legible survives a full-frame crop of this figure.
			return banner(ctx, text, figure, kicker)
		img.paste(cover(fig, ctx.width, ctx.height), (0, 0))
		# Opaque under the text, clear over the right third where the figure lives.
		scrim(img, (0, 0, int(ctx.width * 0.72), ctx.height), horizontal=True, start=252, end=0)
	else:
		return type_only(ctx, text, None, kicker)

	d = ImageDraw.Draw(img)
	text_w = int(ctx.width * 0.56) - m
	y = _kicker(d, ctx, kicker, m, m + ctx.scaled(18))
	_, _, block = _measure(d, ctx, text, text_w, ctx.height - y - m)
	_headline(d, ctx, text, m, max(y, int((ctx.height - block) / 2)), text_w, ctx.height - y - m)
	return img


def banner(
	ctx: SlideContext, text: str, figure: Path | None = None, kicker: str = ""
) -> Image.Image:
	"""Figure fills the top, headline sits in a solid band beneath it.

	The most legible of the four: the type never competes with the image, so it
	survives being shown at any size.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	m = ctx.scaled(64)

	fig = load_figure(figure)
	if fig is None:
		return type_only(ctx, text, None, kicker)

	band_h = int(ctx.height * 0.42)
	img_h = ctx.height - band_h
	img.paste(place(fig, ctx.width, img_h), (0, 0))

	d = ImageDraw.Draw(img)
	d.rectangle([0, img_h, ctx.width, img_h + ctx.scaled(7)], fill=ACCENT)

	y = img_h + ctx.scaled(7)
	inner = ctx.height - y
	tx = m
	if kicker:
		kf = bold(ctx.scaled(30))
		d.text((tx, y + ctx.scaled(26)), kicker.upper(), font=kf, fill=ACCENT)

	head_top = y + (ctx.scaled(76) if kicker else ctx.scaled(28))
	avail = ctx.height - head_top - ctx.scaled(26)
	font, lines, block = _measure(d, ctx, text, ctx.width - 2 * m, avail, start=124)
	draw_lines(d, lines, font, tx, head_top + max(0, (avail - block) // 2), FG, 1.08)
	_ = inner
	return img


def type_only(
	ctx: SlideContext, text: str, figure: Path | None = None, kicker: str = ""
) -> Image.Image:
	"""No figure: the words carry it. Also the fallback when a paper has no usable
	figure, which is why every other layout degrades to this one."""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(84)
	box = ctx.width - 2 * m

	font, lines, block = _measure(d, ctx, text, box, int(ctx.height * 0.62), start=208)
	rule_h = ctx.scaled(10)
	gap = ctx.scaled(40)
	total = rule_h + gap + block
	y = max(int((ctx.height - total) / 2), ctx.scaled(60))

	d.rectangle([m, y, m + ctx.scaled(150), y + rule_h], fill=ACCENT)
	y = draw_lines(d, lines, font, m, y + rule_h + gap, FG, 1.08)

	if kicker:
		kf = bold(ctx.scaled(32))
		d.text((m, y + ctx.scaled(26)), kicker.upper(), font=kf, fill=DIM)
	return img


LAYOUTS = {
	"split": split,
	"full_bleed": full_bleed,
	"banner": banner,
	"type_only": type_only,
}
