"""The animated-callout templates, rendered by manim.

This module is **executed by the manim CLI in a subprocess**, never imported by
the pipeline. `animate.py` writes a spec to a temp file, points
`TABLOID_CLIP_SPEC` at it and shells out; that keeps manim's global config, its
import cost and its failure modes out of the render stage entirely, and makes a
timeout enforceable.

Twenty-one templates, and the model may only choose between them and fill
their parameters (`schemas.Comparison`). It never writes animation code: a scene
that compiles, reads well and matches its narration is hard to generate and
impossible to check by eye at scale, while a handful of numbers and labels can
be validated in full. See DECISIONS.md D36, D37 and D46.

Two rules hold across every template:

  - **Nothing from the MathTex family.** `DecimalNumber`, `Tex`, `MathTex` and
    friends render through LaTeX, which would put a ~1GB TeX install on the CI
    runner to draw three digits. Numbers are Pango `Text` rebuilt per frame.
  - **The clip is exactly as long as it is told to be.** Stage 8 syncs video to
    *measured* audio, so a clip that runs its own length would break the one
    invariant the render is built on (D20).
"""

from __future__ import annotations

import itertools
import json
import math
import os
import textwrap

from manim import (
	DOWN,
	LEFT,
	PI,
	RIGHT,
	UP,
	Arrow,
	BraceBetweenPoints,
	Create,
	CurvedArrow,
	DashedLine,
	Dot,
	Ellipse,
	FadeIn,
	GrowArrow,
	GrowFromCenter,
	GrowFromEdge,
	LaggedStart,
	Line,
	MoveAlongPath,
	Rectangle,
	RoundedRectangle,
	Scene,
	Text,
	ValueTracker,
	VGroup,
	VMobject,
	always_redraw,
	config,
	rate_functions,
)

# Absolute, not relative: manim loads this file by path, so it has no package
# context and `from .slides import ...` raises. The project is installed, so the
# palette still comes from one place rather than being copied here.
from pipeline.render.slides import ACCENT, BASE, BASE_TEXT, BG, DIM, FAINT, FG, MARGIN, mix

SPEC: dict = json.loads(os.environ.get("TABLOID_CLIP_SPEC_JSON") or "{}")

# Measured against Pillow at 1920x1080: a manim `font_size` of F renders Arial at
# about 0.534x the pixel em size Pillow draws at. Without this the chrome on an
# animated slide would merely resemble the chrome on a static one.
FONT_RATIO = 0.534
BOLD = REGULAR = "Arial"


def hexof(rgb) -> str:
	return "#" + "".join(f"{c:02x}" for c in rgb)


def px_per_unit() -> float:
	return config.pixel_height / config.frame_height


def fs(px: float) -> float:
	return px * FONT_RATIO * (1080 / config.pixel_height) * (config.pixel_height / 1080)


def x_at(px: float) -> float:
	"""Screen x for a pixel column measured from the left edge."""
	return -config.frame_width / 2 + px / px_per_unit()


def y_at(px: float) -> float:
	"""Screen y for a pixel row measured from the top edge."""
	return config.frame_height / 2 - px / px_per_unit()


def scaled(at_1080: float) -> float:
	"""Mirror of SlideContext.scaled: constants were chosen against 1080p."""
	return at_1080 * config.pixel_height / 1080


def fmt(v: float) -> str:
	if abs(v - round(v)) < 1e-9:
		return f"{round(v):,}" if abs(v) >= 10000 else str(round(v))
	return f"{v:.1f}"


TEMPLATES = frozenset(
	{
		"two_bar",
		"count_up",
		"split",
		"decay_curve",
		"delta",
		"gap",
		"range",
		"multiplier",
		"fraction",
		"waffle",
		"ranking",
		"funnel",
		"trend",
		"stat_row",
		"scale",
		"stack",
		"slope",
		"threshold",
		"flow",
		"cycle",
		"versus",
	}
)

# Series colours past the first two. The accent is the paper's value and BASE
# what it is measured against (D36); a third and fourth series take muted mixes
# of those two rather than new hues, so a five-part stack still reads as one
# palette.
SERIES = [ACCENT, BASE, DIM, mix(ACCENT, BG, 0.45), mix(BASE, BG, 0.45)]
SERIES_TEXT = [ACCENT, BASE_TEXT, FG, mix(ACCENT, FG, 0.4), BASE_TEXT]

# Units short enough to sit against the number. Anything longer is a phrase
# ("tokens per parameter") and goes in the heading instead of after every value.
_SUFFIX = {"%": "%", "x": "\u00d7", "\u00d7": "\u00d7", "pp": " pts", "pts": " pts"}

MINUS = "\u2212"

_RELATION = {
	"!=": "\u2260",
	"vs": "vs",
	"->": "\u2192",
	">": ">",
	"<": "<",
	"~": "\u2248",
	"-": "\u2212",
	"+": "+",
}


def places(v: float) -> int:
	"""Decimal places `v` is written to, so a counter lands on the value as stated."""
	s = f"{abs(v):g}"
	if "e" in s:
		return 2
	return min(len(s.split(".")[1]), 3) if "." in s else 0


def num(v: float, like: float | None = None) -> str:
	"""`v` to the precision of `like` (default: its own), thousands separated."""
	p = places(v if like is None else like)
	return f"{v:,.{p}f}" if abs(v) >= 1000 else f"{v:.{p}f}"


def suffix() -> str:
	u = SPEC.get("unit", "")
	if u in _SUFFIX:
		return _SUFFIX[u]
	return f" {u}" if u and len(u) <= 3 else ""


def val(v: float, like: float | None = None) -> str:
	return num(v, like) + suffix()


def long_unit() -> str:
	"""The unit when it is a phrase rather than a suffix, for the heading."""
	u = SPEC.get("unit", "")
	return "" if u in _SUFFIX or len(u) <= 3 else u


def nice_ceil(x: float) -> float:
	"""The next round number at or above `x`: 1, 2, 2.5 or 5 times a power of ten."""
	if x <= 0:
		return 1.0
	mag = 10 ** math.floor(math.log10(x))
	for m in (1, 2, 2.5, 5, 10):
		if m * mag >= x - 1e-12:
			return m * mag
	return 10 * mag


def bounds(values: list[float]) -> tuple[float, float]:
	"""The axis a value sits on. Fixed by `scale`, never chosen by the model.

	`auto` always starts at zero: an axis that starts wherever the data does makes
	a two-point difference fill the frame, which is the same overstatement as an
	invented baseline (D46).
	"""
	kind = SPEC.get("scale", "auto")
	fixed = {"percent": (0.0, 100.0), "unit": (0.0, 1.0), "signed_unit": (-1.0, 1.0)}
	if kind in fixed:
		return fixed[kind]
	return 0.0, nice_ceil(max(values) * 1.08)


def text(s: str, size: float, colour=FG) -> Text:
	return Text(s, font=REGULAR, font_size=fs(size), color=hexof(colour))


def fit(m, width: float):
	"""Shrink `m` to `width` if it is wider. Never grows it."""
	if m.width > width:
		m.scale_to_fit_width(width)
	return m


def wrap(s: str, chars: int) -> str:
	return "\n".join(textwrap.wrap(s, max(chars, 6))) or s


def left_edge() -> float:
	return x_at(scaled(MARGIN))


def right_edge() -> float:
	return x_at(config.pixel_width - scaled(MARGIN))


def spread(ys: list[float], gap: float) -> list[float]:
	"""Push label positions apart until no two are closer than `gap`.

	Order is preserved: labels are laid out from the top down and only ever
	moved down, so the label for the higher value stays above.
	"""
	order = sorted(range(len(ys)), key=lambda i: -ys[i])
	out = list(ys)
	for prev, cur in itertools.pairwise(order):
		if out[prev] - out[cur] < gap:
			out[cur] = out[prev] - gap
	return out


