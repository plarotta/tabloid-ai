"""A small, deterministic motion-design system for research explainers.

The model supplies content, never drawing code. Each layout builds a handful of
cached Pillow layers; motion reveals those layers in reading order, then holds
the completed argument. Figures stay intact and chart bars share a zero origin.
No camera zoom, generated data, web assets, or optional animation runtime.
"""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from ..schemas import Scene
from .slides import (
	_BOLD,
	_REGULAR,
	ACCENT,
	BASE,
	BASE_TEXT,
	BG,
	CARD,
	DIM,
	FG,
	SlideContext,
	fit_text,
	mix,
	truncate,
)

log = logging.getLogger(__name__)

BEAT_LABELS = {
	"hook": "THE FINDING",
	"context": "THE PROBLEM",
	"mechanism": "HOW IT WORKS",
	"evidence": "THE EVIDENCE",
	"caveat": "THE LIMIT",
	"payoff": "WHY IT MATTERS",
}


def ease(value: float) -> float:
	"""Cubic ease-out, with a stable hold before and after the move."""
	return 1 - (1 - min(max(value, 0.0), 1.0)) ** 3


@dataclass
class Layer:
	image: Image.Image
	xy: tuple[int, int]
	at: float
	seconds: float
	mode: str = "rise"


class EditorialScene:
	"""A seekable scene: the same time always produces the same frame."""

	def __init__(
		self,
		scene: Scene,
		ctx: SlideContext,
		duration: float,
		cues: dict[str, float] | None = None,
		previous_values: dict[str, str] | None = None,
	):
		self.scene, self.ctx = scene, ctx
		self.cues = cues or {}
		self.previous_values = previous_values or {}
		self.duration = max(duration, 0.001)
		self.layers: list[Layer] = []
		self.base = self._ground()
		self._chrome()
		{
			"title_card": self._title,
			"transition": self._transition,
			"result_callout": self._result,
			"figure": self._figure,
			"process": self._process,
			"contrast": self._contrast,
			"bullet_slide": self._bullets,
		}[scene.visual.type]()

	def x(self, n: float) -> int:
		return round(n * self.ctx.width / 1920)

	def y(self, n: float) -> int:
		return round(n * self.ctx.content_height / 1080)

	def size(self, n: float) -> int:
		return max(1, round(n * min(self.ctx.width / 1920, self.ctx.content_height / 1080)))

	def rect(self, coords) -> tuple[int, int, int, int]:
		return self.x(coords[0]), self.y(coords[1]), self.x(coords[2]), self.y(coords[3])

	def _ground(self) -> Image.Image:
		# Restrained vertical light; no random texture or animation behind the data.
		strip = Image.new("RGB", (1, self.ctx.height))
		for y in range(self.ctx.height):
			strip.putpixel((0, y), mix(BG, (10, 14, 23), y / max(self.ctx.height - 1, 1)))
		return strip.resize((self.ctx.width, self.ctx.height))

	def canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
		image = Image.new("RGBA", self.base.size)
		return image, ImageDraw.Draw(image)

	def add(self, image: Image.Image, at: float = 0, mode: str = "rise", cue: str = "") -> None:
		box = image.getbbox()
		if box:
			# Motion takes at most half a second. Later content has reading time:
			# everything has landed by 68% of the measured audio duration.
			start = self.cues.get(cue, at * self.duration)
			self.layers.append(
				Layer(
					image.crop(box),
					box[:2],
					start,
					min(0.48, self.duration * 0.12, max(0.001, self.duration - start)),
					mode,
				)
			)

	def text(self, draw, text, box, size=64, color=FG, heavy=True) -> None:
		left, top, right, bottom = self.rect(box)
		font, lines = fit_text(
			draw,
			text or "",
			_BOLD if heavy else _REGULAR,
			right - left,
			bottom - top,
			self.size(size),
			min_size=self.size(20),
		)
		# Ellipsis is a last resort for an unbroken token or malformed model output.
		line_h = max(1, round(font.size * 1.23))
		for line in lines[: max(1, (bottom - top) // line_h)]:
			draw.text((left, top), truncate(draw, line, font, right - left), font=font, fill=color)
			top += line_h

	def _chrome(self) -> None:
		d = ImageDraw.Draw(self.base)
		d.rounded_rectangle(self.rect((112, 67, 128, 83)), radius=self.size(3), fill=ACCENT)
		self.text(d, "ML PAPERS OF THE DAY", (146, 60, 1040, 105), 25)
		label = BEAT_LABELS.get(self.scene.beat, "RESEARCH BRIEF")
		self.text(d, label, (1410, 62, 1810, 100), 24, ACCENT)
		d.line(self.rect((112, 124, 1808, 124)), fill=mix(BG, ACCENT, 0.22), width=1)
		footer = f"arXiv:{self.ctx.arxiv_id}" if self.ctx.arxiv_id else "RESEARCH, EXPLAINED."
		self.text(d, footer, (112, 1004, 1000, 1043), 24, DIM, False)
		if self.ctx.scene_total:
			self.text(
				d,
				f"{self.ctx.scene_index + 1:02d} / {self.ctx.scene_total:02d}",
				(1660, 1004, 1810, 1043),
				24,
				DIM,
				False,
			)
		# Quiet section progress, kept outside the scientific content.
		if self.ctx.scene_total > 1:
			for i in range(self.ctx.scene_total):
				left = 112 + i * (1696 / self.ctx.scene_total)
				color = ACCENT if i <= self.ctx.scene_index else mix(BG, ACCENT, 0.18)
				d.rectangle(
					self.rect((left, 1068, left + 1696 / self.ctx.scene_total - 8, 1071)),
					fill=color,
				)

	def _heading(self, text: str) -> None:
		image, d = self.canvas()
		self.text(d, text, (112, 177, 1810, 306), 70)
		self.add(image)

	def _source(self, fallback: str = "") -> None:
		text = self.scene.visual.source or fallback
		if text:
			image, d = self.canvas()
			self.text(d, text, (112, 941, 1808, 986), 24, DIM, False)
			self.add(image, 0, "fade")

	def _title(self) -> None:
		v = self.scene.visual
		image, d = self.canvas()
		self.text(d, v.title or v.highlight or "A new finding", (112, 314, 1690, 725), 132)
		self.add(image)
		image, d = self.canvas()
		d.rectangle(self.rect((116, 269, 252, 277)), fill=ACCENT)
		self.add(image, 0, "wipe")
		if v.highlight and v.title and v.highlight != v.title:
			image, d = self.canvas()
			self.text(d, v.highlight, (118, 774, 1650, 894), 42, ACCENT, False)
			self.add(image, 0.38)
		self._source()

	def _transition(self) -> None:
		v = self.scene.visual
		d = ImageDraw.Draw(self.base)
		self.text(
			d,
			(v.highlight or "01").split("/")[0].strip(),
			(1170, 250, 1810, 920),
			420,
			mix(BG, ACCENT, 0.20),
		)
		image, d = self.canvas()
		self.text(d, "NEXT PAPER", (118, 312, 1000, 370), 28, ACCENT)
		self.text(d, v.title or "Next finding", (112, 420, 1340, 756), 102)
		self.add(image)
		image, d = self.canvas()
		d.rectangle(self.rect((118, 814, 1040, 819)), fill=ACCENT)
		self.add(image, 0.12, "wipe")

	def _bullets(self) -> None:
		v = self.scene.visual
		self._heading(v.title or BEAT_LABELS.get(self.scene.beat, "The key idea"))
		points = v.bullets[:4] or ([v.highlight] if v.highlight else [])
		step = min(154, 580 / max(len(points), 1))
		for i, point in enumerate(points):
			y = 342 + i * step
			image, d = self.canvas()
			self.text(d, f"{i + 1:02d}", (116, y + 12, 220, y + 84), 30, ACCENT)
			self.text(d, point, (256, y, 1780, y + step - 24), 51, FG, False)
			d.line(
				self.rect((254, y + step - 16, 1800, y + step - 16)),
				fill=mix(BG, ACCENT, 0.24),
				width=1,
			)
			self.add(image, 0.08 + 0.44 * i / max(len(points) - 1, 1))
		self._source()

	def _process(self) -> None:
		v = self.scene.visual
		if v.diagram:
			self._diagram()
			return
		points = v.bullets[:4]
		if not points:
			self._title()
			return
		self._heading(v.title or "How it works")
		gap, width = 60, (1696 - 60 * (len(points) - 1)) / len(points)
		for i, point in enumerate(points):
			x = 112 + i * (width + gap)
			at = 0.08 + 0.46 * i / max(len(points) - 1, 1)
			if i:
				image, d = self.canvas()
				d.line(self.rect((x - 46, 590, x - 14, 590)), fill=ACCENT, width=self.size(3))
				d.polygon(
					[
						(self.x(x - 12), self.y(590)),
						(self.x(x - 24), self.y(581)),
						(self.x(x - 24), self.y(599)),
					],
					fill=ACCENT,
				)
				self.add(image, max(0, at - 0.06), "wipe")
			image, d = self.canvas()
			d.rounded_rectangle(
				self.rect((x, 416, x + width, 763)),
				radius=self.size(22),
				fill=mix(BG, ACCENT, 0.07),
				outline=mix(BG, ACCENT, 0.48),
				width=self.size(2),
			)
			self.text(d, f"{i + 1:02d}", (x + 32, 450, x + width - 30, 510), 34, ACCENT)
			self.text(d, point, (x + 32, 556, x + width - 30, 718), 45)
			self.add(image, at)
		if v.highlight:
			image, d = self.canvas()
			self.text(d, v.highlight, (116, 826, 1800, 908), 36, ACCENT, False)
			self.add(image, 0.58)
		self._source("Method schematic")

	def _diagram(self) -> None:
		"""Keep object geometry stable; reveal state and supported output on cues."""
		v = self.scene.visual
		nodes = v.diagram.nodes
		self._heading(v.title or "Follow the mechanism")
		ground = self.base.copy()
		d = ImageDraw.Draw(self.base)
		gap = 84
		width = (1696 - gap * (len(nodes) - 1)) / len(nodes)
		for i, node in enumerate(nodes):
			x = 112 + i * (width + gap)
			if i:
				d.line(self.rect((x - 68, 590, x - 16, 590)), fill=ACCENT, width=self.size(4))
				d.polygon(
					[
						(self.x(x - 12), self.y(590)),
						(self.x(x - 28), self.y(580)),
						(self.x(x - 28), self.y(600)),
					],
					fill=ACCENT,
				)
			d.rounded_rectangle(
				self.rect((x, 416, x + width, 763)),
				radius=self.size(22),
				fill=mix(BG, ACCENT, 0.07),
				outline=mix(BG, ACCENT, 0.55),
				width=self.size(3),
			)
			self.text(d, node.label, (x + 28, 520, x + width - 28, 625), 66)
			if previous := self.previous_values.get(node.id):
				self.text(d, previous, (x + 28, 652, x + width - 28, 735), 54, ACCENT)
		# All nodes exist before state layers, so connector masks never erase labels.
		for i, node in enumerate(nodes):
			x = 112 + i * (width + gap)
			image, overlay = self.canvas()
			color = BASE if node.state == "removed" else ACCENT
			if node.state != "normal":
				overlay.rounded_rectangle(
					self.rect((x, 416, x + width, 763)),
					radius=self.size(22),
					outline=color,
					width=self.size(5),
				)
				self.text(
					overlay,
					"REMOVED" if node.state == "removed" else "FOCUS",
					(x + 28, 446, x + width - 28, 505),
					38,
					BASE_TEXT if node.state == "removed" else ACCENT,
				)
			if node.state == "removed":
				for left in ([x - gap] if i else []) + ([x + width] if i + 1 < len(nodes) else []):
					box = self.rect((left + 3, 568, left + gap - 3, 611))
					image.paste(ground.crop(box), box[:2])
					for offset in (16, 50):
						overlay.line(
							self.rect((left + offset, 590, left + offset + 16, 590)),
							fill=BASE,
							width=self.size(4),
						)
				overlay.line(
					self.rect((x + width - 58, 442, x + width - 28, 472)),
					fill=BASE,
					width=self.size(4),
				)
				overlay.line(
					self.rect((x + width - 58, 472, x + width - 28, 442)),
					fill=BASE,
					width=self.size(4),
				)
			if self.previous_values.get(node.id):
				overlay.rectangle(
					self.rect((x + 24, 646, x + width - 24, 740)), fill=mix(BG, ACCENT, 0.07)
				)
			if node.value:
				self.text(
					overlay,
					node.value,
					(x + 28, 652, x + width - 28, 735),
					54,
					BASE_TEXT if node.state == "removed" else ACCENT,
				)
			self.add(image, 0.08 + 0.15 * i, "fade", cue=f"node:{node.id}")
		if v.highlight:
			image, overlay = self.canvas()
			self.text(overlay, v.highlight, (116, 814, 1800, 924), 50, FG)
			self.add(image, 0.58, "fade", cue="highlight")
		self._source("Method schematic")

	def _contrast(self) -> None:
		v = self.scene.visual
		self._heading(v.title or "The distinction")
		for i, text in enumerate(v.bullets[:2]):
			x, color = 112 + i * 880, BASE if i == 0 else ACCENT
			image, d = self.canvas()
			d.rounded_rectangle(
				self.rect((x, 359, x + 816, 793)),
				radius=self.size(22),
				fill=mix(BG, color, 0.08),
				outline=mix(BG, color, 0.32),
				width=self.size(2),
			)
			d.rectangle(self.rect((x + 36, 399, x + 118, 405)), fill=color)
			self.text(d, text, (x + 38, 495, x + 777, 730), 76, BASE_TEXT if i == 0 else FG)
			self.add(image, 0.06 if i == 0 else 0.34)
		if v.highlight:
			image, d = self.canvas()
			self.text(d, v.highlight, (116, 844, 1800, 912), 36, ACCENT, False)
			self.add(image, 0.55)
		self._source()

	def _result(self) -> None:
		v, c = self.scene.visual, self.scene.visual.comparison
		if c is None:
			if v.title:
				self._heading(v.title)
			image, d = self.canvas()
			self.text(
				d, v.highlight or v.title or "The finding", (112, 383, 1804, 760), 145, ACCENT
			)
			self.add(image, 0.07)
			self._source()
			return
		self._heading(v.title or c.label_a)
		if c.template == "two_bar":
			maximum = max(c.value_a, c.value_b)
			for i, (label, value, color) in enumerate(
				[(c.label_a, c.value_a, ACCENT), (c.label_b, c.value_b, BASE)]
			):
				y = 385 + i * 224
				image, d = self.canvas()
				self.text(d, label, (116, y, 1430, y + 67), 35, FG, False)
				self.add(image, 0.04 + i * 0.20, "fade")
				image, d = self.canvas()
				d.rectangle(self.rect((116, y + 81, 1376, y + 158)), fill=mix(BG, FG, 0.06))
				self.add(image, 0.04, "fade")
				image, d = self.canvas()
				# One common, zero-based scale. No minimum bar width distortion.
				d.rectangle(
					self.rect((116, y + 81, 116 + 1260 * value / maximum, y + 158)), fill=color
				)
				self.add(image, 0.10 + i * 0.20, "wipe")
				image, d = self.canvas()
				self.text(d, f"{value:g}", (1430, y + 67, 1800, y + 179), 83, color)
				self.add(image, 0.20 + i * 0.20)
		elif c.template == "split":
			image, d = self.canvas()
			cut = 112 + 1696 * c.value_a / 100
			d.rectangle(self.rect((112, 530, cut, 631)), fill=ACCENT)
			d.rectangle(self.rect((cut, 530, 1808, 631)), fill=BASE)
			self.add(image, 0.16, "wipe")
			for i, (label, value, color) in enumerate(
				[(c.label_a, c.value_a, ACCENT), (c.label_b, 100 - c.value_a, BASE_TEXT)]
			):
				x = 112 + i * 920
				image, d = self.canvas()
				self.text(d, f"{value:g}%", (x, 368, x + 750, 498), 102, color)
				self.text(d, label, (x, 671, x + 750, 800), 36, FG, False)
				self.add(image, 0.09 + i * 0.24)
		else:
			image, d = self.canvas()
			self.text(d, f"{c.value_a:g}", (112, 339, 1770, 690), 246, ACCENT)
			self.add(image, 0.10)
		if c.unit or c.note:
			image, d = self.canvas()
			self.text(
				d,
				"  /  ".join(x for x in (c.unit, c.note) if x),
				(116, 831, 1800, 911),
				36,
				DIM,
				False,
			)
			self.add(image, 0.53)
		self._source()

	def _figure(self) -> None:
		v, ctx = self.scene.visual, self.ctx
		self._heading(v.title or "The evidence")
		image, d = self.canvas()
		box = self.rect((112, 314, 1808, 867))
		d.rounded_rectangle(box, radius=self.size(16), fill=CARD)
		try:
			if not ctx.figures_dir or not v.figure_file:
				raise FileNotFoundError("No figure provided")
			with Image.open(ctx.figures_dir / Path(v.figure_file).name) as original:
				fig = original.convert("RGBA")
				flat = Image.new("RGBA", fig.size, CARD)
				flat.alpha_composite(fig)
				pad = self.size(24)
				flat.thumbnail(
					(box[2] - box[0] - 2 * pad, box[3] - box[1] - 2 * pad), Image.Resampling.LANCZOS
				)
				image.alpha_composite(
					flat,
					((box[0] + box[2] - flat.width) // 2, (box[1] + box[3] - flat.height) // 2),
				)
		except (OSError, ValueError) as e:
			log.warning("Figure unavailable: %s", e)
			self.text(d, "Figure unavailable", (180, 500, 1660, 640), 60, BG)
		self.add(image, 0.04, "fade")
		if v.highlight:
			image, d = self.canvas()
			self.text(d, v.highlight, (116, 891, 1808, 944), 32, ACCENT)
			self.add(image, 0.50)
		# No fabricated figure number inferred from a filename with unrelated digits.
		self._source(f"Paper figure · arXiv:{ctx.arxiv_id}" if ctx.arxiv_id else "Paper figure")

	def frame(self, seconds: float) -> Image.Image:
		image = self.base.copy()
		for layer in self.layers:
			p = ease((seconds - layer.at) / max(layer.seconds, 0.001))
			if p <= 0:
				continue
			piece, (x, y) = layer.image, layer.xy
			if p < 1:
				if layer.mode == "wipe":
					piece = piece.crop((0, 0, max(1, round(piece.width * p)), piece.height))
				else:
					piece = piece.copy()
					piece.putalpha(piece.getchannel("A").point(lambda a, p=p: round(a * p)))
					if layer.mode == "rise":
						y += round(self.size(28) * (1 - p))
			image.paste(piece, (x, y), piece)
		return image

	def poster(self) -> Image.Image:
		return self.frame(self.duration)


def render_clip(
	scene: Scene,
	ctx: SlideContext,
	duration: float,
	out: Path,
	fps: int = 30,
	timeout: int = 120,
	cues: dict[str, float] | None = None,
	previous_values: dict[str, str] | None = None,
) -> Path | None:
	"""Encode cached layers in a bounded worker; failures leave a usable poster.

	The parent renderer invokes this module in a subprocess to enforce the whole
	deadline, including frame generation and pipe writes (not just encoder exit).
	"""
	import json
	import sys
	from dataclasses import asdict

	out.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(prefix="tabloid-editorial-") as tmp:
		spec = Path(tmp) / "scene.json"
		spec.write_text(
			json.dumps(
				{
					"scene": scene.model_dump(),
					"ctx": asdict(ctx),
					"duration": duration,
					"out": str(out.resolve()),
					"fps": fps,
					"cues": cues or {},
					"previous_values": previous_values or {},
				},
				default=str,
			),
			encoding="utf-8",
		)
		try:
			subprocess.run(
				[sys.executable, "-m", "pipeline.render.editorial", str(spec)],
				capture_output=True,
				text=True,
				check=True,
				timeout=timeout,
			)
			return out
		except (OSError, subprocess.SubprocessError) as e:
			log.warning("Editorial motion failed for %s; keeping its poster: %s", scene.id, e)
			out.unlink(missing_ok=True)
			return None


def _encode(design: EditorialScene, out: Path, duration: float, fps: int) -> None:
	frames = max(1, math.ceil(duration * fps))
	cmd = [
		"ffmpeg",
		"-hide_banner",
		"-loglevel",
		"error",
		"-y",
		"-f",
		"rawvideo",
		"-pix_fmt",
		"rgb24",
		"-s",
		f"{design.ctx.width}x{design.ctx.height}",
		"-r",
		str(fps),
		"-i",
		"pipe:0",
		"-an",
		"-c:v",
		"libx264",
		"-threads",
		"2",
		"-preset",
		"veryfast",
		"-crf",
		"18",
		"-pix_fmt",
		"yuv420p",
		"-movflags",
		"+faststart",
		str(out),
	]
	# A file prevents stderr from filling a pipe and deadlocking a failed encoder.
	with (
		tempfile.TemporaryFile() as errors,
		subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errors) as proc,
	):
		try:
			last_key, data = None, b""
			for i in range(frames):
				t = i / fps
				key = tuple(round(ease((t - x.at) / x.seconds), 5) for x in design.layers)
				if key != last_key:
					data = design.frame(t).tobytes()
					last_key = key
				proc.stdin.write(data)
		finally:
			proc.stdin.close()
		if proc.wait() != 0:
			errors.seek(0)
			raise RuntimeError(errors.read().decode()[-1000:])


if __name__ == "__main__":
	import json
	import sys

	payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
	context = payload["ctx"]
	if context.get("figures_dir"):
		context["figures_dir"] = Path(context["figures_dir"])
	design = EditorialScene(
		Scene.model_validate(payload["scene"]),
		SlideContext(**context),
		payload["duration"],
		payload.get("cues"),
		payload.get("previous_values"),
	)
	_encode(design, Path(payload["out"]), payload["duration"], payload["fps"])
