"""Finding the panels of a paper figure, so the camera can go to the one being discussed.

A paper figure is usually several figures: (a) a setup, (b) a plot, (c) an
ablation. Shown whole, the viewer is left to find on their own the part the
narration is about, which is the split-attention problem Mayer's signaling
principle exists for (D50). Shown one panel at a time, the same figure becomes
a guided tour, and a six-panel grid that v7 of the script prompt banned as
unreadable becomes six readable slides.

Panels are found by recursive whitespace cuts (an XY-cut, the classic
page-segmentation method): a figure is split along any full-height or
full-width gutter of background, and each part is split again. No model and no
OCR, so it runs on any machine in milliseconds, and when it finds nothing useful
it returns the whole figure, which is exactly what is drawn today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageChops

Box = tuple[int, int, int, int]  # left, top, right, bottom, in figure pixels

# A gutter narrower than this fraction of the figure is the space between a
# label and its axis, not between two panels.
_MIN_GUTTER = 0.012
# Panels smaller than this (as a fraction of the figure's area or side) are
# legends, axis titles or stray marks; they are merged back, never visited.
_MIN_AREA = 0.08
_MIN_SIDE = 0.2
_MAX_DEPTH = 2
_INK = 28  # grey-level difference from the background that counts as ink


@dataclass(frozen=True)
class Panel:
	box: Box

	@property
	def width(self) -> int:
		return self.box[2] - self.box[0]

	@property
	def height(self) -> int:
		return self.box[3] - self.box[1]

	@property
	def area(self) -> int:
		return self.width * self.height


def _ink_mask(img: Image.Image) -> Image.Image:
	"""1 where there is ink, 0 where there is background (the corner colour)."""
	rgb = img.convert("RGB")
	bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
	diff = ImageChops.difference(rgb, bg).convert("L")
	return diff.point(lambda v: 255 if v > _INK else 0)


def _profile(mask: Image.Image, box: Box, axis: int) -> list[bool]:
	"""For each column (axis=0) or row (axis=1) of `box`, whether it has ink."""
	crop = mask.crop(box)
	w, h = crop.size
	squashed = crop.resize((w, 1) if axis == 0 else (1, h), Image.BOX)
	return [v > 0 for v in squashed.tobytes()]


def _runs(ink: list[bool], min_gap: int) -> list[tuple[int, int]]:
	"""Spans of ink separated by gutters at least `min_gap` long."""
	spans: list[tuple[int, int]] = []
	start: int | None = None
	last = 0  # one past the last inked index of the open span
	for i, v in enumerate(ink):
		if not v:
			continue
		if start is not None and i - last >= min_gap:
			spans.append((start, last))
			start = None
		if start is None:
			start = i
		last = i + 1
	if start is not None:
		spans.append((start, last))
	return spans


def _tight(mask: Image.Image, box: Box) -> Box | None:
	bb = mask.crop(box).getbbox()
	if not bb:
		return None
	return (box[0] + bb[0], box[1] + bb[1], box[0] + bb[2], box[1] + bb[3])


def _cut(mask: Image.Image, box: Box, size: tuple[int, int], depth: int) -> list[Box]:
	box = _tight(mask, box) or box
	if depth >= _MAX_DEPTH:
		return [box]
	fw, fh = size
	for axis in (1, 0):  # rows first: reading order is top to bottom, then across
		length = fh if axis == 1 else fw
		spans = _runs(_profile(mask, box, axis), max(int(length * _MIN_GUTTER), 3))
		if len(spans) < 2:
			continue
		parts: list[Box] = []
		for a, b in spans:
			if axis == 1:
				parts.append((box[0], box[1] + a, box[2], box[1] + b))
			else:
				parts.append((box[0] + a, box[1], box[0] + b, box[3]))
		parts = _merge_small(parts, axis, size)
		if len(parts) < 2:
			continue
		out: list[Box] = []
		for p in parts:
			out += _cut(mask, p, size, depth + 1)
		return out
	return [box]


def _big_enough(b: Box, size: tuple[int, int]) -> bool:
	fw, fh = size
	w, h = b[2] - b[0], b[3] - b[1]
	return w * h >= _MIN_AREA * fw * fh and w >= _MIN_SIDE * fw and h >= _MIN_SIDE * fh


def _merge_small(parts: list[Box], axis: int, size: tuple[int, int]) -> list[Box]:
	"""Fold slivers (a caption row, a legend strip) into their neighbour.

	A title row above a plot is ink separated by a gutter, and visiting it on its
	own would be a zoom into three words. Merged into the panel it labels, it
	travels with it instead."""
	parts = list(parts)
	changed = True
	while changed and len(parts) > 1:
		changed = False
		for i, p in enumerate(parts):
			if _big_enough(p, size):
				continue
			j = i + 1 if i + 1 < len(parts) else i - 1
			if i > 0 and i + 1 < len(parts):
				# Prefer the closer neighbour.
				before = p[1] - parts[i - 1][3] if axis == 1 else p[0] - parts[i - 1][2]
				after = parts[i + 1][1] - p[3] if axis == 1 else parts[i + 1][0] - p[2]
				j = i - 1 if before <= after else i + 1
			q = parts[j]
			merged = (min(p[0], q[0]), min(p[1], q[1]), max(p[2], q[2]), max(p[3], q[3]))
			lo, hi = sorted((i, j))
			parts[lo : hi + 1] = [merged]
			changed = True
			break
	return parts


def find_panels(img: Image.Image) -> list[Panel]:
	"""The figure's panels in reading order. One panel means "no useful split"."""
	try:
		small = img
		scale = 1.0
		if max(img.size) > 1400:
			scale = 1400 / max(img.size)
			small = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
		mask = _ink_mask(small)
		size = small.size
		boxes = _cut(mask, (0, 0, *size), size, 0)
	except Exception:
		return [Panel((0, 0, img.width, img.height))]
	big = [b for b in boxes if _big_enough(b, size)]
	if not big:
		boxes = [(0, 0, *size)]
	else:
		# A sliver left at the deepest cut is still part of some panel - a row
		# label, a column of axis names. Fold it into the nearest one rather than
		# dropping it, or the camera crops it off.
		for b in boxes:
			if b in big:
				continue
			k = min(range(len(big)), key=lambda i: _gap(big[i], b))
			c = big[k]
			big[k] = (min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3]))
		boxes = big
	if scale != 1.0:
		boxes = [
			(int(b[0] / scale), int(b[1] / scale), int(b[2] / scale + 0.5), int(b[3] / scale + 0.5))
			for b in boxes
		]
	return [Panel(b) for b in boxes]


