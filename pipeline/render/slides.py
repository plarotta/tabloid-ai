"""Slide composition with Pillow.

The spec's visual system: a **dark ground**, a **single accent colour**, large
type, and paper figures on light cards with their source attribution burned in.
Figures are the star; everything else stays out of the way.

This is episode one's look. D27 replaced it with a paper-white ground and one
accent per paper, answering an owner review of episode two; D35 put it back on
an owner review of episode three. Both palettes are recorded in the decision log
and the swap is confined to the constants below.

Every slide shares the same chrome so the frame reads as one system rather than
four unrelated layouts:

    eyebrow (which paper we are on)          top-left, dim
    [ content ]                              vertically centred
    arXiv id                    progress     bottom-left / bottom-right

Each slide is a pure function of (visual, context) -> PNG, which is what makes it
testable without rendering video: the tests assert on the produced image rather
than on a finished mp4.

Fonts are resolved from a candidate list so a missing font on another machine
degrades to Pillow's default rather than crashing the render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont

log = logging.getLogger(__name__)

# --- visual system -----------------------------------------------------------

# Episode one's palette, restored on the owner's review of the third (D35). The
# paper-white ground and the three per-paper accents that replaced it are D27,
# and their exact values are recorded there - putting them back is an edit to
# these six lines, not a rewrite.
#
# The ground is episode one's (15, 17, 21) lifted a little off pure black, which
# is D36: large flat fields of near-black read hard once bars and rules sit on
# them, and the animated comparison slides put a lot of both on screen.
BG = (22, 26, 33)  # near-black, lifted off pure black (D36)
FG = (233, 237, 243)
DIM = (146, 156, 170)
FAINT = (58, 64, 76)  # rules and inactive progress
ACCENT = (122, 162, 247)  # the single accent colour
CARD = (250, 250, 248)  # the light card figures sit on

# Kept as a list of one. D27 gave each paper its own hue off the running order;
# a single accent is what episode one had and what the owner asked for back. The
# list survives because Stage 8 indexes it by running order, and collapsing that
# to a constant would delete the seam a future per-paper palette hangs on.
PAPER_ACCENTS = [ACCENT]

# The wrapper wore a neutral graphite under D27 so it read as the frame rather
# than a fourth chapter. With one accent across the episode there is nothing for
# it to contrast against, so it takes the same accent as everything else.
SERIES_ACCENT = ACCENT


# The value a comparison is measured *against*, and the type that labels it.
# A sixth role, added by D36: with only ground/type/dim/faint/accent, a baseline
# bar had to borrow FAINT, which is the colour of rules and inactive chrome - so
# real data read as furniture. Warm, because the separation from the accent is
# the whole point and lightness alone was not carrying it.
BASE = (201, 139, 63)
BASE_TEXT = (232, 199, 154)


def accent_for(index: int | None) -> tuple[int, int, int]:
	"""Accent for the paper at `index` in running order; the wrapper is `None`.

	Both arms return the same colour under the single-accent palette. The
	branch is kept because it is the only place that knows the wrapper is not a
	paper, and that distinction outlives any particular palette.
	"""
	if index is None:
		return SERIES_ACCENT
	return PAPER_ACCENTS[index % len(PAPER_ACCENTS)]


MARGIN = 110


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
	"""Blend two palette colours. `t` is how much of `b` to take."""
	return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def tint(accent: tuple[int, int, int], t: float = 0.12) -> tuple[int, int, int]:
	"""The accent washed into the ground - a tinted panel, not a colour field.

	Used for the bridge card. At full strength a five-second accent field is a
	flashbang; on the plain ground it would be indistinguishable from a title
	card, which is the whole problem the bridge exists to solve.
	"""
	return mix(BG, accent, t)


_BOLD = [
	"/System/Library/Fonts/Supplemental/Arial Bold.ttf",
	"/System/Library/Fonts/Helvetica.ttc",
	"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_REGULAR = [
	"/System/Library/Fonts/Supplemental/Arial.ttf",
	"/System/Library/Fonts/Helvetica.ttc",
	"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
	for path in candidates:
		if Path(path).exists():
			try:
				return ImageFont.truetype(path, size)
			except OSError:
				continue
	log.warning("No system font found; falling back to Pillow's default (small)")
	return ImageFont.load_default()


def bold(size: int):
	return _font(_BOLD, size)


def regular(size: int):
	return _font(_REGULAR, size)


@dataclass(slots=True)
class SlideContext:
	"""Everything a slide may need beyond the visual itself."""

	width: int = 1920
	height: int = 1080
	arxiv_id: str = ""
	figures_dir: Path | None = None
	segment_label: str = ""
	# Chrome. `eyebrow` orients a viewer who joined mid-segment; the progress
	# pair drives the bar showing how far through the segment we are.
	eyebrow: str = ""
	scene_index: int = 0
	scene_total: int = 0
	# Which paper's colour this part wears. None is the wrapper (cold open,
	# outro), which stays neutral. Set by Stage 8 from the running order.
	paper_index: int | None = None
	# Pixels of the bottom edge reserved for burned-in captions, which are
	# composited later and know nothing about what a slide drew. Zero when
	# captions are off, and every layout then behaves exactly as before.
	caption_band: int = 0

	@property
	def accent(self) -> tuple[int, int, int]:
		return accent_for(self.paper_index)

	@property
	def content_height(self) -> int:
		"""The frame height a slide may actually use. Everything that centres
		vertically or anchors to the bottom measures against this, not `height`."""
		return self.height - self.caption_band

	def scaled(self, at_1080: float) -> int:
		"""Scale a 1080p-designed measurement to the configured frame height.

		Every constant here was chosen against 1920x1080. Without this, rendering
		at another size leaves absolute pixel offsets in the wrong places.
		"""
		return int(at_1080 * self.height / 1080)

	def fitted(self, at_1080: float) -> int:
		"""Like `scaled`, but measured against the height a slide may use.

		For the boxes that text is fitted into: reserving a caption band has to
		shrink them, or a long title merely wraps into the space the captions
		are about to occupy. Identical to `scaled` when captions are off.
		"""
		return int(at_1080 * self.content_height / 1080)


# --- text helpers ------------------------------------------------------------


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
	"""Greedy word wrap measured against the actual font."""
	words, lines, cur = text.split(), [], ""
	for w in words:
		trial = f"{cur} {w}".strip()
		if draw.textlength(trial, font=font) <= max_width or not cur:
			cur = trial
		else:
			lines.append(cur)
			cur = w
	if cur:
		lines.append(cur)
	return lines


def fit_text(
	draw: ImageDraw.ImageDraw,
	text: str,
	candidates: list[str],
	max_width: int,
	max_height: int,
	start: int,
	min_size: int = 24,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
	"""Largest size at which `text` wraps inside the box. Long titles shrink
	rather than overflowing the frame."""
	size = start
	while size > min_size:
		font = _font(candidates, size)
		lines = wrap(draw, text, font, max_width)
		if len(lines) * (size * 1.26) <= max_height:
			return font, lines
		size -= 4
	font = _font(candidates, min_size)
	return font, wrap(draw, text, font, max_width)


def draw_lines(draw, lines, font, x, y, fill, leading=1.26) -> int:
	for line in lines:
		draw.text((x, y), line, font=font, fill=fill)
		y += int(font.size * leading)
	return y


def block_height(lines: list[str], font, leading: float = 1.26) -> int:
	return int(len(lines) * font.size * leading)


def truncate(draw, text: str, font, max_width: int) -> str:
	"""Single-line ellipsis, for chrome that must not wrap."""
	if draw.textlength(text, font=font) <= max_width:
		return text
	while text and draw.textlength(text + "...", font=font) > max_width:
		text = text[:-1]
	return text.rstrip() + "..."


# --- shared chrome -----------------------------------------------------------


def chrome(draw: ImageDraw.ImageDraw, ctx: SlideContext, show_eyebrow: bool = True) -> None:
	"""Eyebrow, arXiv footer and progress bar. Identical on every slide type."""
	m = ctx.scaled(MARGIN)

	if show_eyebrow and ctx.eyebrow:
		ef = regular(ctx.scaled(26))
		draw.text(
			(m, ctx.scaled(74)),
			truncate(draw, ctx.eyebrow, ef, ctx.width - 2 * m),
			font=ef,
			fill=DIM,
		)

	if ctx.arxiv_id:
		ff = regular(ctx.scaled(25))
		draw.text(
			(m, ctx.content_height - ctx.scaled(84)), f"arXiv:{ctx.arxiv_id}", font=ff, fill=DIM
		)

	# Progress: a thin rule with the elapsed portion in the accent colour.
	if ctx.scene_total > 1:
		bar_w = ctx.scaled(260)
		x1, x2 = ctx.width - m - bar_w, ctx.width - m
		y = ctx.content_height - ctx.scaled(72)
		h = max(ctx.scaled(4), 2)
		draw.rectangle([x1, y, x2, y + h], fill=FAINT)
		frac = min(max((ctx.scene_index + 1) / ctx.scene_total, 0.0), 1.0)
		draw.rectangle([x1, y, x1 + int(bar_w * frac), y + h], fill=ctx.accent)


# --- slide types -------------------------------------------------------------


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
	"""Scale to fill w x h and centre-crop the overflow."""
	scale = max(w / img.width, h / img.height)
	img = img.resize(
		(max(int(img.width * scale), w), max(int(img.height * scale), h)), Image.LANCZOS
	)
	left, top = (img.width - w) // 2, (img.height - h) // 2
	return img.crop((left, top, left + w, top + h))


def backdrop_ground(ctx: SlideContext, path: Path) -> Image.Image:
	"""A generated image, pushed back until it is a ground rather than a picture.

	Three things happen to it, and each is doing a job. It is desaturated most of
	the way, because the palette has exactly one accent (D35) and an image full of
	its own colours would compete with it for the only job colour has here. It is
	darkened, so the frame still reads as the same near-black ground as every
	other slide. And a left-to-right scrim takes the left third almost to flat
	ground, because the type is left-aligned and the image is only allowed to show
	where nothing is written.

	Falls back to the flat ground if the file will not open - a generated image is
	the one input here that arrived over a network.
	"""
	try:
		art = _cover(Image.open(path).convert("RGB"), ctx.width, ctx.height)
	except Exception as e:
		log.warning("Backdrop %s unusable (%s); using the flat ground", path, e)
		return Image.new("RGB", (ctx.width, ctx.height), BG)

	art = ImageEnhance.Color(art).enhance(0.35)
	art = ImageEnhance.Brightness(art).enhance(0.62)

	# One row of the gradient, stretched: the scrim varies across x only.
	row = Image.new("L", (ctx.width, 1))
	row.putdata(
		[
			int(255 * (0.93 - 0.63 * min(max((x / ctx.width - 0.18) / 0.62, 0.0), 1.0)))
			for x in range(ctx.width)
		]
	)
	scrim = row.resize((ctx.width, ctx.height))
	return Image.composite(Image.new("RGB", art.size, BG), art, scrim)


def title_card(
	ctx: SlideContext, title: str, subtitle: str = "", backdrop: Path | None = None
) -> Image.Image:
	"""Opens every segment, so it carries the paper's hook and nothing else."""
	img = (
		backdrop_ground(ctx, backdrop)
		if backdrop is not None
		else Image.new("RGB", (ctx.width, ctx.height), BG)
	)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	# Sized to command the frame. A title card holds for four seconds with nothing
	# else on it, so type that merely fits reads as an empty slide.
	font, lines = fit_text(d, title or "", _BOLD, box, ctx.fitted(620), start=ctx.scaled(124))
	sf = regular(ctx.scaled(36))
	sub_lines = wrap(d, subtitle, sf, box) if subtitle else []

	rule_h = ctx.scaled(10)
	gap = ctx.scaled(46)
	total = rule_h + gap + block_height(lines, font)
	if sub_lines:
		total += ctx.scaled(30) + block_height(sub_lines, sf, 1.35)

	# Optically centred: a title block reads better slightly above true centre.
	y = max(int((ctx.content_height - total) / 2) - ctx.scaled(30), ctx.scaled(120))

	d.rectangle([m, y, m + ctx.scaled(132), y + rule_h], fill=ctx.accent)
	y = draw_lines(d, lines, font, m, y + rule_h + gap, FG)
	if sub_lines:
		draw_lines(d, sub_lines, sf, m, y + ctx.scaled(30), DIM, 1.35)

	chrome(d, ctx, show_eyebrow=False)
	return img


