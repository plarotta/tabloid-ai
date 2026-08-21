"""Spike: an animated result_callout, against the pipeline's own visual system.

Real scene from runs/2026-08-19: segment 2608.17286, scene s3, measured 9.55s.

  narration  "Compute-optimal training occurs at two hundred image tokens per
              parameter, ten times the Chinchilla rule of twenty tokens per
              parameter for language models."
  static     the words "200 tokens per parameter", centred, in the accent colour

The narration makes a *comparison* the static slide cannot show. That is the
whole case for animating this scene type and none other.

Palette, margins, chrome and fonts are copied from pipeline/render/slides.py so
the difference on screen is motion and layout, not styling.
"""

import os

from manim import *

# --- the pipeline's visual system (slides.py) --------------------------------
#
# The static palette has five roles: ground, type, dim type, faint rules, and one
# accent. A comparison needs a sixth it does not have - a colour for the value
# being compared *against*. Drawn in FAINT, the baseline bar reads as chrome
# rather than as data, which is the palette problem this spike surfaced.
#
# BASE is that sixth role. Each variant below answers it differently; the rest of
# the palette is held fixed so the judgement is about one decision.

PALETTES = {
	# What the spike rendered first: the baseline bar in the rules colour.
	"current": dict(BG="#0F1115", BASE="#3A404C", BASE_TEXT="#E9EDF3", BRACE="#3A404C"),
	# A data-neutral slate: still neutral, but light enough to read as a value.
	"neutral": dict(BG="#0F1115", BASE="#59637A", BASE_TEXT="#E9EDF3", BRACE="#59637A"),
	# Monochrome: the baseline is the accent hue, darkened. Keeps the episode to
	# one colour, which is what D35 restored.
	"mono": dict(BG="#0F1115", BASE="#3E5C8A", BASE_TEXT="#A8C0E8", BRACE="#3E5C8A"),
	# Duotone: a warm counter-colour for the baseline. Strongest separation, and
	# the only variant that puts a second hue on screen.
	"duotone": dict(BG="#0F1115", BASE="#C98B3F", BASE_TEXT="#E8C79A", BRACE="#6B6152"),
	# The neutral, on a ground lifted off pure black.
	"lifted": dict(BG="#161A21", BASE="#59637A", BASE_TEXT="#E9EDF3", BRACE="#59637A"),
	# Chosen (owner, 2026-08-20): the warm counter-colour on the lifted ground.
	"chosen": dict(BG="#161A21", BASE="#C98B3F", BASE_TEXT="#E8C79A", BRACE="#7A6E58"),
}
_P = PALETTES[os.environ.get("SPIKE_PALETTE", "current")]

BG = _P["BG"]
FG = "#E9EDF3"  # (233, 237, 243)
DIM = "#929CAA"  # (146, 156, 170)
FAINT = "#3A404C"  # (58, 64, 76)  - rules and inactive chrome only
ACCENT = "#7AA2F7"  # (122, 162, 247)
BASE = _P["BASE"]  # the value being compared against
BASE_TEXT = _P["BASE_TEXT"]
BRACE = _P["BRACE"]

PX = 135.0  # pixels per manim unit at 1920x1080 with frame_height 8
MARGIN_PX = 110
BOLD, REGULAR = "Arial", "Arial"

# Measured: manim font_size F renders Arial at about 0.534 x the pixel em size
# Pillow draws at, so the chrome matches the static slide rather than merely
# resembling it.
FONT_RATIO = 0.534

DURATION = 9.55  # the measured audio for this scene, which the slide is held for


def fs(px: float) -> float:
	return px * FONT_RATIO


def x_at(px: float) -> float:
	"""Screen x for a pixel column measured from the left edge."""
	return -config.frame_width / 2 + px / PX


def y_at(px: float) -> float:
	"""Screen y for a pixel row measured from the top edge."""
	return config.frame_height / 2 - px / PX