class Beats:
	"""Fits a template's animation into the duration it was given.

	Templates are written with natural run times. If they do not fit the clip -
	a short scene, or a long note to read - every beat is scaled down together
	rather than the last one being clipped, so the shape of the animation
	survives at any length. Whatever is left over becomes a hold at the end,
	which is where a viewer reads the finished frame.
	"""

	def __init__(self, duration: float, budget: float = 0.62) -> None:
		self.duration = duration
		self.budget = budget
		self.scale = 1.0
		self.spent = 0.0

	def plan(self, total_natural: float) -> None:
		room = self.duration * self.budget
		if total_natural > room > 0:
			self.scale = room / total_natural

	def rt(self, natural: float) -> float:
		t = max(natural * self.scale, 1 / 30)
		self.spent += t
		return t

	def hold(self, natural: float) -> float:
		t = max(natural * self.scale, 0.0)
		self.spent += t
		return t

	def remainder(self) -> float:
		return max(self.duration - self.spent, 0.0)


class Clip(Scene):
	"""One animated callout. The template is chosen by the spec, not by name, so
	the manim CLI invocation is identical whatever is being drawn."""

	def construct(self) -> None:
		self.camera.background_color = hexof(SPEC.get("bg", (22, 26, 33)))
		self.add(*self._chrome())
		name = SPEC["template"]
		if name not in TEMPLATES:
			raise ValueError(f"unknown template {name!r}")
		getattr(self, name)()

	# --- chrome, matching slides.chrome() --------------------------------

	def _chrome(self) -> list:
		parts = []
		left = x_at(scaled(MARGIN))

		if SPEC.get("eyebrow"):
			parts.append(
				Text(SPEC["eyebrow"], font=REGULAR, font_size=fs(26), color=hexof(DIM))
				.move_to([0, y_at(scaled(74 + 13)), 0])
				.align_to([left, 0, 0], LEFT)
			)
		if SPEC.get("arxiv_id"):
			parts.append(
				Text(
					f"arXiv:{SPEC['arxiv_id']}",
					font=REGULAR,
					font_size=fs(25),
					color=hexof(DIM),
				)
				.move_to([0, y_at(scaled(996 + 12)), 0])
				.align_to([left, 0, 0], LEFT)
			)

		total = SPEC.get("scene_total", 0)
		if total > 1:
			bar_w = scaled(260) / px_per_unit()
			bar_h = max(scaled(4), 2) / px_per_unit()
			y = y_at(scaled(1008) + scaled(4) / 2)
			right = x_at(config.pixel_width - scaled(MARGIN))
			track = Rectangle(
				width=bar_w, height=bar_h, fill_color=hexof(FAINT), fill_opacity=1, stroke_width=0
			).move_to([right - bar_w / 2, y, 0])
			frac = min(max((SPEC.get("scene_index", 0) + 1) / total, 0.0), 1.0)
			done = (
				Rectangle(
					width=bar_w * frac,
					height=bar_h,
					fill_color=hexof(ACCENT),
					fill_opacity=1,
					stroke_width=0,
				)
				.align_to(track, LEFT)
				.set_y(y)
			)
			parts += [track, done]
		return parts

	# --- shared parts ----------------------------------------------------

	def _label(self, text: str, y: float, colour=DIM):
		return (
			Text(text.upper(), font=REGULAR, font_size=fs(30), color=hexof(colour))
			.move_to([0, y + 0.62, 0])
			.align_to([x_at(scaled(MARGIN)), 0, 0], LEFT)
		)

	def _bar(self, width: float, colour, y: float):
		left = x_at(scaled(MARGIN))
		w = max(width, 1e-4)
		return Rectangle(
			width=w, height=0.55, fill_color=hexof(colour), fill_opacity=1, stroke_width=0
		).move_to([left + w / 2, y, 0])

	def _counter(self, tracker: ValueTracker, colour, y: float, right_of: float, size=96):
		"""A counting number in Pango text, anchored by its left edge.

		Left-anchored because centring would shuffle the number sideways as it
		grows from one digit to three, which reads as a wobble rather than a
		count.
		"""
		left = x_at(scaled(MARGIN))

		def draw():
			t = Text(fmt(tracker.get_value()), font=BOLD, font_size=fs(size), color=hexof(colour))
			t.move_to([left + right_of + 0.30 + t.width / 2, y, 0])
			t.align_to([left + right_of + 0.30, 0, 0], LEFT)
			return t

		return always_redraw(draw)

	def _note(
		self,
		beats: Beats,
		under,
		text: str,
		span: tuple[float, float, float] | None = None,
		left_align: bool = False,
	):
		"""The line that names what the picture just showed."""
		if not text:
			return
		if span is not None:
			brace = BraceBetweenPoints(
				[span[0], span[2], 0], [span[1], span[2], 0], DOWN
			).set_color(hexof(SPEC.get("brace", FAINT)))
			note = Text(text, font=BOLD, font_size=fs(38), color=hexof(FG)).next_to(
				brace, DOWN, buff=0.22
			)
			self.play(GrowFromCenter(brace), FadeIn(note, shift=UP * 0.1), run_time=beats.rt(0.75))
		else:
			note = Text(text, font=REGULAR, font_size=fs(34), color=hexof(DIM)).next_to(
				under, DOWN, buff=0.55
			)
			# `next_to` centres on its anchor. Under a left-anchored number that
			# walks the note off the left edge of the frame, so templates whose
			# anchor is flush left say so.
			if left_align:
				note.align_to([x_at(scaled(MARGIN)), 0, 0], LEFT)
			self.play(FadeIn(note, shift=UP * 0.1), run_time=beats.rt(0.6))

	# --- templates -------------------------------------------------------

	def two_bar(self) -> None:
		"""The paper's value against the one it beats, at true relative length.

		The bars carry the claim on their own: at 200 against 20 the second is a
		tenth of the first, and the ratio is visible before the note names it.
		"""
		a, b = float(SPEC["value_a"]), float(SPEC["value_b"])
		full = 9.6
		wide = full * (max(a, b) / max(a, b))  # the larger bar spans `full`
		small = full * (min(a, b) / max(a, b))
		a_is_bigger = a >= b
		w_a, w_b = (wide, small) if a_is_bigger else (small, wide)

		row_a, row_b = y_at(scaled(420)), y_at(scaled(700))
		left = x_at(scaled(MARGIN))

		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 1.7 + 2.0 + 0.4 + 0.8 + 0.75)

		la = self._label(SPEC["label_a"], row_a)
		lb = self._label(SPEC["label_b"], row_b)
		va, vb = ValueTracker(0), ValueTracker(0)
		na = self._counter(va, ACCENT, row_a, w_a)
		nb = self._counter(vb, BASE_TEXT, row_b, w_b)
		bar_a, bar_b = self._bar(w_a, ACCENT, row_a), self._bar(w_b, BASE, row_b)

		self.play(FadeIn(la, shift=UP * 0.12), run_time=beats.rt(0.45))
		self.add(na)
		bar_a.stretch_to_fit_width(1e-4).align_to([left, 0, 0], LEFT)
		self.play(
			bar_a.animate.stretch_to_fit_width(w_a).align_to([left, 0, 0], LEFT),
			va.animate.set_value(a),
			run_time=beats.rt(1.7),
			rate_func=rate_functions.ease_out_cubic,
		)
		self.wait(beats.hold(2.0))

		self.play(FadeIn(lb, shift=UP * 0.12), run_time=beats.rt(0.4))
		self.add(nb)
		bar_b.stretch_to_fit_width(1e-4).align_to([left, 0, 0], LEFT)
		self.play(
			bar_b.animate.stretch_to_fit_width(w_b).align_to([left, 0, 0], LEFT),
			vb.animate.set_value(b),
			run_time=beats.rt(0.8),
			rate_func=rate_functions.ease_out_cubic,
		)
		# The brace spans the gap between the two bars - the thing being claimed.
		self._note(
			beats,
			None,
			SPEC.get("note", ""),
			(left + min(w_a, w_b), left + max(w_a, w_b), row_b - 0.34),
		)
		self.wait(beats.remainder())

	def count_up(self) -> None:
		"""One number, counted rather than stated, with its unit under it."""
		a = float(SPEC["value_a"])
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 1.9 + 0.6)

		label = Text(
			SPEC["label_a"].upper(), font=REGULAR, font_size=fs(32), color=hexof(DIM)
		).move_to([0, y_at(scaled(360)), 0])
		v = ValueTracker(0)
		number = always_redraw(
			lambda: Text(
				fmt(v.get_value()), font=BOLD, font_size=fs(210), color=hexof(ACCENT)
			).move_to([0, y_at(scaled(540)), 0])
		)
		unit = Text(SPEC.get("unit", ""), font=REGULAR, font_size=fs(44), color=hexof(FG)).move_to(
			[0, y_at(scaled(720)), 0]
		)

		self.play(FadeIn(label, shift=UP * 0.12), run_time=beats.rt(0.45))
		self.add(number)
		self.play(
			v.animate.set_value(a), run_time=beats.rt(1.9), rate_func=rate_functions.ease_out_cubic
		)
		if SPEC.get("unit"):
			self.add(unit)
		self._note(beats, unit, SPEC.get("note", ""))
		self.wait(beats.remainder())

	def split(self) -> None:
		"""One bar divided, when the point is a proportion rather than a size."""
		a = float(SPEC["value_a"])
		full, row = 11.6, y_at(scaled(520))
		left = x_at(scaled(MARGIN))
		w_a = full * a / 100.0

		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 1.5 + 0.5 + 0.6)

		track = self._bar(full, BASE, row)
		share = self._bar(w_a, ACCENT, row)
		v = ValueTracker(0)
		pct = always_redraw(
			lambda: (
				Text(f"{fmt(v.get_value())}%", font=BOLD, font_size=fs(96), color=hexof(ACCENT))
				.move_to([left, row - 1.15, 0])
				.align_to([left, 0, 0], LEFT)
			)
		)
		la = self._label(SPEC["label_a"], row)
		lb = (
			Text(
				SPEC.get("label_b", "").upper(),
				font=REGULAR,
				font_size=fs(30),
				color=hexof(BASE_TEXT),
			)
			.move_to([0, row - 1.15, 0])
			.align_to([left + full, 0, 0], LEFT + UP)
		)

		self.add(track)
		self.play(FadeIn(la, shift=UP * 0.12), run_time=beats.rt(0.45))
		self.add(pct)
		share.stretch_to_fit_width(1e-4).align_to([left, 0, 0], LEFT)
		self.play(
			share.animate.stretch_to_fit_width(w_a).align_to([left, 0, 0], LEFT),
			v.animate.set_value(a),
			run_time=beats.rt(1.5),
			rate_func=rate_functions.ease_out_cubic,
		)
		if SPEC.get("label_b"):
			lb.next_to(track, DOWN, buff=0.35).align_to([left + full, 0, 0], LEFT)
			lb.shift(LEFT * lb.width)
			self.play(FadeIn(lb), run_time=beats.rt(0.5))
		self._note(beats, pct, SPEC.get("note", ""), left_align=True)
		self.wait(beats.remainder())

	def decay_curve(self) -> None:
		"""A value falling step by step, drawn as it falls.

		The one template where the *shape* is the finding. "Success collapses from
		near-perfect to near-zero within sixteen steps" is a curve, and Ep. 6 spent
		it on a static number because a curve was not on offer. Drawing it as it
		falls is the whole point: the viewer watches the decline happen rather than
		reading its endpoint.

		Geometric between the two endpoints the digest supplies - which is what the
		papers this fires on actually report - so the curve is determined by
		`value_a`, `value_b` and `steps` rather than by anything invented here.
		"""
		a = float(SPEC["value_a"])
		b = float(SPEC["value_b"])
		steps = max(int(float(SPEC.get("steps") or 2)), 2)

		left = x_at(scaled(MARGIN))
		right = x_at(config.pixel_width - scaled(MARGIN))
		base = y_at(scaled(840))
		top = y_at(scaled(340))
		width, height = right - left, top - base

		# Normalised to the starting value, so the curve fills the frame whatever
		# the units are. A zero or negative start has no ratio to fall along.
		ratio = (b / a) if a > 0 and b > 0 else 0.0
		rate = ratio ** (1.0 / steps) if ratio > 0 else 0.0

		def at(x: float) -> list[float]:
			frac = rate**x if rate > 0 else max(1.0 - x / steps, 0.0)
			return [left + width * (x / steps), base + height * frac, 0]

		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 2.2 + 0.5 + 0.6)

		axis_colour = hexof(SPEC.get("brace", FAINT))
		axes = [
			Line([left, base, 0], [right, base, 0], stroke_width=2, color=axis_colour),
			Line([left, base, 0], [left, top, 0], stroke_width=2, color=axis_colour),
		]
		curve = VMobject(stroke_width=7, color=hexof(ACCENT))
		# Sampled well above the step count: the fall between the first two steps
		# is the steepest part and a per-step polyline visibly corners there.
		curve.set_points_smoothly([at(i / 8.0) for i in range(steps * 8 + 1)])

		# Both stacked above the axis rather than beside it: anything at the top
		# left of the plot area is where the curve starts, and the first render
		# put the label straight through the y-axis.
		label = (
			Text(SPEC["label_a"].upper(), font=REGULAR, font_size=fs(30), color=hexof(DIM))
			.move_to([left, top + 1.12, 0])
			.align_to([left, 0, 0], LEFT)
		)
		start = (
			Text(f"{fmt(a)}{SPEC.get('unit', '')}", font=BOLD, font_size=fs(52), color=hexof(FG))
			.move_to([left, top + 0.46, 0])
			.align_to([left, 0, 0], LEFT)
		)
		end_dot = Dot(at(steps), radius=0.11, color=hexof(ACCENT))
		end_text = (
			Text(
				f"{fmt(b)}{SPEC.get('unit', '')}",
				font=BOLD,
				font_size=fs(60),
				color=hexof(ACCENT),
			)
			.next_to(end_dot, UP, buff=0.3)
			.shift(LEFT * 0.35)
		)
		steps_label = Text(
			f"{fmt(steps)} STEPS", font=REGULAR, font_size=fs(30), color=hexof(DIM)
		).move_to([right - 0.9, base - 0.45, 0])

		self.add(*axes)
		self.play(FadeIn(label, shift=UP * 0.12), FadeIn(start), run_time=beats.rt(0.45))
		self.play(Create(curve), run_time=beats.rt(2.2), rate_func=rate_functions.linear)
		self.play(FadeIn(end_dot), FadeIn(end_text, shift=UP * 0.1), run_time=beats.rt(0.5))
		self.add(steps_label)
		# Not `_note`: under the step count is the frame's bottom edge, where the
		# progress bar already is. A decaying curve empties its own top right, so
		# the note goes there instead.
		if SPEC.get("note"):
			note = (
				Text(SPEC["note"], font=REGULAR, font_size=fs(34), color=hexof(DIM))
				.move_to([right, top + 0.75, 0])
				.align_to([right, 0, 0], RIGHT)
			)
			self.play(FadeIn(note, shift=UP * 0.1), run_time=beats.rt(0.6))
		self.wait(beats.remainder())

	# --- D46: shared by the templates below ----------------------------------

	def _heading(self, beats: Beats, s: str) -> None:
		"""What is being measured, small and dim, above the chart."""
		if not s:
			return
		h = fit(text(s.upper(), 30, DIM), right_edge() - left_edge())
		h.move_to([0, y_at(scaled(215)), 0]).align_to([left_edge(), 0, 0], LEFT)
		self.play(FadeIn(h, shift=UP * 0.12), run_time=beats.rt(0.45))

	def _footer(self, beats: Beats) -> None:
		"""`note`, on a fixed line above the attribution, where no chart reaches."""
		if not SPEC.get("note"):
			return
		n = fit(text(SPEC["note"], 34, DIM), right_edge() - left_edge())
		n.move_to([0, y_at(scaled(925)), 0]).align_to([left_edge(), 0, 0], LEFT)
		self.play(FadeIn(n, shift=UP * 0.1), run_time=beats.rt(0.6))

	def _axis(self, lo: float, hi: float, y: float, x0: float, x1: float):
		"""A horizontal scale with its ends (and zero, when it spans zero) marked."""

		def at(v: float) -> float:
			return x0 + (x1 - x0) * (v - lo) / (hi - lo)

		parts = [Line([x0, y, 0], [x1, y, 0], stroke_width=3, color=hexof(DIM))]
		ticks = [lo, hi] + ([0.0] if lo < 0 < hi else [])
		for t in ticks:
			parts.append(
				Line([at(t), y - 0.12, 0], [at(t), y + 0.12, 0], stroke_width=3, color=hexof(DIM))
			)
			parts.append(text(val(t), 28, DIM).move_to([at(t), y - 0.45, 0]))
		return VGroup(*parts), at

	def _counter_at(self, tracker: ValueTracker, final: float, size: float, colour, centre):
		"""A counting number, anchored where its *final* text will sit.

		Centring each frame would shuffle the number sideways as it gains digits.
		Anchoring the left edge to where the finished value is centred keeps it
		still and still lands it centred.
		"""
		width = text(val(final), size).width
		x0 = centre[0] - width / 2

		def draw():
			t = text(val(tracker.get_value(), final), size, colour)
			return t.move_to([x0 + t.width / 2, centre[1], 0])

		return always_redraw(draw)

	def _hbars(self, beats: Beats, items: list, order: list[int], colours: list, vmax: float):
		"""Labelled horizontal bars, one row per item, grown in `order`."""
		left, right = left_edge(), right_edge()
		col = left + 4.4
		full = right - col - 2.0
		top, bottom = y_at(scaled(290)), y_at(scaled(870))
		step = min(1.05, (top - bottom) / len(items))
		rows = []
		for rank, i in enumerate(order):
			y = top - step * (rank + 0.5)
			v = float(items[i]["value"])
			w = max(full * v / vmax, 0.02)
			bar = Rectangle(
				width=w,
				height=step * 0.56,
				fill_color=hexof(colours[i]),
				fill_opacity=1,
				stroke_width=0,
			).move_to([col + w / 2, y, 0])
			label = fit(text(items[i]["label"], 32, FG if i == 0 else DIM), col - left - 0.3)
			label.move_to([0, y, 0]).align_to([left, 0, 0], LEFT)
			figure = text(val(v), 36, SERIES_TEXT[0] if colours[i] == ACCENT else BASE_TEXT)
			figure.next_to(bar, RIGHT, buff=0.22)
			self.play(
				FadeIn(label),
				GrowFromEdge(bar, LEFT),
				FadeIn(figure, shift=RIGHT * 0.1),
				run_time=beats.rt(0.5),
				rate_func=rate_functions.ease_out_cubic,
			)
			rows.append(bar)
		return col, full, top, top - step * len(items)

	def _grid(self, beats: Beats, n: float, lit: float, round_cells: bool):
		"""`n` cells on the right of the frame, the first `lit` of them in accent.

		A fractional `lit` fills the last cell partially - 2.5% is two and a half
		squares, not three - and a fractional `n` (multiplier) draws a partial
		last cell, so the area on screen is always the stated number.
		"""
		x0, x1 = left_edge() + 4.0, right_edge()
		y0, y1 = y_at(scaled(870)), y_at(scaled(290))
		count = math.ceil(n)
		best = (0.0, 1)
		# A hundred cells are a percentage, and a percentage reads as ten by ten:
		# any other shape makes a row something other than ten points.
		for cols in [10] if count == 100 else range(1, count + 1):
			rows = math.ceil(count / cols)
			size = min((x1 - x0) / cols, (y1 - y0) / rows)
			if size > best[0]:
				best = (size, cols)
		pitch, cols = min(best[0], 1.6), best[1]
		cell = pitch * 0.8
		rows = math.ceil(count / cols)
		ox = x0 + ((x1 - x0) - pitch * cols) / 2 + pitch / 2
		oy = y1 - ((y1 - y0) - pitch * rows) / 2 - pitch / 2

		def make(i: int, frac: float, colour, opacity=1.0):
			w = cell * frac
			if round_cells:
				m = Dot(radius=cell / 2, color=hexof(colour), fill_opacity=opacity)
			else:
				m = Rectangle(
					width=max(w, 1e-3),
					height=cell,
					fill_color=hexof(colour),
					fill_opacity=opacity,
					stroke_width=0,
				)
			cx = ox + (i % cols) * pitch - (cell - w) / 2
			return m.move_to([cx, oy - (i // cols) * pitch, 0])

		track = VGroup(*[make(i, min(n - i, 1.0), FAINT) for i in range(count)])
		whole = int(lit)
		cells = [make(i, 1.0, ACCENT) for i in range(whole)]
		if lit - whole > 1e-6 and not round_cells:
			cells.append(make(whole, lit - whole, ACCENT))
		return track, VGroup(*cells), cell

	# --- D46: one or two values --------------------------------------------

	def delta(self) -> None:
		"""One value becoming another, with the change named.

		For "refusal falls from 58 to 32.5" or "false accepts 4.5% to 1.1%": two
		endpoints of a single change, where `two_bar` would draw them as rivals.
		"""
		a, b = float(SPEC["value_a"]), float(SPEC["value_b"])
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 + 0.6 + 1.4 + 0.5 + 0.6)
		self._heading(beats, long_unit())

		y = y_at(scaled(480))
		size = 150 if max(len(val(a)), len(val(b))) <= 6 else 110
		before = fit(text(val(a), size, BASE_TEXT), 5.2).move_to([-3.6, y, 0])
		end_w = min(text(val(b), size).width, 5.2)
		v = ValueTracker(a)
		after = self._counter_at(v, b, size, ACCENT, (3.6, y))
		arrow = Arrow(
			[-3.6 + before.width / 2 + 0.25, y, 0],
			[3.6 - end_w / 2 - 0.25, y, 0],
			stroke_width=8,
			color=hexof(DIM),
			buff=0,
		)
		la = fit(text(SPEC.get("label_a", ""), 32, DIM), 5.2).move_to([-3.6, y - 1.15, 0])
		lb = fit(text(SPEC.get("label_b", ""), 32, DIM), 5.2).move_to([3.6, y - 1.15, 0])
		d = b - a
		unit = " pts" if SPEC.get("unit") == "%" else suffix()
		change = text(f"{'+' if d > 0 else MINUS}{num(abs(d), d)}{unit}", 52, FG).move_to(
			[0, y - 2.15, 0]
		)

		self.play(FadeIn(before), FadeIn(la), run_time=beats.rt(0.5))
		self.play(GrowArrow(arrow), run_time=beats.rt(0.6))
		self.add(after)
		self.play(
			v.animate.set_value(b),
			FadeIn(lb),
			run_time=beats.rt(1.4),
			rate_func=rate_functions.ease_out_cubic,
		)
		self.play(FadeIn(change, shift=UP * 0.1), run_time=beats.rt(0.5))
		self._footer(beats)
		self.wait(beats.remainder())

	def gap(self) -> None:
		"""Two values on one true scale, and the distance between them braced.

		For "the best agent solved 57%, humans 85%: a 28-point gap". The axis is
		fixed by `scale`, not fitted to the two points, so the gap is drawn at its
		real share of the range.
		"""
		a, b = float(SPEC["value_a"]), float(SPEC["value_b"])
		lo, hi = bounds([a, b])
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.6 + 0.9 + 0.8 + 0.6)
		self._heading(beats, long_unit())

		x0, x1 = left_edge() + 0.4, right_edge() - 0.4
		y = y_at(scaled(600))
		axis, at = self._axis(lo, hi, y, x0, x1)
		self.play(Create(axis), run_time=beats.rt(0.6))

		hi_a = a >= b
		marks = []
		for value, label, colour, tcolour, lift in (
			(a, SPEC.get("label_a", ""), ACCENT, ACCENT, 1.75 if hi_a else 0.95),
			(b, SPEC.get("label_b", ""), BASE, BASE_TEXT, 0.95 if hi_a else 1.75),
		):
			dot = Dot([at(value), y, 0], radius=0.17, color=hexof(colour))
			stem = Line(
				[at(value), y, 0],
				[at(value), y + lift - 0.25, 0],
				stroke_width=2,
				color=hexof(colour),
			)
			tag = VGroup(
				text(val(value), 46, tcolour),
				text(label, 28, DIM),
			).arrange(RIGHT, buff=0.25)
			tag.move_to([at(value), y + lift, 0])
			# Keep the tag inside the frame at either end of the axis.
			if tag.get_left()[0] < left_edge():
				tag.align_to([left_edge(), 0, 0], LEFT)
			if tag.get_right()[0] > right_edge():
				tag.align_to([right_edge(), 0, 0], RIGHT)
			marks.append(VGroup(stem, dot, tag))
		self.play(
			LaggedStart(*[FadeIn(m, shift=DOWN * 0.15) for m in marks], lag_ratio=0.4),
			run_time=beats.rt(0.9),
		)

		span = Line([at(a), y, 0], [at(b), y, 0], stroke_width=12, color=hexof(ACCENT))
		span.set_opacity(0.45)
		brace = BraceBetweenPoints(
			[min(at(a), at(b)), y - 0.8, 0], [max(at(a), at(b)), y - 0.8, 0], DOWN
		)
		brace.set_color(hexof(FAINT))
		d = abs(a - b)
		unit = " pts" if SPEC.get("scale") == "percent" or SPEC.get("unit") == "%" else suffix()
		size = text(f"{num(d, d)}{unit}", 44, FG).next_to(brace, DOWN, buff=0.15)
		self.play(Create(span), GrowFromCenter(brace), FadeIn(size), run_time=beats.rt(0.8))
		self._footer(beats)
		self.wait(beats.remainder())

	def range(self) -> None:
		"""A result reported as a span, drawn as one.

		For "27-29% attack success" or "1.55x to 6.87x speedup": the static callout
		printed the two ends and the eye had to imagine the width. A reference line
		(`threshold`) can sit on the same axis - chance, a baseline, a target.
		"""
		a, b = float(SPEC["value_a"]), float(SPEC["value_b"])
		t = SPEC.get("threshold")
		lo, hi = bounds([a, b] + ([float(t)] if t is not None else []))
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.6 + 0.9 + 0.5 + 0.6 + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		x0, x1 = left_edge() + 0.4, right_edge() - 0.4
		y = y_at(scaled(640))
		axis, at = self._axis(lo, hi, y, x0, x1)
		self.play(Create(axis), run_time=beats.rt(0.6))

		w = max(at(b) - at(a), 0.06)
		band = Rectangle(
			width=w, height=0.7, fill_color=hexof(ACCENT), fill_opacity=0.35, stroke_width=0
		).move_to([(at(a) + at(b)) / 2, y + 0.45, 0])
		ends = VGroup(
			*[
				Line([at(v), y, 0], [at(v), y + 0.9, 0], stroke_width=6, color=hexof(ACCENT))
				for v in (a, b)
			]
		)
		figure = text(f"{num(a, a)}\u2013{val(b)}", 110, ACCENT)
		figure = fit(figure, x1 - x0).move_to([0, y + 2.0, 0])
		self.play(GrowFromCenter(band), Create(ends), run_time=beats.rt(0.9))
		self.play(FadeIn(figure, shift=UP * 0.12), run_time=beats.rt(0.5))

		if t is not None:
			t = float(t)
			ref = DashedLine(
				[at(t), y - 0.9, 0], [at(t), y + 1.2, 0], color=hexof(BASE), stroke_width=4
			)
			tag = text(f"{SPEC.get('threshold_label', '')} {val(t)}".strip(), 30, BASE_TEXT)
			tag.next_to(ref, DOWN, buff=0.1)
			if tag.get_right()[0] > right_edge():
				tag.align_to([right_edge(), 0, 0], RIGHT)
			if tag.get_left()[0] < left_edge():
				tag.align_to([left_edge(), 0, 0], LEFT)
			self.play(Create(ref), FadeIn(tag), run_time=beats.rt(0.6))
		self._footer(beats)
		self.wait(beats.remainder())

	def multiplier(self) -> None:
		"""One unit beside N of them, at the same size, so the factor is an area.

		For "seventy times smaller" or "3.6 times the base rate". A ratio stated
		as a number asks the viewer to imagine it; this draws it. Fractional
		factors draw a partial last block, so 3.6x is three and three fifths.
		"""
		n = float(SPEC["value_a"])
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 + 1.9 + 0.6)
		self._heading(beats, SPEC.get("label_a", ""))

		_, cells, cell = self._grid(beats, n, n, round_cells=False)
		left = left_edge()
		one = Rectangle(
			width=cell, height=cell, fill_color=hexof(BASE), fill_opacity=1, stroke_width=0
		).move_to([left + cell / 2, y_at(scaled(700)), 0])
		one_label = fit(text(SPEC.get("label_b", ""), 30, BASE_TEXT), 3.5)
		one_label.next_to(one, DOWN, buff=0.25).align_to([left, 0, 0], LEFT)
		v = ValueTracker(1)
		count = always_redraw(
			lambda: (
				text(f"{num(v.get_value(), n)}\u00d7", 130, ACCENT)
				.move_to([0, y_at(scaled(420)), 0])
				.align_to([left, 0, 0], LEFT)
			)
		)

		self.play(FadeIn(one), FadeIn(one_label), run_time=beats.rt(0.5))
		self.add(count)
		self.play(
			LaggedStart(*[GrowFromCenter(c) for c in cells], lag_ratio=0.6),
			v.animate.set_value(n),
			run_time=beats.rt(1.9),
			rate_func=rate_functions.ease_out_cubic,
		)
		self._footer(beats)
		self.wait(beats.remainder())

	def _part_of_whole(self, k: float, total: float, round_cells: bool, label: str) -> None:
		beats = Beats(SPEC["duration"])
		beats.plan(0.6 + 1.9 + 0.45 + 0.6)
		track, cells, _ = self._grid(beats, total, k, round_cells)
		left = left_edge()
		v = ValueTracker(0)
		pct = not round_cells

		def draw():
			s = f"{num(v.get_value(), k)}%" if pct else num(v.get_value(), k)
			return (
				text(s, 150, ACCENT).move_to([0, y_at(scaled(470)), 0]).align_to([left, 0, 0], LEFT)
			)

		count = always_redraw(draw)
		y_lab = y_at(scaled(600))
		parts = []
		if not pct:
			of = text(f"of {num(total)}", 44, FG)
			of.move_to([0, y_lab, 0]).align_to([left, 0, 0], LEFT)
			parts.append(of)
			y_lab -= 0.75
		lab = fit(text(wrap(label, 18), 32, DIM), 3.7)
		lab.move_to([0, y_lab - lab.height / 2 + 0.2, 0]).align_to([left, 0, 0], LEFT)
		parts.append(lab)

		self.play(FadeIn(track), run_time=beats.rt(0.6))
		self.add(count)
		anims = [v.animate.set_value(k)]
		if len(cells):
			anims.append(LaggedStart(*[FadeIn(c) for c in cells], lag_ratio=0.5))
		self.play(
			*anims,
			run_time=beats.rt(1.9),
			rate_func=rate_functions.linear,
		)
		self.play(*[FadeIn(m) for m in parts], run_time=beats.rt(0.45))
		self._footer(beats)
		self.wait(beats.remainder())

	def fraction(self) -> None:
		"""k of n, counted out as n marks with k lit.

		For "nine of ten models shifted rank", "sixteen of sixteen judgments".
		Small whole numbers are the claim, so each one gets a mark.
		"""
		self._part_of_whole(
			float(SPEC["value_a"]), float(SPEC["value_b"]), True, SPEC.get("label_a", "")
		)

	def waffle(self) -> None:
		"""A percentage as a hundred squares, for the shares a bar makes too thin.

		`split` is a bar, and 2.5% of a bar is a sliver. Two and a half squares
		out of a hundred is countable, which is the point of a small share.
		"""
		self._part_of_whole(float(SPEC["value_a"]), 100.0, False, SPEC.get("label_a", ""))

	# --- D46: several values --------------------------------------------------

	def ranking(self) -> None:
		"""Three to six bars, longest first: "beats X, Y and Z on benchmark B".

		The most common shape of an ML result and the one `two_bar` could only
		draw by dropping the middle. The paper's item (the first one given) keeps
		the accent wherever it lands in the order.
		"""
		items = SPEC["items"]
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 * len(items) + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())
		order = sorted(range(len(items)), key=lambda i: -float(items[i]["value"]))
		colours = [ACCENT] + [BASE] * (len(items) - 1)
		self._hbars(beats, items, order, colours, max(float(i["value"]) for i in items))
		self._footer(beats)
		self.wait(beats.remainder())

	def threshold(self) -> None:
		"""Bars against a line: which side of it each value falls.

		For "above sixty percent baseline, every model loses more than it gains"
		or a pass mark. Bars over the line wear the accent; the line is BASE,
		since it is the thing being measured against (D36).
		"""
		items = SPEC["items"]
		t = float(SPEC["threshold"])
		vmax = max([float(i["value"]) for i in items] + [t])
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.6 + 0.5 * len(items) + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		left = left_edge()
		col, full = left + 4.4, right_edge() - (left + 4.4) - 2.0
		top, bottom = y_at(scaled(290)), y_at(scaled(870))
		x = col + full * t / vmax
		line = DashedLine(
			[x, top + 0.1, 0], [x, bottom - 0.05, 0], color=hexof(BASE), stroke_width=4
		)
		tag = text(f"{SPEC.get('threshold_label', '')} {val(t)}".strip(), 28, BASE_TEXT)
		tag.next_to(line, DOWN, buff=0.08)
		self.play(Create(line), FadeIn(tag), run_time=beats.rt(0.6))
		colours = [ACCENT if float(i["value"]) >= t else BASE for i in items]
		self._hbars(beats, items, list(range(len(items))), colours, vmax)
		self.bring_to_front(line)
		self._footer(beats)
		self.wait(beats.remainder())

	def funnel(self) -> None:
		"""Stages that only narrow: 11,628 records, then 290, then 7.

		Each bar is drawn by shrinking a copy of the one above it, so the loss at
		every stage is something the viewer watches happen. The last stage wears
		the accent - in a funnel the end is the finding.
		"""
		items = SPEC["items"]
		vals = [float(i["value"]) for i in items]
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.55 * len(items) + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		left, right = left_edge(), right_edge()
		col = left + 4.4
		full = right - col - 2.4
		cx = col + full / 2
		top, bottom = y_at(scaled(290)), y_at(scaled(870))
		step = min(1.05, (top - bottom) / len(items))
		prev = None
		for i, (it, v) in enumerate(zip(items, vals, strict=True)):
			y = top - step * (i + 0.5)
			last = i == len(items) - 1
			colour = ACCENT if last else mix(BASE, BG, 0.15 * i)
			w = max(full * v / vals[0], 0.04)
			bar = Rectangle(
				width=w,
				height=step * 0.62,
				fill_color=hexof(colour),
				fill_opacity=1,
				stroke_width=0,
			).move_to([cx, y, 0])
			label = fit(text(it["label"], 32, FG if last else DIM), col - left - 0.3)
			label.move_to([0, y, 0]).align_to([left, 0, 0], LEFT)
			figure = text(val(v), 36, ACCENT if last else BASE_TEXT)
			figure.move_to([0, y, 0]).align_to([col + full + 0.3, 0, 0], LEFT)
			if prev is None:
				anim = GrowFromCenter(bar)
			else:
				ghost = prev.copy()
				self.add(ghost)
				anim = ghost.animate.become(bar)
			self.play(anim, FadeIn(label), FadeIn(figure), run_time=beats.rt(0.55))
			prev = bar
		self._footer(beats)
		self.wait(beats.remainder())

	def trend(self) -> None:
		"""Three to six values in order, joined: a scaling claim as a line.

		Points are evenly spaced and labelled with their own x (`items[].label`):
		the x values are categories as the paper reports them ("1B", "7B",
		"70B"), and spacing them to scale would claim a functional form the
		digest never gave. The y-axis starts at zero.
		"""
		items = SPEC["items"]
		vals = [float(i["value"]) for i in items]
		n = len(vals)
		top_v = nice_ceil(max(vals) * 1.1) if max(vals) > 0 else 1.0
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.4 + 2.0 + 0.6 + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		left, right = left_edge() + 0.6, right_edge() - 0.6
		base, top = y_at(scaled(800)), y_at(scaled(330))
		xs = [left + (right - left) * i / (n - 1) for i in range(n)]
		pts = [[x, base + (top - base) * v / top_v, 0] for x, v in zip(xs, vals, strict=True)]
		floor = Line(
			[left - 0.3, base, 0], [right + 0.3, base, 0], stroke_width=2, color=hexof(FAINT)
		)
		ticks = VGroup(
			*[
				fit(text(i["label"], 28, DIM), (right - left) / n).move_to([x, base - 0.4, 0])
				for i, x in zip(items, xs, strict=True)
			]
		)
		self.play(Create(floor), FadeIn(ticks), run_time=beats.rt(0.4))

		line = VMobject(stroke_width=7, color=hexof(ACCENT)).set_points_as_corners(pts)
		dots = VGroup(*[Dot(p, radius=0.11, color=hexof(ACCENT)) for p in pts])
		self.play(
			Create(line),
			LaggedStart(*[FadeIn(d) for d in dots], lag_ratio=0.9),
			run_time=beats.rt(2.0),
			rate_func=rate_functions.linear,
		)
		figures = VGroup(
			*[
				text(val(v), 34, FG).next_to(d, UP, buff=0.2)
				for v, d in zip(vals, dots, strict=True)
			]
		)
		self.play(FadeIn(figures, shift=UP * 0.1), run_time=beats.rt(0.6))
		self._footer(beats)
		self.wait(beats.remainder())

	def stat_row(self) -> None:
		"""Two to four headline counts side by side, counted up together.

		For "48,662 questions, 8 benchmarks, 10 models" - the scale of a study,
		which a single `count_up` could only give one number of.
		"""
		items = SPEC["items"]
		n = len(items)
		size = {2: 150, 3: 120, 4: 100}[n]
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 + 1.9 + 0.6)
		self._heading(beats, long_unit())

		left, right = left_edge(), right_edge()
		w = (right - left) / n
		y = y_at(scaled(500))
		trackers, counters, labels = [], [], []
		for i, it in enumerate(items):
			cx = left + w * (i + 0.5)
			v = ValueTracker(0)
			final = float(it["value"])
			trackers.append((v, final))
			counters.append(self._counter_at(v, final, size, ACCENT if i == 0 else FG, (cx, y)))
			labels.append(
				fit(text(wrap(it["label"], 16), 32, DIM), w - 0.4).move_to([cx, y - 1.3, 0])
			)
		self.play(*[FadeIn(lab) for lab in labels], run_time=beats.rt(0.5))
		self.add(*counters)
		self.play(
			*[v.animate.set_value(f) for v, f in trackers],
			run_time=beats.rt(1.9),
			rate_func=rate_functions.ease_out_cubic,
		)
		self._footer(beats)
		self.wait(beats.remainder())

	def scale(self) -> None:
		"""One to three values placed on a bounded scale: a cosine, a correlation.

		For "pain is nearly orthogonal to fear, cosine 0.12". A number like that
		means nothing until the viewer sees where it sits between -1 and 1, so
		the markers slide in from the low end to their place.
		"""
		items = SPEC["items"]
		vals = [float(i["value"]) for i in items]
		lo, hi = bounds(vals)
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.6 + 1.5 + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		x0, x1 = left_edge() + 0.4, right_edge() - 0.4
		y = y_at(scaled(640))
		axis, at = self._axis(lo, hi, y, x0, x1)
		self.play(Create(axis), run_time=beats.rt(0.6))

		lifts = [0.0] * len(vals)
		order = sorted(range(len(vals)), key=lambda i: at(vals[i]))
		for rank, i in enumerate(order):
			lifts[i] = 0.9 + 0.85 * (rank % 3)
		anims = []
		for i, (it, v) in enumerate(zip(items, vals, strict=True)):
			colour = SERIES[i]
			tcolour = SERIES_TEXT[i]
			tr = ValueTracker(lo)
			lift = lifts[i]

			def draw(tr=tr, lift=lift, it=it, v=v, colour=colour, tcolour=tcolour):
				x = at(tr.get_value())
				tag = VGroup(
					text(val(tr.get_value(), v), 40, tcolour), text(it["label"], 28, DIM)
				).arrange(RIGHT, buff=0.2)
				tag.move_to([x, y + lift, 0])
				if tag.get_left()[0] < left_edge():
					tag.align_to([left_edge(), 0, 0], LEFT)
				if tag.get_right()[0] > right_edge():
					tag.align_to([right_edge(), 0, 0], RIGHT)
				return VGroup(
					Line([x, y, 0], [x, y + lift - 0.25, 0], stroke_width=2, color=hexof(colour)),
					Dot([x, y, 0], radius=0.16, color=hexof(colour)),
					tag,
				)

			self.add(always_redraw(draw))
			anims.append(tr.animate.set_value(v))
		self.play(*anims, run_time=beats.rt(1.5), rate_func=rate_functions.ease_out_cubic)
		self._footer(beats)
		self.wait(beats.remainder())

	def stack(self) -> None:
		"""One bar split into two to five shares that sum to at most 100%.

		For "46% of first mistakes are reasoning errors, 24% action faults...".
		`split` draws one share against the rest; this draws the breakdown. What
		the items do not cover stays as the faint track, unlabelled.
		"""
		items = SPEC["items"]
		vals = [float(i["value"]) for i in items]
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.4 + 0.55 * len(items) + 0.6)
		self._heading(beats, SPEC.get("label_a") or long_unit())

		left, right = left_edge(), right_edge()
		full = right - left
		y = y_at(scaled(560))
		h = 1.0
		track = Rectangle(
			width=full, height=h, fill_color=hexof(FAINT), fill_opacity=1, stroke_width=0
		)
		track.move_to([left + full / 2, y, 0])
		self.play(FadeIn(track), run_time=beats.rt(0.4))
		x = left
		for i, (it, v) in enumerate(zip(items, vals, strict=True)):
			w = full * v / 100.0
			seg = Rectangle(
				width=max(w, 0.02),
				height=h,
				fill_color=hexof(SERIES[i]),
				fill_opacity=1,
				stroke_width=0,
			)
			seg.move_to([x + w / 2, y, 0])
			figure = text(val(v), 40, SERIES_TEXT[i])
			label = text(wrap(it["label"], 16), 28, DIM)
			# Alternate above and below, so two narrow neighbours do not collide.
			above = i % 2 == 1
			tag = VGroup(figure, label).arrange(DOWN, buff=0.12, aligned_edge=LEFT)
			if above:
				tag = VGroup(label, figure).arrange(DOWN, buff=0.12, aligned_edge=LEFT)
				tag.next_to(seg, UP, buff=0.3)
			else:
				tag.next_to(seg, DOWN, buff=0.3)
			tag.align_to([x + 0.05, 0, 0], LEFT)
			if tag.get_right()[0] > right:
				tag.align_to([right, 0, 0], RIGHT)
			self.play(
				GrowFromEdge(seg, LEFT),
				FadeIn(tag),
				run_time=beats.rt(0.55),
				rate_func=rate_functions.ease_out_cubic,
			)
			x += w
		self._footer(beats)
		self.wait(beats.remainder())

	def slope(self) -> None:
		"""Two to four items, each measured under two conditions, joined by a line.

		For "88-97% pressed again after sham relief, 24-72% after real relief", or
		one model's score before and after across benchmarks. Where `delta` is one
		change, this is several - and whether the lines cross is often the point.
		"""
		items = SPEC["items"]
		va = [float(i["value"]) for i in items]
		vb = [float(i["value_b"]) for i in items]
		lo = min(0.0, *va, *vb)
		hi = nice_ceil(max(*va, *vb) * 1.05)
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 + 0.5 + 1.4 + 0.6 + 0.6)
		self._heading(beats, long_unit())

		xl, xr = left_edge() + 3.2, right_edge() - 4.2
		bottom, top = y_at(scaled(860)), y_at(scaled(360))

		def y(v):
			return bottom + (top - bottom) * (v - lo) / (hi - lo)

		rails = VGroup(
			*[
				Line([x, bottom, 0], [x, top, 0], stroke_width=2, color=hexof(FAINT))
				for x in (xl, xr)
			]
		)
		heads = VGroup(
			fit(text(SPEC.get("label_a", "").upper(), 28, DIM), 4.0).move_to([xl, top + 0.45, 0]),
			fit(text(SPEC.get("label_b", "").upper(), 28, DIM), 4.0).move_to([xr, top + 0.45, 0]),
		)
		self.play(Create(rails), FadeIn(heads), run_time=beats.rt(0.5))

		ly = spread([y(v) for v in va], 0.45)
		ry = spread([y(v) for v in vb], 0.45)
		lefts, lines, rights = [], [], []
		for i, it in enumerate(items):
			c, tc = SERIES[i], SERIES_TEXT[i]
			d0, d1 = (
				Dot([xl, y(va[i]), 0], radius=0.12, color=hexof(c)),
				Dot([xr, y(vb[i]), 0], radius=0.12, color=hexof(c)),
			)
			lefts.append(
				VGroup(
					d0,
					text(val(va[i]), 34, tc)
					.move_to([0, ly[i], 0])
					.align_to([xl - 0.3, 0, 0], RIGHT),
				)
			)
			lines.append(Line(d0.get_center(), d1.get_center(), stroke_width=6, color=hexof(c)))
			tag = VGroup(text(val(vb[i]), 34, tc), fit(text(it["label"], 28, DIM), 2.6)).arrange(
				RIGHT, buff=0.22
			)
			tag.move_to([0, ry[i], 0]).align_to([xr + 0.3, 0, 0], LEFT)
			rights.append(VGroup(d1, tag))
		self.play(*[FadeIn(m) for m in lefts], run_time=beats.rt(0.5))
		self.play(
			*[Create(m) for m in lines],
			run_time=beats.rt(1.4),
			rate_func=rate_functions.ease_in_out_sine,
		)
		self.play(*[FadeIn(m, shift=RIGHT * 0.1) for m in rights], run_time=beats.rt(0.6))
		self._footer(beats)
		self.wait(beats.remainder())

	# --- D46: labels only -------------------------------------------------------

	def _box(self, label: str, width: float, height: float, chars: int, emphasis: bool = False):
		box = RoundedRectangle(
			corner_radius=0.14,
			width=width,
			height=height,
			fill_color=hexof(mix(BG, ACCENT, 0.16 if emphasis else 0.08)),
			fill_opacity=1,
			stroke_color=hexof(ACCENT if emphasis else DIM),
			stroke_width=3,
		)
		words = fit(text(wrap(label, chars), 32, FG), width - 0.35)
		if words.height > height - 0.25:
			words.scale_to_fit_height(height - 0.25)
		return VGroup(box, words.move_to(box.get_center()))

	def flow(self) -> None:
		"""Three to five stages joined by arrows: a method, in order.

		Method scenes were bullet slides, the one place a viewer most needs a
		diagram. Labels only, so the digest check has nothing to hold - the
		risk is a true label on the wrong box (D37), and the defence is the
		prompt's rule that every box names a step the narration says.
		"""
		items = SPEC["items"]
		n = len(items)
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.75 * n + 0.6)
		self._heading(beats, SPEC.get("label_a", ""))

		left, right = left_edge(), right_edge()
		gap = 0.75
		w = (right - left - gap * (n - 1)) / n
		y = y_at(scaled(560))
		chars = {3: 18, 4: 14, 5: 11}[n]
		boxes = [
			self._box(it["label"], w, 1.9, chars, emphasis=i == n - 1) for i, it in enumerate(items)
		]
		for i, b in enumerate(boxes):
			b.move_to([left + w / 2 + i * (w + gap), y, 0])
		for i, b in enumerate(boxes):
			anims = [FadeIn(b, shift=RIGHT * 0.15)]
			if i:
				arrow = Arrow(
					boxes[i - 1].get_right(),
					b.get_left(),
					buff=0.08,
					stroke_width=6,
					color=hexof(DIM),
					max_tip_length_to_length_ratio=0.45,
				)
				anims.insert(0, GrowArrow(arrow))
			self.play(*anims, run_time=beats.rt(0.75))
		self._footer(beats)
		self.wait(beats.remainder())

	def cycle(self) -> None:
		"""Three to five steps in a loop, with a marker going round once.

		For "validate, explore, plan - and back to validate": a `flow` drawn as a
		line would say the process ends, which is the one thing a loop does not.
		"""
		items = SPEC["items"]
		n = len(items)
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.6 * n + 0.5 + 1.6 + 0.6)
		self._heading(beats, SPEC.get("label_a", ""))

		cx, cy = 0.0, y_at(scaled(590))
		rx, ry = 4.3, 1.85
		angles = [PI / 2 - 2 * PI * i / n for i in range(n)]
		pts = [[cx + rx * math.cos(a), cy + ry * math.sin(a), 0] for a in angles]
		boxes = [self._box(it["label"], 3.3, 1.05, 16) for it in items]
		for b, p in zip(boxes, pts, strict=True):
			b.move_to(p)
		path = Ellipse(width=2 * rx, height=2 * ry, color=hexof(FAINT), stroke_width=3).move_to(
			[cx, cy, 0]
		)
		path.rotate(PI / 2).stretch_to_fit_width(2 * rx).stretch_to_fit_height(2 * ry)
		self.add(path)
		for i, b in enumerate(boxes):
			anims = [FadeIn(b, scale=0.9)]
			if i:
				anims.insert(0, self._arc(pts[i - 1], pts[i]))
			self.play(*anims, run_time=beats.rt(0.6))
		self.play(self._arc(pts[-1], pts[0]), run_time=beats.rt(0.5))
		marker = Dot(pts[0], radius=0.13, color=hexof(ACCENT))
		loop = Ellipse(width=2 * rx, height=2 * ry).move_to([cx, cy, 0]).rotate(PI / 2).flip()
		loop.stretch_to_fit_width(2 * rx).stretch_to_fit_height(2 * ry).move_to([cx, cy, 0])
		self.add(marker)
		self.bring_to_front(*boxes)
		self.play(
			MoveAlongPath(marker, loop),
			run_time=beats.rt(1.6),
			rate_func=rate_functions.ease_in_out_sine,
		)
		self._footer(beats)
		self.wait(beats.remainder())

	def _arc(self, p0, p1):
		"""A clockwise arrow between two nodes on the ellipse, clear of both boxes."""
		import numpy as np

		a, b = np.array(p0, dtype=float), np.array(p1, dtype=float)
		d = b - a
		start, end = a + d * 0.3, b - d * 0.3
		arrow = CurvedArrow(
			start, end, angle=-0.5, color=hexof(DIM), stroke_width=5, tip_length=0.22
		)
		return Create(arrow)

	def versus(self) -> None:
		"""Two terms and the relation between them: "reliability ≠ validity".

		Nine of the static callouts across twelve episodes were exactly this - a
		distinction the segment turns on, typed as a sentence fragment. Labels
		only; `label_c` is the result of an arithmetic relation ("authority -
		evidence = the verifiability gap"), or a caption under any other.
		"""
		rel = SPEC.get("relation", "vs")
		beats = Beats(SPEC["duration"])
		beats.plan(0.45 + 0.5 + 0.5 + 0.7 + 0.6 + 0.6)

		y = y_at(scaled(500))
		a = fit(text(wrap(SPEC["label_a"], 16), 64, FG), 5.0).move_to([-3.7, y, 0])
		b = fit(text(wrap(SPEC["label_b"], 16), 64, FG), 5.0).move_to([3.7, y, 0])
		glyph = _RELATION.get(rel, "vs")
		sym = text(glyph, 150 if glyph != "vs" else 90, ACCENT).move_to([0, y, 0])

		self.play(FadeIn(a, shift=RIGHT * 0.3), run_time=beats.rt(0.5))
		self.play(FadeIn(b, shift=LEFT * 0.3), run_time=beats.rt(0.5))
		if rel == "!=":
			# Drawn as the equals and then struck through: the claim is that the
			# two look the same and are not.
			eq = text("=", 150, ACCENT).move_to([0, y, 0])
			slash = Line(
				[0.35, y + 0.7, 0], [-0.35, y - 0.7, 0], stroke_width=10, color=hexof(ACCENT)
			)
			self.play(GrowFromCenter(eq), run_time=beats.rt(0.35))
			self.play(Create(slash), run_time=beats.rt(0.35))
		else:
			self.play(GrowFromCenter(sym), run_time=beats.rt(0.7))
		if SPEC.get("label_c"):
			arithmetic = rel in ("-", "+")
			c = text(
				("= " if arithmetic else "") + SPEC["label_c"],
				52 if arithmetic else 38,
				ACCENT if arithmetic else DIM,
			)
			c = fit(c, right_edge() - left_edge()).move_to([0, y - 2.0, 0])
			self.play(FadeIn(c, shift=UP * 0.1), run_time=beats.rt(0.6))
		self._footer(beats)
		self.wait(beats.remainder())
