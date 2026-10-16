"""Geometry-led explanations: parallel computation, measured sweeps, test scope.

Motion is explanatory and seekable. Traveling marks illustrate information flow,
not measured attention weights; observations never interpolate invented values.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from .slides import ACCENT, BASE_TEXT, DIM, FG, mix

PALETTE = [ACCENT, (87, 211, 189), (245, 201, 107), (203, 153, 247)]
GROUND = (12, 16, 24)


def smooth(t):
	t = min(1.0, max(0.0, t))
	return t * t * (3 - 2 * t)


class ConceptScene:
	def __init__(self, design):
		self.d = design
		self.c = design.scene.visual.concept

	def time(self, key, fraction):
		return self.d.cues.get(key, fraction * self.d.duration)

	def phase(self, t, key, fraction, seconds=0.8):
		start = self.time(key, fraction)
		return smooth(
			(t - start) / max(0.001, min(seconds, self.d.duration * 0.2, self.d.duration - start))
		)

	def text(self, draw, label, box, size=44, color=FG):
		self.d.text(draw, label, box, size, color, False)

	def line(self, draw, points, color, width=3):
		draw.line(
			[(self.d.x(x), self.d.y(y)) for x, y in points], fill=color, width=self.d.size(width)
		)

	def dot(self, draw, x, y, radius, color, outline=False):
		box = self.d.rect((x - radius, y - radius, x + radius, y + radius))
		draw.ellipse(box, fill=None if outline else color, outline=color, width=self.d.size(3))

	def frame(self, t):
		image = Image.new("RGB", self.d.base.size, GROUND)
		draw = ImageDraw.Draw(image)
		self.text(draw, self.d.scene.visual.title or "", (105, 75, 1810, 175), 62)
		if self.c.kind == "parallel_paths":
			self.network(draw, t)
		elif self.c.kind == "parameter_sweep":
			self.sweep(draw, t)
		else:
			self.scope(draw, t)
		self.text(draw, self.d.scene.visual.source, (105, 1000, 1800, 1050), 22, DIM)
		return image

	def network(self, draw, t):
		c = self.c
		before = c.previous_branches or c.branches
		change = self.phase(t, "branches", 0.2)
		built = 1 if c.previous_branches is not None else change
		input_p = 1 if c.previous_branches is not None else self.phase(t, "input", 0.03)
		combine = 1 if c.previous_branches is not None else self.phase(t, "combine", 0.65)
		# A vector glyph, not fabricated data: equal cells encode representation.
		for row in range(6):
			color = mix(GROUND, ACCENT, input_p)
			draw.rounded_rectangle(
				self.d.rect((180, 360 + row * 54, 310, 400 + row * 54)),
				radius=self.d.size(5),
				fill=color,
			)
		self.text(draw, c.input_label, (130, 815, 490, 890), 44)
		self.text(draw, c.output_label, (1480, 815, 1810, 890), 44)
		count = max(before, c.branches)
		for i in reversed(range(count)):
			old_y = 530 if before == 1 else 280 + 500 * min(i, before - 1) / (before - 1)
			new_y = (
				530 if c.branches == 1 else 280 + 500 * min(i, c.branches - 1) / (c.branches - 1)
			)
			y = old_y + (new_y - old_y) * change
			visibility = (1 - change if i < before else 0) + (change if i < c.branches else 0)
			color = mix(GROUND, PALETTE[i % len(PALETTE)], visibility * built)
			if visibility * built <= 0:
				continue
			# Bezier fan: split one representation into independent views.
			points = []
			for step in range(61):
				u = step / 60
				points.append((340 + 570 * u, 530 + (y - 530) * smooth(u)))
			self.line(draw, points[: max(2, round(61 * built))], color, 3)
			radius = 70 / math.sqrt(before) * (1 - change) + 70 / math.sqrt(c.branches) * change
			self.dot(draw, 960, y, radius, color, outline=True)
			# Separate outputs meet at a concatenate/project glyph.
			right = [(1010 + 470 * j / 60, y + (530 - y) * smooth(j / 60)) for j in range(61)]
			self.line(draw, right[: max(1, round(61 * combine))], mix(GROUND, color, combine), 3)
			# One forward pass along each path, not decorative perpetual motion.
			travel = (t - self.time("branches", 0.2)) / max(
				0.5, self.time("combine", 0.65) - self.time("branches", 0.2)
			)
			if 0 < travel < 1:
				j = min(60, int(travel * 60))
				self.dot(draw, *points[j], 7, color)
			if combine > 0:
				j = min(60, round(combine * 60))
				self.dot(draw, *right[j], 5, color)
		for i in range(4):
			draw.rectangle(
				self.d.rect((1530 + i * 48, 445, 1565 + i * 48, 615)),
				fill=mix(GROUND, PALETTE[i], combine),
			)
		for i, point in enumerate(c.points):
			if point.state == "hidden":
				continue
			p = 1 if point.state == "context" else self.phase(t, f"point:{i}", 0.62)
			if point.state == "context" and any(q.state == "focus" for q in c.points):
				focus = next(j for j, q in enumerate(c.points) if q.state == "focus")
				p *= 1 - self.phase(t, f"point:{focus}", 0.62)
			if p > 0:
				self.text(
					draw, f"{point.value:g}", (1480, 255, 1810, 370), 92, mix(GROUND, ACCENT, p)
				)
			self.text(draw, c.y_label, (1480, 365, 1810, 415), 30, DIM)
		shown = before if change < 0.5 else c.branches
		self.text(
			draw,
			f"{shown} {c.branch_label.removesuffix('s') if shown == 1 else c.branch_label}",
			(740, 850, 1260, 930),
			56,
			ACCENT,
		)
		self.text(draw, "Schematic · information flow", (660, 950, 1450, 990), 24, DIM)

	def sweep(self, draw, t):
		c = self.c
		lo = math.floor(min(p.value for p in c.points))
		hi = math.ceil(max(p.value for p in c.points))
		if hi == lo:
			hi += 1
		left, right, top, bottom = 290, 1660, 290, 805
		self.line(draw, [(left, top - 35), (left, bottom), (right + 50, bottom)], DIM, 2)
		for i in range(5):
			value = lo + (hi - lo) * i / 4
			y = bottom - (bottom - top) * i / 4
			self.line(draw, [(left, y), (right, y)], mix(GROUND, DIM, 0.18), 1)
			self.text(draw, f"{value:g}", (125, y - 25, 260, y + 35), 32, DIM)
		self.text(draw, c.y_label, (110, 185, 850, 245), 38, DIM)
		self.text(draw, c.x_label, (780, 915, 1450, 975), 38, DIM)
		for i, p in enumerate(c.points):
			x = left + 160 + i * (right - left - 260) / max(len(c.points) - 1, 1)
			y = bottom - (p.value - lo) / (hi - lo) * (bottom - top)
			self.text(draw, p.label, (x - 90, 840, x + 180, 900), 42, DIM)
			if p.state == "hidden":
				continue
			visible = 1 if p.state == "context" else self.phase(t, f"point:{i}", 0.12 + i * 0.14)
			color = mix(GROUND, PALETTE[i % 4] if p.state == "focus" else DIM, visible)
			for mark in range(0, round((bottom - y) * visible), 18):
				self.line(
					draw, [(x, bottom - mark), (x, bottom - mark - 7)], mix(GROUND, color, 0.45), 2
				)
			self.dot(draw, x, y, 14 * visible, color)
			if p.state == "focus":
				self.dot(draw, x, y, 29 * visible, mix(GROUND, color, 0.5), outline=True)
			self.text(draw, f"{p.value:g}", (x - 65, y - 84, x + 180, y - 24), 58, color)
		# Points are discrete reported experiments; no interpolated trend line.

	def scope(self, draw, t):
		c = self.c
		# Separate experimental configurations, each with its own route to a test.
		for row in range(3):
			y = 350 + row * 180
			p = self.phase(t, "condition:0", 0.04)
			color = mix(GROUND, PALETTE[row], p)
			for col in range(row + 1):
				x = 235 + col * 95
				draw.rounded_rectangle(
					self.d.rect((x, y - 38, x + 65, y + 38)),
					radius=self.d.size(8),
					outline=color,
					width=self.d.size(3),
				)
			self.line(draw, [(550, y), (870, 530)], color, 3)
		self.text(draw, c.conditions[0], (120, 840, 730, 930), 43, ACCENT)
		p = self.phase(t, "condition:1", 0.3)
		for row in range(5):
			for col in range(4):
				color = mix(GROUND, (87, 211, 189), p)
				x, y = 920 + col * 52, 410 + row * 52
				draw.rectangle(
					self.d.rect((x, y, x + 36, y + 36)), outline=color, width=self.d.size(2)
				)
		self.text(draw, c.conditions[1], (835, 840, 1360, 930), 43, (87, 211, 189))
		p = self.phase(t, "condition:2", 0.55)
		color = mix(GROUND, BASE_TEXT, p)
		draw.rounded_rectangle(
			self.d.rect((1470, 425, 1660, 650)),
			radius=self.d.size(10),
			outline=color,
			width=self.d.size(3),
		)
		self.line(draw, [(1330, 310), (1330, 760)], mix(GROUND, DIM, 0.3), 2)
		self.text(draw, "Checkpoint", (1420, 685, 1770, 750), 34, color)
		# One checkpoint glyph. Label supplies the exact evaluation restriction.
		self.text(draw, "1", (1530, 480, 1630, 590), 76, color)
		self.text(draw, c.conditions[2], (1390, 840, 1810, 940), 43, BASE_TEXT)
		self.text(draw, "Schematic · separate configurations", (575, 950, 1510, 990), 24, DIM)
