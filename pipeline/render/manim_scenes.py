"""The animated-callout templates, rendered by manim.

This module is **executed by the manim CLI in a subprocess**, never imported by
the pipeline. `animate.py` writes a spec to a temp file, points
`TABLOID_CLIP_SPEC` at it and shells out; that keeps manim's global config, its
import cost and its failure modes out of the render stage entirely, and makes a
timeout enforceable.

Three templates, and the model may only choose between them and fill their
parameters (`schemas.Comparison`). It never writes animation code: a scene that
compiles, reads well and matches its narration is hard to generate and
impossible to check by eye at scale, while four numbers and two labels can be
validated in full. See DECISIONS.md D36.

Two rules hold across every template:

  - **Nothing from the MathTex family.** `DecimalNumber`, `Tex`, `MathTex` and
    friends render through LaTeX, which would put a ~1GB TeX install on the CI
    runner to draw three digits. Numbers are Pango `Text` rebuilt per frame.
  - **The clip is exactly as long as it is told to be.** Stage 8 syncs video to
    *measured* audio, so a clip that runs its own length would break the one
    invariant the render is built on (D20).
"""

from __future__ import annotations

import json
import os

from manim import (
	DOWN,
	LEFT,
	UP,
	BraceBetweenPoints,
	FadeIn,
	GrowFromCenter,
	Rectangle,
	Scene,
	Text,
	ValueTracker,
	always_redraw,
	config,
	rate_functions,
)

# Absolute, not relative: manim loads this file by path, so it has no package
# context and `from .slides import ...` raises. The project is installed, so the
# palette still comes from one place rather than being copied here.
from pipeline.render.slides import ACCENT, BASE, BASE_TEXT, DIM, FAINT, FG, MARGIN

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
		{"two_bar": self.two_bar, "count_up": self.count_up, "split": self.split}[
			SPEC["template"]
		]()

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
