"""Slide composition with Pillow.

The spec's visual system, implemented literally: dark background, a single accent
colour, large type, and paper figures on **light cards** with their source
attribution burned in. Figures are the star; everything else stays out of the way.

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

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# --- visual system -----------------------------------------------------------

BG = (15, 17, 21)  # near-black
FG = (233, 237, 243)
DIM = (146, 156, 170)
FAINT = (58, 64, 76)  # rules and inactive progress
ACCENT = (122, 162, 247)  # the single accent colour
CARD = (250, 250, 248)  # light card the figures sit on

MARGIN = 110

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

	def scaled(self, at_1080: float) -> int:
		"""Scale a 1080p-designed measurement to the configured frame height.

		Every constant here was chosen against 1920x1080. Without this, rendering
		at another size leaves absolute pixel offsets in the wrong places.
		"""
		return int(at_1080 * self.height / 1080)


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
		draw.text((m, ctx.height - ctx.scaled(84)), f"arXiv:{ctx.arxiv_id}", font=ff, fill=DIM)

	# Progress: a thin rule with the elapsed portion in the accent colour.
	if ctx.scene_total > 1:
		bar_w = ctx.scaled(260)
		x1, x2 = ctx.width - m - bar_w, ctx.width - m
		y = ctx.height - ctx.scaled(72)
		h = max(ctx.scaled(4), 2)
		draw.rectangle([x1, y, x2, y + h], fill=FAINT)
		frac = min(max((ctx.scene_index + 1) / ctx.scene_total, 0.0), 1.0)
		draw.rectangle([x1, y, x1 + int(bar_w * frac), y + h], fill=ACCENT)


# --- slide types -------------------------------------------------------------


def title_card(ctx: SlideContext, title: str, subtitle: str = "") -> Image.Image:
	"""Opens every segment, so it carries the paper's hook and nothing else."""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	font, lines = fit_text(d, title or "", _BOLD, box, ctx.scaled(460), start=ctx.scaled(96))
	sf = regular(ctx.scaled(36))
	sub_lines = wrap(d, subtitle, sf, box) if subtitle else []

	rule_h = ctx.scaled(8)
	gap = ctx.scaled(46)
	total = rule_h + gap + block_height(lines, font)
	if sub_lines:
		total += ctx.scaled(30) + block_height(sub_lines, sf, 1.35)

	# Optically centred: a title block reads better slightly above true centre.
	y = max(int((ctx.height - total) / 2) - ctx.scaled(30), ctx.scaled(120))

	d.rectangle([m, y, m + ctx.scaled(96), y + rule_h], fill=ACCENT)
	y = draw_lines(d, lines, font, m, y + rule_h + gap, FG)
	if sub_lines:
		draw_lines(d, sub_lines, sf, m, y + ctx.scaled(30), DIM, 1.35)

	chrome(d, ctx, show_eyebrow=False)
	return img


def bullet_slide(ctx: SlideContext, title: str, bullets: list[str]) -> Image.Image:
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m
	indent = ctx.scaled(52)

	bf = regular(ctx.scaled(46))
	shown = [b for b in bullets[:5] if b.strip()]
	wrapped = [wrap(d, b, bf, box - indent) for b in shown]
	spacing = ctx.scaled(30)

	tf = tl = None
	total = sum(block_height(w, bf) + spacing for w in wrapped)
	if title:
		tf, tl = fit_text(d, title, _BOLD, box, ctx.scaled(190), start=ctx.scaled(60))
		total += block_height(tl, tf) + ctx.scaled(52)

	y = max(int((ctx.height - total) / 2), ctx.scaled(200))
	if tf is not None:
		y = draw_lines(d, tl, tf, m, y, FG) + ctx.scaled(52)

	dot = ctx.scaled(15)
	for w in wrapped:
		cy = y + int(bf.size * 0.46)
		d.ellipse([m, cy, m + dot, cy + dot], fill=ACCENT)
		y = draw_lines(d, w, bf, m + indent, y, FG) + spacing

	chrome(d, ctx)
	return img