def _tracked_width(draw, text: str, font, tracking: int) -> float:
	return sum(draw.textlength(c, font=font) for c in text) + tracking * max(len(text) - 1, 0)


def _draw_tracked(draw, text: str, font, x: float, y: float, fill, tracking: int) -> None:
	"""Letter-spaced text. Pillow has no tracking, and a kicker set solid reads as
	a label rather than as a mark."""
	for ch in text:
		draw.text((x, y), ch, font=font, fill=fill)
		x += draw.textlength(ch, font=font) + tracking


def transition_card(ctx: SlideContext, label: str, marker: str = "") -> Image.Image:
	"""The bridge between two parts of the episode.

	Deliberately the one slide type that does not sit on `BG`. Its job is to be
	unmistakably not-a-segment for the few seconds it holds: without a change of
	ground it would read as one more title card, which is exactly the "one long
	PowerPoint deck" the owner review described.

	The wash is the *incoming* paper's accent, so the colour of the next chapter
	arrives a beat before the chapter does.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), tint(ctx.accent))
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	font, lines = fit_text(d, label or "", _BOLD, box, ctx.fitted(300), start=ctx.scaled(84))
	mf = bold(ctx.scaled(30))
	tracking = ctx.scaled(7)

	rule_h = max(ctx.scaled(3), 1)
	gap = ctx.scaled(44)
	# More room under the label than above it: the rule has to clear descenders,
	# which the line-height alone does not guarantee.
	under = int(gap * 1.5)
	total = block_height(lines, font) + under + rule_h
	if marker:
		total += int(mf.size * 1.2) + gap
	y = max(int((ctx.content_height - total) / 2), ctx.scaled(120))

	if marker:
		_draw_tracked(
			d,
			marker,
			mf,
			(ctx.width - _tracked_width(d, marker, mf, tracking)) / 2,
			y,
			ctx.accent,
			tracking,
		)
		y += int(mf.size * 1.2) + gap

	for line in lines:
		w = d.textlength(line, font=font)
		d.text(((ctx.width - w) / 2, y), line, font=font, fill=FG)
		y += int(font.size * 1.26)

	# A rule that runs most of the frame, unlike the short accent mark a title
	# card opens with - the two must not be mistaken for each other.
	y += under
	rw = int(ctx.width * 0.62)
	d.rectangle([(ctx.width - rw) / 2, y, (ctx.width + rw) / 2, y + rule_h], fill=ctx.accent)

	chrome(d, ctx, show_eyebrow=False)
	return img


def bullet_slide(
	ctx: SlideContext, title: str, bullets: list[str], reveal: int | None = None
) -> Image.Image:
	"""A short list, set as numbered rows rather than as dots.

	The dotted version read as a default: a bullet glyph carries no information
	and every list on every deck has one. Numbering the rows says how many there
	are and how far through them the viewer is, and a hairline between rows gives
	the block a structure the eye can rest on. Both come free - the slide holds
	the same words in the same place (D39).

	Rows are measured before they are drawn, so a four-line bullet and a two-word
	one keep the rule centred between them rather than crowding one and stranding
	the other.

	`reveal` draws only the first N rows and leaves the rest blank. Every row is
	still measured, so the block sits where it will sit once the list is whole -
	which is the point: Stage 8 renders one of these per row and dissolves between
	them, and a layout that recentred on each pass would slide the text up the
	frame instead of adding to it.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	shown = [b.strip() for b in bullets[:5] if b.strip()]

	pad_top = ctx.scaled(26)  # air inside a row, above and below its text
	pad_bottom = ctx.scaled(30)
	rule_h = max(ctx.scaled(2), 1)

	tf = tl = None
	title_h = 0
	if title:
		tf, tl = fit_text(d, title, _BOLD, box, ctx.fitted(190), start=ctx.scaled(60))
		title_h = block_height(tl, tf) + ctx.scaled(58)

	# The body size is fitted to the rows rather than fixed. A fixed size is only
	# ever right for a full list: two short bullets at 46px left three quarters of
	# the frame empty and read as a slide with nothing on it (owner, Ep. 7). The
	# rows now take the room they are given - the largest size at which they still
	# fit the body box wins - so a two-row slide sets large and a five-row one
	# lands near where it always did.
	body_box = max(ctx.fitted(780) - title_h, ctx.scaled(120))

	def rows_at(size: int):
		f = regular(size)
		g = int(size * 2.1)  # the gutter tracks the body size, not the frame
		w = [wrap(d, b, f, box - g) for b in shown]
		h = [block_height(x, f) + pad_top + pad_bottom for x in w]
		return w, h, g

	lo, hi, best = ctx.scaled(38), ctx.scaled(104), ctx.scaled(38)
	while lo <= hi:
		mid = (lo + hi) // 2
		_, h, _ = rows_at(mid)
		if sum(h) + rule_h * max(len(h) - 1, 0) <= body_box:
			best, lo = mid, mid + 1
		else:
			hi = mid - 1

	bf = regular(best)
	wrapped, heights, gutter = rows_at(best)
	nf = bold(max(int(best * 0.56), 12))  # the row number, set small and letter-spaced
	tracking = max(ctx.scaled(3), 1)
	number_drop = int(best * 0.22)  # the number rides the first line, so it scales with it

	total = sum(heights) + rule_h * max(len(heights) - 1, 0) + title_h

	y = max(int((ctx.content_height - total) / 2), ctx.scaled(150))
	if tf is not None:
		y = draw_lines(d, tl, tf, m, y, FG) + ctx.scaled(58)

	for i, (w, h) in enumerate(zip(wrapped, heights, strict=True)):
		text_y = y + pad_top
		if reveal is None or i < reveal:
			# The number sits on the first line's optical centre, not on the row's:
			# on a three-line bullet a vertically centred index reads as detached.
			_draw_tracked(d, f"{i + 1:02d}", nf, m, text_y + number_drop, ctx.accent, tracking)
			draw_lines(d, w, bf, m + gutter, text_y, FG)
		y += h
		# The rule separates two rows, so it waits for the second of them.
		if i < len(heights) - 1:
			if reveal is None or i + 1 < reveal:
				d.rectangle([m, y, ctx.width - m, y + rule_h], fill=FAINT)
			y += rule_h

	chrome(d, ctx)
	return img


