"""Does this machine have what Stage 8 needs?

The rest of the suite fakes ffmpeg and never checks which font it got, which is
the right trade for fast unit tests but leaves the two things most likely to
differ between a developer's Mac and a CI runner completely uncovered:

  fonts   `_font()` falls back to Pillow's built-in bitmap face when it finds
          nothing in its candidate list. That fallback does not raise - it just
          silently renders an episode in tiny unreadable type.
  ffmpeg  every video in the pipeline goes through it.

These tests skip when a dependency is genuinely absent, so a laptop without
ffmpeg still gets a green suite. On Linux CI they are the check that the
scheduled workflow can actually produce an episode.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from PIL import ImageFont

from pipeline.render.slides import SlideContext, bold, regular, title_card
from pipeline.tts.providers import measure_duration

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_a_real_scalable_font_is_available():
	"""Pillow's default face is a small bitmap font. Falling back to it produces
	an episode nobody can read, and nothing else in the pipeline complains."""
	for font in (bold(64), regular(32)):
		assert isinstance(font, ImageFont.FreeTypeFont), (
			"No system font found. On Debian/Ubuntu install `fonts-dejavu-core`; "
			"the slide renderer's candidate list expects DejaVu there."
		)


def test_a_title_card_renders_with_real_text():
	ctx = SlideContext(width=640, height=360)
	img = title_card(ctx, "Hijacking robots with a piece of paper", "2608.05715")
	assert img.size == (640, 360)
	# More than a handful of distinct colours means text and chrome actually drew;
	# a blank card would be one or two.
	assert len(img.convert("RGB").getcolors(maxcolors=100_000) or []) > 8


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
def test_ffmpeg_encodes_a_slide_into_a_playable_clip(tmp_path):
	"""The end-to-end shape of what Stage 8 does: a PNG in, an H.264 clip out,
	with a duration something else can measure."""
	ctx = SlideContext(width=640, height=360)
	png = tmp_path / "slide.png"
	title_card(ctx, "Portability check").save(png, "PNG")

	out = tmp_path / "clip.mp4"
	subprocess.run(
		[
			"ffmpeg",
			"-hide_banner",
			"-loglevel",
			"error",
			"-y",
			"-loop",
			"1",
			"-i",
			str(png),
			"-t",
			"1",
			"-r",
			"30",
			"-c:v",
			"libx264",
			"-pix_fmt",
			"yuv420p",
			str(out),
		],
		capture_output=True,
		text=True,
		timeout=120,
		check=True,
	)

	assert out.exists() and out.stat().st_size > 0
	# measure_duration is what Stage 8 syncs the timeline to, so exercise it
	# rather than trusting the file exists.
	assert 0.9 <= measure_duration(out) <= 1.6


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
def test_libx264_is_compiled_in():
	"""ffmpeg builds vary. The render config asks for h264 by name, so an ffmpeg
	without it fails at the last stage of a run that already spent money."""
	out = subprocess.run(
		["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60
	).stdout
	assert "libx264" in out