def result_callout(ctx: SlideContext, highlight: str, caption: str = "") -> Image.Image:
	"""A single number or finding, as large as it will go."""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)
	box = ctx.width - 2 * m

	font, lines = fit_text(d, highlight or "", _BOLD, box, ctx.scaled(430), start=ctx.scaled(132))
	cf = regular(ctx.scaled(34))
	cap_lines = wrap(d, caption, cf, int(box * 0.8)) if caption else []

	rule_h = ctx.scaled(6)
	total = block_height(lines, font, 1.2) + ctx.scaled(40) + rule_h
	if cap_lines:
		total += ctx.scaled(34) + block_height(cap_lines, cf, 1.35)
	y = max(int((ctx.height - total) / 2), ctx.scaled(150))

	for line in lines:
		w = d.textlength(line, font=font)
		d.text(((ctx.width - w) / 2, y), line, font=font, fill=ACCENT)
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


def figure_slide(
	ctx: SlideContext,
	figure_path: Path,
	attribution: str,
	title: str = "",
	caption: str = "",
) -> Image.Image:
	"""A paper figure on a light card, with its source burned in.

	The card matters: most paper figures are drawn for white paper, and dropping
	one straight onto a dark background renders black axes and text invisible.
	"""
	img = Image.new("RGB", (ctx.width, ctx.height), BG)
	d = ImageDraw.Draw(img)
	m = ctx.scaled(MARGIN)

	top = ctx.scaled(198) if title else ctx.scaled(140)
	if title:
		tf, lines = fit_text(
			d, title, _BOLD, ctx.width - 2 * m, ctx.scaled(100), start=ctx.scaled(46)
		)
		draw_lines(d, lines, tf, m, ctx.scaled(116), FG)

	# Reserve room under the card for a caption line when there is one.
	cf = regular(ctx.scaled(28))
	cap = truncate(d, caption, cf, ctx.width - 2 * m) if caption else ""
	bottom = ctx.height - (ctx.scaled(186) if cap else ctx.scaled(140))

	card = [m, top, ctx.width - m, bottom]
	d.rounded_rectangle(card, radius=ctx.scaled(18), fill=CARD)

	pad = ctx.scaled(32)
	attr_h = ctx.scaled(40)
	inner_w = max(card[2] - card[0] - 2 * pad, 1)
	inner_h = max(card[3] - card[1] - 2 * pad - attr_h, 1)

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
		fig.thumbnail((inner_w, inner_h), Image.LANCZOS)
		img.paste(
			fig,
			(
				card[0] + pad + (inner_w - fig.width) // 2,
				card[1] + pad + (inner_h - fig.height) // 2,
			),
		)
	except Exception as e:
		log.warning("Could not place figure %s: %s", figure_path, e)
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
		font=regular(ctx.scaled(25)),
		fill=(112, 112, 112),
	)
	if cap:
		d.text((m, bottom + ctx.scaled(22)), cap, font=cf, fill=DIM)

	chrome(d, ctx)
	return img


def render_visual(ctx: SlideContext, visual, scene_id: str = "") -> Image.Image:
	"""Dispatch a SceneManifest visual to its slide type."""
	vtype = visual.type
	if vtype == "figure" and visual.figure_file and ctx.figures_dir:
		path = ctx.figures_dir / Path(visual.figure_file).name
		num = "".join(c for c in Path(visual.figure_file).stem if c.isdigit()).lstrip("0") or "?"
		attribution = f"Figure {num}, arXiv:{ctx.arxiv_id}" if ctx.arxiv_id else f"Figure {num}"
		return figure_slide(ctx, path, attribution, visual.title or "", visual.highlight or "")
	if vtype == "result_callout":
		return result_callout(ctx, visual.highlight or visual.title or "", "")
	if vtype == "bullet_slide":
		return bullet_slide(ctx, visual.title or "", list(visual.bullets))
	return title_card(ctx, visual.title or ctx.segment_label, "")


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
		d.rounded_rectangle(card, radius=ctx.scaled(16), fill=CARD)
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
		d.text((m, y), kicker.upper(), font=kf, fill=ACCENT)
		y += ctx.scaled(58)

	font, lines = fit_text(d, text, _BOLD, text_w, ctx.height - y - m, start=ctx.scaled(104))
	block = block_height(lines, font, 1.15)
	y = max(y, int((ctx.height - block) / 2))
	draw_lines(d, lines, font, m, y, FG, 1.15)
	return img