def result_callout(
	ctx: SlideContext, highlight: str, caption: str = "", reveal: bool = True
) -> Image.Image:
	"""A single number or finding, as large as it will go.

	`reveal=False` measures the number and then does not draw it, leaving the rule
	on an empty stage. Dissolving that into the drawn version lands the number
	rather than cutting to it, and because both passes measure the same text the
	rule does not move underneath it.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	font, lines = fit_text(d, highlight or "", _BOLD, box, ctx.fitted(430), start=ctx.scaled(132))
	cf = regular(ctx.scaled(34))
	cap_lines = wrap(d, caption, cf, int(box * 0.8)) if caption else []

	rule_h = ctx.scaled(6)
	total = block_height(lines, font, 1.2) + ctx.scaled(40) + rule_h
	if cap_lines:
		total += ctx.scaled(34) + block_height(cap_lines, cf, 1.35)
	y = max(int((ctx.content_height - total) / 2), ctx.scaled(150))

	for line in lines:
		if reveal:
			w = d.textlength(line, font=font)
			d.text(((ctx.width - w) / 2, y), line, font=font, fill=ctx.accent)
		y += int(font.size * 1.2)

	# Short centred rule under the number - anchors the block.
	rw = ctx.scaled(120)
	y += ctx.scaled(40)
	d.rectangle([(ctx.width - rw) / 2, y, (ctx.width + rw) / 2, y + rule_h], fill=FAINT)

	if cap_lines:
		y += ctx.scaled(34)
		for line in cap_lines:
			w = d.textlength(line, font=cf)
			d.text(((ctx.width - w) / 2, y), line, font=cf, fill=DIM)
			y += int(cf.size * 1.35)

	chrome(d, ctx)
	return img


def _trim_border(fig: Image.Image) -> Image.Image:
	"""Crop the uniform margin a paper figure is saved with.

	Figures are cropped for a page, not a frame: a typical one carries an inch of
	white on every side, and that margin is then scaled down with the content it
	surrounds, so the part worth seeing arrives smaller than the card it sits on.
	Trimming costs nothing - the border is one flat colour by definition, and what
	is removed carries no ink.

	Deliberately conservative. It measures against the corner pixel, keeps a thin
	pad so nothing touches the card edge, and returns the figure untouched unless
	the crop is both meaningful and sane. A figure that is mostly background - a
	scatter plot on white - must not be mistaken for a figure that is mostly
	margin, which is why the bounding box is taken over ink rather than over rows.
	"""
	try:
		flat = Image.new("RGB", fig.size, fig.convert("RGB").getpixel((0, 0)))
		box = ImageChops.difference(fig.convert("RGB"), flat).getbbox()
	except Exception:
		return fig
	if not box:
		return fig
	pad = max(int(min(fig.width, fig.height) * 0.01), 2)
	left, top, right, bottom = box
	left, top = max(left - pad, 0), max(top - pad, 0)
	right, bottom = min(right + pad, fig.width), min(bottom + pad, fig.height)
	w, h = right - left, bottom - top
	# Not worth a crop, or the result is a sliver: leave it alone.
	if w < fig.width * 0.25 or h < fig.height * 0.25:
		return fig
	if w > fig.width * 0.97 and h > fig.height * 0.97:
		return fig
	return fig.crop((left, top, right, bottom))


def figure_slide(
	ctx: SlideContext,
	figure_path: Path,
	attribution: str,
	title: str = "",
	caption: str = "",
	reveal: bool = True,
) -> Image.Image:
	"""A paper figure on a white card, with its source burned in.

	On the old near-black ground the card was doing rescue work: paper figures are
	drawn for white paper and their black axes vanished against it. On the paper
	ground the card is nearly the same value as the surround, so it now only marks
	the figure's edge - which is why it carries a hairline rule rather than
	relying on contrast alone.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)

	top = ctx.scaled(150) if title else ctx.scaled(104)
	if title:
		tf, lines = fit_text(
			d, title, _BOLD, ctx.width - 2 * m, ctx.fitted(84), start=ctx.scaled(40)
		)
		draw_lines(d, lines, tf, m, ctx.scaled(104), FG)

	# Reserve room under the card for a caption line when there is one.
	cf = regular(ctx.scaled(28))
	cap = truncate(d, caption, cf, ctx.width - 2 * m) if caption else ""
	bottom = ctx.content_height - (ctx.scaled(172) if cap else ctx.scaled(104))

	# The card runs to a tighter margin than the type does. Text needs a reading
	# margin; a figure only needs to not touch the edge, and every pixel given
	# back here is a pixel of figure (owner, Ep. 7).
	fm = ctx.scaled(72)
	pad = ctx.scaled(32)
	attr_h = ctx.scaled(40)
	max_card = [fm, top, ctx.width - fm, bottom]
	inner_w = max(max_card[2] - max_card[0] - 2 * pad, 1)
	inner_h = max(max_card[3] - max_card[1] - 2 * pad - attr_h, 1)

	fig = None
	try:
		fig = Image.open(figure_path)
		if fig.mode in ("RGBA", "LA", "P"):
			# Flatten transparency onto the card, not onto black.
			fig = fig.convert("RGBA")
			flat = Image.new("RGB", fig.size, CARD)
			flat.paste(fig, mask=fig.split()[-1])
			fig = flat
		else:
			fig = fig.convert("RGB")
		fig = _trim_border(fig)
		# `thumbnail` only ever shrinks, so a figure saved small arrived small and
		# sat marooned in the middle of the card. Scale to the card in both
		# directions, capped at 2x native: past that a raster figure turns to
		# porridge and reads worse than it did at half the size.
		scale = min(inner_w / fig.width, inner_h / fig.height, 2.0)
		if abs(scale - 1.0) > 0.01:
			fig = fig.resize(
				(max(int(fig.width * scale), 1), max(int(fig.height * scale), 1)),
				Image.LANCZOS,
			)
	except Exception as e:
		log.warning("Could not place figure %s: %s", figure_path, e)
		fig = None

	# **The card is cut to the figure, not the other way round.** A portrait
	# figure scaled to a landscape card is limited by height, and the card then
	# carries two columns of blank white either side of it - which reads as a
	# mistake rather than as a margin. Sizing the card to what it holds keeps the
	# figure exactly as large as it was and removes the emptiness around it.
	# Floored at the width the attribution line needs, since that is burned into
	# the card and must not wrap (Ep. 8).
	af = regular(ctx.scaled(25))
	attr_w = d.textlength(attribution or "", font=af) + 2 * pad
	if fig is not None:
		card_w = max(int(fig.width) + 2 * pad, int(attr_w), ctx.scaled(360))
		card_h = int(fig.height) + 2 * pad + attr_h
	else:
		card_w = max_card[2] - max_card[0]
		card_h = max_card[3] - max_card[1]
	card_w = min(card_w, max_card[2] - max_card[0])
	card_h = min(card_h, max_card[3] - max_card[1])
	# Centred horizontally; hung from the top of the space the card may use, so a
	# short figure does not float in the middle of the frame with the title
	# stranded above it.
	cx = (ctx.width - card_w) // 2
	card = [cx, top, cx + card_w, top + card_h]

	d.rounded_rectangle(
		card, radius=ctx.scaled(18), fill=CARD, outline=FAINT, width=max(ctx.scaled(2), 1)
	)

	if fig is not None:
		img.paste(
			fig,
			(
				card[0] + (card_w - fig.width) // 2,
				card[1] + pad,
			),
		)
	else:
		d.text(
			(card[0] + pad, card[1] + pad),
			"[figure unavailable]",
			font=regular(ctx.scaled(32)),
			fill=(140, 140, 140),
		)

	# Source attribution, burned into the card (spec Stage 8).
	d.text(
		(card[0] + pad, card[3] - ctx.scaled(42)),
		attribution,
		font=af,
		fill=(112, 112, 112),
	)
	# Sits under the card rather than at a fixed height, because the card is now
	# cut to its figure and a short one ends well above `bottom`. Measured either
	# way, so withholding it for one reveal state moves nothing but the caption.
	if cap and reveal:
		d.text((m, card[3] + ctx.scaled(22)), cap, font=cf, fill=DIM)

	chrome(d, ctx)
	return img


