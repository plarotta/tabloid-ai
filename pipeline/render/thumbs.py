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
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .panels import find_panels
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


# --- hero: the D50 thumbnail ---------------------------------------------------

_NUMBER = re.compile(r"^\s*([~<>]?\d[\d,.]*\s?(?:%|x|\u00d7|X)?)\s+(.*)$")


def parse_hook(text: str) -> tuple[str, str, set[int]]:
	"""Split "80% *never* read the files" into ("80%", "never read the files", {0}).

	The number, if the hook leads with one, is set separately and huge. A word
	wrapped in asterisks is the one to highlight; the asterisks never reach the
	frame. Returns (number, words, indices of highlighted words).
	"""
	text = " ".join((text or "").split())
	number = ""
	m = _NUMBER.match(text)
	if m:
		number, text = m.group(1).replace(" ", ""), m.group(2)
	words, marked = [], set()
	for i, w in enumerate(text.split()):
		if w.startswith("*") and w.rstrip(".,!").endswith("*") and len(w.strip("*")) > 0:
			marked.add(i)
		words.append(w.replace("*", ""))
	return number, " ".join(words), marked


def _fit_words(d, words: list[str], max_w: int, max_h: int, start: int, lines_max: int = 3):
	"""Largest bold size at which `words` wrap into <= lines_max lines inside the box."""
	size = start
	while size > 24:
		font = bold(size)
		lines: list[list[int]] = [[]]
		for i in range(len(words)):
			trial = " ".join(words[j] for j in [*lines[-1], i])
			if lines[-1] and d.textlength(trial, font=font) > max_w:
				lines.append([i])
			else:
				lines[-1].append(i)
		widest = max(d.textlength(" ".join(words[j] for j in ln), font=font) for ln in lines)
		if len(lines) <= lines_max and widest <= max_w and len(lines) * size * 1.08 <= max_h:
			return font, lines
		size = int(size * 0.94)
	return bold(size), [list(range(len(words)))]


def hero(
	ctx: SlideContext,
	text: str,
	figure: Path | None = None,
	kicker: str = "",
	accent: tuple[int, int, int] | None = None,
	panel: tuple[int, int, int, int] | None = None,
) -> Image.Image:
	"""One focal element, readable at 168 pixels wide (D50).

	A thumbnail is judged at phone size in a fraction of a second, so it gets one
	idea: a number set as large as the frame allows, or two to four words with
	one of them on a highlighter block. The figure is never the whole page - at
	feed size a paper figure is grey texture - but one panel, cropped to fill
	its side of the frame on the paper-white it was drawn for.
	"""
	accent = accent or ACCENT
	ink = BG
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(56)

	fig = load_figure(figure)
	text_right = ctx.width - m
	if fig is not None:
		if panel is None:
			# No panel the camera visited: any detected panel will do here. A
			# thumbnail needs a legible region, not the panel the narration meant,
			# and the whole page at 168 pixels wide is grey texture.
			found = find_panels(fig)
			if len(found) > 1:
				panel = found[0].box
		if panel:
			fig = fig.crop(panel)
		panel_w = int(ctx.width * 0.44)
		x0 = ctx.width - panel_w
		img.paste(place(fig, panel_w, ctx.height, min_retained=0.35), (x0, 0))
		d.rectangle([x0 - ctx.scaled(12), 0, x0, ctx.height], fill=accent)
		text_right = x0 - ctx.scaled(44)

	number, words, marked = parse_hook(text)
	tokens = words.split()
	box_w = text_right - m
	y = m
	if kicker:
		kf = bold(ctx.scaled(30))
		d.text((m, y), kicker.upper(), font=kf, fill=accent)
		y += ctx.scaled(54)

	avail = ctx.height - y - m
	blocks = []
	if number:
		nf = fit_text(d, number, _BOLD, box_w, int(avail * 0.58), start=ctx.scaled(300))[0]
		nb = d.textbbox((0, 0), number, font=nf)
		blocks.append(("num", nf, nb))
		avail -= (nb[3] - nb[1]) + ctx.scaled(26)
	if tokens:
		wf, lines = _fit_words(
			d, tokens, box_w, avail, ctx.scaled(150 if number else 170), 2 if number else 3
		)
		blocks.append(("words", wf, lines))

	def height(b):
		if b[0] == "num":
			return b[2][3] - b[2][1]
		return int(len(b[2]) * b[1].size * 1.1)

	total = sum(height(b) for b in blocks) + (ctx.scaled(26) if len(blocks) == 2 else 0)
	y = max(y, (ctx.height - total) // 2)
	for b in blocks:
		if b[0] == "num":
			_, nf, nb = b
			d.text((m - nb[0], y - nb[1]), number, font=nf, fill=accent)
			y += (nb[3] - nb[1]) + ctx.scaled(26)
			continue
		_, wf, lines = b
		for ln in lines:
			x = m
			for j in ln:
				w = tokens[j]
				tw = d.textlength(w, font=wf)
				if j in marked:
					pad = int(wf.size * 0.12)
					bb = d.textbbox((x, y), w, font=wf)
					d.rectangle([bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad], fill=accent)
					d.text((x, y), w, font=wf, fill=ink)
				else:
					d.text((x, y), w, font=wf, fill=FG)
				x += tw + d.textlength(" ", font=wf)
			y += int(wf.size * 1.1)
	return img


def preview_sheet(images: list[Image.Image], labels: list[str]) -> Image.Image:
	"""Every option at the three sizes a viewer meets it: home feed (360x202),
	search (246x138) and the phone's up-next rail (168x94), on YouTube's dark UI.
	The decision is made on this sheet, not on the 1280x720 originals."""
	sizes = [(360, 202), (246, 138), (168, 94)]
	pad = 24
	row_h = sizes[0][1] + pad
	sheet = Image.new(
		"RGB",
		(pad + sum(w + pad for w, _ in sizes) + 40, pad + row_h * len(images) + 10),
		(15, 15, 15),
	)
	d = ImageDraw.Draw(sheet)
	for r, (im, label) in enumerate(zip(images, labels, strict=True)):
		x = pad
		y = pad + r * row_h
		for w, h in sizes:
			sheet.paste(im.resize((w, h), Image.LANCZOS), (x, y))
			x += w + pad
		d.text((x, y), label, fill=(170, 170, 170))
	return sheet