def pick(panels: list[Panel], ref: str | int | None) -> Panel | None:
	"""Resolve a script's panel reference: "a"/"b", "1"/"2", or an int (1-based).

	Unresolvable references return None and the caller shows the whole figure:
	pointing at the wrong panel is worse than pointing at none.
	"""
	if ref is None or len(panels) < 2:
		return None
	s = str(ref).strip().lower().strip("()")
	if s.isdigit():
		i = int(s) - 1
	elif len(s) == 1 and "a" <= s <= "z":
		i = ord(s) - ord("a")
	else:
		return None
	return panels[i] if 0 <= i < len(panels) else None


_LETTER = re.compile(r"\(([a-h])\)|(?:^|[\s,;])([a-h])\)")


def caption_letters(caption: str) -> list[str]:
	"""The panel letters a caption names, if they run a, b, c... without a hole."""
	found = {m.group(1) or m.group(2) for m in _LETTER.finditer((caption or "").lower())}
	letters: list[str] = []
	for c in "abcdefgh":
		if c not in found:
			break
		letters.append(c)
	return letters if len(letters) >= 2 else []


def _aligned(p: Box, q: Box, tol: float) -> bool:
	"""Stacked in one column, or side by side in one row."""
	same_col = abs(p[0] - q[0]) <= tol and abs(p[2] - q[2]) <= tol
	same_row = abs(p[1] - q[1]) <= tol and abs(p[3] - q[3]) <= tol
	return same_col or same_row


def _gap(p: Box, q: Box) -> float:
	dx = max(q[0] - p[2], p[0] - q[2], 0)
	dy = max(q[1] - p[3], p[1] - q[3], 0)
	return max(dx, dy)


def fit_to(panels: list[Panel], k: int, size: tuple[int, int]) -> list[Panel] | None:
	"""Merge over-split panels down to `k`, but only when there is one way to do it.

	Detection over-splits more often than it under-splits - a panel holding two
	worked examples is two blocks of ink with a gutter between them. Merging
	neighbours undoes that, but a 2x2 grid that should be three panels can be
	merged four ways and only one is right. A wrong merge points the camera at
	the wrong panel under the right letter, which is worse than no camera at
	all, so an ambiguous merge gives up and the figure is shown whole.
	"""
	boxes = [p.box for p in panels]
	tol = 0.04 * max(size)
	near = 0.06 * max(size)
	while len(boxes) > k:
		pairs = [
			(i, j)
			for i in range(len(boxes))
			for j in range(i + 1, len(boxes))
			if _aligned(boxes[i], boxes[j], tol) and _gap(boxes[i], boxes[j]) <= near
		]
		if len(pairs) != 1:
			return None
		i, j = pairs[0]
		p, q = boxes[i], boxes[j]
		boxes[i] = (min(p[0], q[0]), min(p[1], q[1]), max(p[2], q[2]), max(p[3], q[3]))
		del boxes[j]
	return [Panel(b) for b in boxes] if len(boxes) == k else None


def lettered_panels(img: Image.Image, caption: str) -> dict[str, Panel]:
	"""Panels keyed by the caption's letters, or {} when the two cannot be matched.

	The script model never sees the image; it reads "(b)" in the caption. So a
	panel is only offered under a letter when detection and caption agree on how
	many there are, and the camera can then go exactly where the narration points.
	"""
	letters = caption_letters(caption)
	if not letters:
		return {}
	fitted = fit_to(find_panels(img), len(letters), img.size)
	return dict(zip(letters, fitted, strict=True)) if fitted else {}


def offered_letters(figure_path, caption: str) -> list[str]:
	"""The letters the script may point the camera at, for one figure on disk."""
	try:
		with Image.open(figure_path) as im:
			if im.width * im.height > 60_000_000:
				return []
			return sorted(lettered_panels(im.convert("RGB"), caption))
	except Exception:
		return []