def render_visual(
	ctx: SlideContext,
	visual,
	scene_id: str = "",
	backdrop: Path | None = None,
	reveal: int | None = None,
) -> Image.Image:
	"""Dispatch a SceneManifest visual to its slide type.

	`backdrop` is only ever read by the title card; every other slide type is
	dense enough already, and an image behind a figure would be competing with
	the one thing the format exists to show.

	`reveal` renders a partial state of the slide, for the staged reveals in
	`render/reveal.py`. `None` - the default everywhere except that module - is
	the whole slide, so nothing that does not ask for a reveal can get one. What
	the number means depends on the type: rows shown, for a bullet slide; whether
	the number or the caption is drawn yet, for the other two.
	"""
	vtype = visual.type
	if vtype == "transition":
		return transition_card(ctx, visual.title or "", visual.highlight or "")
	if vtype == "figure" and visual.figure_file and ctx.figures_dir:
		path = ctx.figures_dir / Path(visual.figure_file).name
		num = "".join(c for c in Path(visual.figure_file).stem if c.isdigit()).lstrip("0") or "?"
		attribution = f"Figure {num}, arXiv:{ctx.arxiv_id}" if ctx.arxiv_id else f"Figure {num}"
		return figure_slide(
			ctx,
			path,
			attribution,
			visual.title or "",
			visual.highlight or "",
			reveal=reveal is None or reveal >= 1,
		)
	if vtype == "result_callout":
		return result_callout(
			ctx,
			visual.highlight or visual.title or "",
			"",
			reveal=reveal is None or reveal >= 1,
		)
	if vtype == "bullet_slide":
		return bullet_slide(ctx, visual.title or "", list(visual.bullets), reveal=reveal)
	return title_card(ctx, visual.title or ctx.segment_label, "", backdrop)


