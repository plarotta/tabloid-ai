"""Burned-in captions, because most of this format is watched muted.

Two decisions are worth stating up front.

**Timing is derived, not measured.** `cues_for` splits a scene's narration into
short phrases and gives each one a share of that scene's **measured** duration
proportional to its length in characters. ElevenLabs can return character-level
alignment from its `/with-timestamps` endpoint, but the macOS and OpenAI
adapters cannot, and a caption track that only lines up on one engine is worse
than one that is a fraction of a second loose on all three. Characters are a
good proxy at phrase length: the error is bounded by how far a phrase's speaking
rate strays from the scene's average, which for one voice reading prose is
small. The cues always tile the scene exactly, so error cannot accumulate across
a segment - each scene re-syncs to a measured boundary.

**One line, always.** Two-line captions would need a fifth of the frame held in
reserve on every slide, and the visual system makes figures the star (D27).
Short cues keep the reserve to ~16% and read better in a feed anyway.

Captions are composited *after* the slides are cross-faded and zoomed, so a
caption never dissolves with the slide under it and never drifts with the Ken
Burns move.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from ..tts.providers import strip_ssml
from .slides import BG, FAINT, FG, SlideContext, bold

# Cue length. `MAX_CHARS` is a soft ceiling - a cue may exceed it slightly when a
# runt is folded back into it, and the type shrinks to cover that.
MAX_CHARS = 36
MIN_CHARS = 14

# Layout, in 1080p units. `SlideContext.scaled` maps them to the real frame.
SIZE = 42
MIN_SIZE = 28
LEADING = 1.24
PAD_X = 40
PAD_Y = 22
BOTTOM = 54  # gap between the pill and the bottom of the frame
CLEARANCE = 20  # gap between the pill and whatever the slide draws above it
RADIUS = 14

# Not fully opaque: a caption that hides the figure under it is a worse trade
# than one that is slightly see-through.
PILL_ALPHA = 235

# A cue may end on a dash as well as on punctuation. Both dashes are spelled by
# codepoint (U+2014, U+2013) because set literally in a character class they are
# indistinguishable from a plain hyphen at a glance.
_ENDS_CLAUSE = re.compile("[.!?,;:\u2014\u2013]$")


@dataclass(slots=True)
class Cue:
	"""One caption, on screen from `start` to `end` seconds into the part."""

	text: str
	start: float
	end: float

	@property
	def seconds(self) -> float:
		return self.end - self.start


def split_phrases(text: str) -> list[str]:
	"""Break narration into caption-sized phrases.

	Words are packed up to `MAX_CHARS`, but a clause boundary is a better place
	to break than an arbitrary word boundary, so one is taken as soon as the cue
	is long enough to be worth showing on its own. SSML is stripped first: the
	narration handed to ElevenLabs carries `<break>` markup that must never
	reach the screen.
	"""
	words = " ".join(strip_ssml(text).split()).split()
	if not words:
		return []

	cues: list[str] = []
	cur = ""
	for i, word in enumerate(words):
		trial = f"{cur} {word}".strip()
		if cur and len(trial) > MAX_CHARS:
			cues.append(cur)
			cur = word
		else:
			cur = trial
		last = i == len(words) - 1
		if not last and len(cur) >= MIN_CHARS and _ENDS_CLAUSE.search(cur):
			cues.append(cur)
			cur = ""
	if cur:
		cues.append(cur)

	# A one- or two-word tail flashes on screen and reads as a glitch. Fold it
	# back into the cue before it and let the type shrink instead.
	if len(cues) > 1 and len(cues[-1]) < MIN_CHARS:
		tail = cues.pop()
		cues[-1] = f"{cues[-1]} {tail}"
	return cues


def cues_for(narration: str, duration: float, start: float = 0.0) -> list[Cue]:
	"""Time a scene's phrases across its measured duration.

	The cues tile `[start, start + duration)` exactly - the last one is snapped
	to the end rather than left wherever the division landed, so a scene's
	captions can never run past the scene or leave a gap before the next one.
	"""
	phrases = split_phrases(narration)
	if not phrases or duration <= 0:
		return []

	weights = [max(len(p), 1) for p in phrases]
	total = sum(weights)
	cues: list[Cue] = []
	at = start
	for phrase, weight in zip(phrases, weights, strict=True):
		end = at + duration * weight / total
		cues.append(Cue(phrase, at, end))
		at = end
	cues[-1].end = start + duration
	return cues


def band_height(ctx: SlideContext) -> int:
	"""How much of the frame's bottom edge the caption zone owns.

	Slides are composed against `ctx.height - band_height(...)` so nothing they
	draw ends up underneath a caption. Passed to `SlideContext.caption_band`.
	"""
	return ctx.scaled(BOTTOM + 2 * PAD_Y + int(SIZE * LEADING) + CLEARANCE)


def caption_image(ctx: SlideContext, text: str) -> Image.Image:
	"""One cue as a full-frame RGBA overlay; empty text gives a clear frame.

	Full-frame rather than a cropped strip so the compositor can overlay it at
	0:0 without tracking where the pill ended up. Transparent PNG rows cost
	almost nothing on disk.
	"""
	img = Image.new("RGBA", (ctx.width, ctx.height), (0, 0, 0, 0))
	# Mid-sentence punctuation is a break the *split* needed, not something the
	# viewer needs to see; a cue ending on a dangling comma reads as a typo. A
	# full stop or a question mark stays, because it closes the thought.
	text = " ".join(text.split()).rstrip(",;:\u2014\u2013").strip()
	if not text:
		return img

	d = ImageDraw.Draw(img)
	pad_x, pad_y = ctx.scaled(PAD_X), ctx.scaled(PAD_Y)
	max_text = int(ctx.width * 0.86) - 2 * pad_x

	# Shrink rather than wrap: this is a one-line format by construction, and a
	# cue that overruns is a folded-in runt, not a sentence.
	size = max(ctx.scaled(SIZE), 1)
	floor = max(ctx.scaled(MIN_SIZE), 1)
	step = max(ctx.scaled(2), 1)
	font = bold(size)
	while size > floor and d.textlength(text, font=font) > max_text:
		size -= step
		font = bold(size)

	text_w = d.textlength(text, font=font)
	pill_w = int(text_w) + 2 * pad_x
	pill_h = int(font.size * LEADING) + 2 * pad_y
	x0 = (ctx.width - pill_w) // 2
	y1 = ctx.height - ctx.scaled(BOTTOM)
	y0 = y1 - pill_h

	d.rounded_rectangle(
		[x0, y0, x0 + pill_w, y1],
		radius=ctx.scaled(RADIUS),
		fill=(*BG, PILL_ALPHA),
		outline=(*FAINT, PILL_ALPHA),
		width=max(ctx.scaled(2), 1),
	)
	d.text((x0 + pad_x, y0 + pad_y), text, font=font, fill=(*FG, 255))
	return img