class AnimatedCallout(Scene):
	def construct(self):
		self.camera.background_color = BG
		left = x_at(MARGIN_PX)

		# --- chrome, identical to slides.chrome() ---------------------------
		eyebrow = Text(
			"Abra: Scaling Diffusion Image Training", font=REGULAR, font_size=fs(26), color=DIM
		).move_to([0, y_at(74 + 13), 0]).align_to([left, 0, 0], LEFT)

		footer = Text(
			"arXiv:2608.17286", font=REGULAR, font_size=fs(25), color=DIM
		).move_to([0, y_at(996 + 12), 0]).align_to([left, 0, 0], LEFT)

		bar_w, bar_h = 260 / PX, 4 / PX
		track = Rectangle(width=bar_w, height=bar_h, fill_color=FAINT, fill_opacity=1, stroke_width=0)
		track.move_to([x_at(1920 - MARGIN_PX) - bar_w / 2, y_at(1008 + 2), 0])
		# scene 3 of 8, the same fraction the static slide would draw
		done = Rectangle(
			width=bar_w * 3 / 8, height=bar_h, fill_color=ACCENT, fill_opacity=1, stroke_width=0
		).align_to(track, LEFT).set_y(track.get_y())

		self.add(eyebrow, footer, track, done)

		# --- the comparison --------------------------------------------------
		# 200 against 20 is 10:1, so the bar lengths carry the claim on their own.
		full_w = 9.6
		row1_y, row2_y = y_at(420), y_at(700)
		label_dy, num_gap = 0.62, 0.30

		def label(text, y):
			return Text(text, font=REGULAR, font_size=fs(30), color=DIM).move_to(
				[0, y + label_dy, 0]
			).align_to([left, 0, 0], LEFT)

		l1 = label("DIFFUSION  ·  THIS PAPER", row1_y)
		l2 = label("LANGUAGE MODELS  ·  CHINCHILLA", row2_y)

		def bar(width, colour, y):
			r = Rectangle(
				width=max(width, 1e-4), height=0.55, fill_color=colour, fill_opacity=1, stroke_width=0
			)
			r.move_to([left + max(width, 1e-4) / 2, y, 0])
			return r

		b1 = bar(full_w, ACCENT, row1_y)
		b2 = bar(full_w / 10, BASE, row2_y)

		v1, v2 = ValueTracker(0), ValueTracker(0)

		def counter(tracker, colour, y, target_w):
			"""A counting number built from Pango text, not DecimalNumber.

			`DecimalNumber` renders through MathTex, which would drag a full TeX
			install into CI for the sake of three digits. Rebuilding a `Text` each
			frame costs nothing at this size and keeps the dependency to Pango,
			which manim already needs for every other label on the slide.
			"""

			def draw():
				t = Text(
					f"{int(round(tracker.get_value()))}",
					font=BOLD,
					font_size=fs(96),
					color=colour,
				)
				# Anchored by its left edge: centring would shuffle the number
				# sideways as it grows from one digit to three.
				t.move_to([left + target_w + num_gap + t.width / 2, y, 0])
				t.align_to([left + target_w + num_gap, 0, 0], LEFT)
				return t

			return always_redraw(draw)

		n1 = counter(v1, ACCENT, row1_y, full_w)
		n2 = counter(v2, BASE_TEXT, row2_y, full_w / 10)

		# --- 1. the paper's number, while the narration states it ------------
		self.play(FadeIn(l1, shift=UP * 0.12), run_time=0.45)
		self.add(n1)
		b1.stretch_to_fit_width(1e-4).align_to([left, 0, 0], LEFT)
		self.play(
			b1.animate.stretch_to_fit_width(full_w).align_to([left, 0, 0], LEFT),
			v1.animate.set_value(200),
			run_time=1.7,
			rate_func=rate_functions.ease_out_cubic,
		)
		self.wait(2.35)

		# --- 2. the rule it beats, as the narration reaches "Chinchilla" -----
		self.play(FadeIn(l2, shift=UP * 0.12), run_time=0.4)
		self.add(n2)
		b2.stretch_to_fit_width(1e-4).align_to([left, 0, 0], LEFT)
		self.play(
			b2.animate.stretch_to_fit_width(full_w / 10).align_to([left, 0, 0], LEFT),
			v2.animate.set_value(20),
			run_time=0.8,
			rate_func=rate_functions.ease_out_cubic,
		)

		# --- 3. name the gap the two bars have just drawn --------------------
		brace = BraceBetweenPoints(
			[left + full_w / 10, row2_y - 0.34, 0], [left + full_w, row2_y - 0.34, 0], DOWN
		).set_color(BRACE)
		gap = Text("10×  tokens per parameter", font=BOLD, font_size=fs(38), color=FG)
		gap.next_to(brace, DOWN, buff=0.22)
		self.play(GrowFromCenter(brace), FadeIn(gap, shift=UP * 0.1), run_time=0.75)

		spent = 0.45 + 1.7 + 2.35 + 0.4 + 0.8 + 0.75
		self.wait(DURATION - spent)