def thumbnail(
	ctx: SlideContext,
	text: str,
	figure_path: Path | None = None,
	kicker: str = "",
) -> Image.Image:
	"""YouTube thumbnail: big text left, paper figure right on a light card.

	Generated from the template rather than an image model (spec Stage 9). Text
	is sized to survive being shown at a fraction of full size, which is the only
	thing that matters here.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(70)

	# Figure on the right half when there is one; text takes the rest.
	text_w = ctx.width - 2 * m
	if figure_path is not None and figure_path.exists():
		card_w = int(ctx.width * 0.42)
		card = [ctx.width - m - card_w, m, ctx.width - m, ctx.height - m]
		d.rounded_rectangle(
			card, radius=ctx.scaled(16), fill=CARD, outline=FAINT, width=max(ctx.scaled(2), 1)
		)
		pad = ctx.scaled(20)
		try:
			fig = Image.open(figure_path)
			if fig.mode in ("RGBA", "LA", "P"):
				fig = fig.convert("RGBA")
				flat = Image.new("RGB", fig.size, CARD)
				flat.paste(fig, mask=fig.split()[-1])
				fig = flat
			else:
				fig = fig.convert("RGB")
			fig.thumbnail((card_w - 2 * pad, card[3] - card[1] - 2 * pad), Image.LANCZOS)
			img.paste(
				fig,
				(
					card[0] + (card_w - fig.width) // 2,
					card[1] + ((card[3] - card[1]) - fig.height) // 2,
				),
			)
		except Exception as e:
			log.warning("Thumbnail figure %s unusable: %s", figure_path, e)
		text_w = card[0] - m - ctx.scaled(40)

	y = m + ctx.scaled(40)
	if kicker:
		kf = bold(ctx.scaled(30))
		d.text((m, y), kicker.upper(), font=kf, fill=ctx.accent)
		y += ctx.scaled(58)

	font, lines = fit_text(d, text, _BOLD, text_w, ctx.height - y - m, start=ctx.scaled(104))
	block = block_height(lines, font, 1.15)
	y = max(y, int((ctx.height - block) / 2))
	draw_lines(d, lines, font, m, y, FG, 1.15)
	return img
