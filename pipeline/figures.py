"""Figure normalisation: get every figure to PNG.

The renderer needs raster images, but arXiv sources ship PDF, EPS and PS vector
figures alongside PNG/JPG. What is available on this machine shapes the strategy:

    pdftoppm (poppler)  present  -> PDF -> PNG works
    ghostscript (gs)    ABSENT   -> EPS/PS cannot be rasterised
    ImageMagick         ABSENT   -> no general-purpose fallback

So PDF (the most common vector format in the sample papers, and the majority of
figures in one of them) converts, and EPS does not. Rather than fail, an
unconvertible figure is kept with `converted=False` so Stage 4 can still use its
caption and Stage 6 can avoid selecting it as a visual.

`pymupdf` is used when available as a pure-Python PDF rasteriser, which removes
the poppler dependency where it is installed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _pymupdf():
	"""Import pymupdf under whichever name this version exposes.

	1.24+ renamed the module from `fitz` to `pymupdf` and warns on the old name;
	older releases only have `fitz`. Returns None when it is not installed.
	"""
	try:
		import pymupdf

		return pymupdf
	except ImportError:
		try:
			import fitz

			return fitz
		except ImportError:
			return None


RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif"}
VECTOR_PDF_EXTS = {".pdf"}
# Convertible only with ghostscript, which is not installed here.
VECTOR_EPS_EXTS = {".eps", ".ps"}

# Rendering resolution for vector figures. 200 DPI gives a ~1700px-wide image for
# a full-column figure, enough to fill a 1920x1080 frame without upscaling.
RENDER_DPI = 200


def have_pdftoppm() -> bool:
	return shutil.which("pdftoppm") is not None


def _convert_with_pymupdf(src: Path, dest: Path, dpi: int) -> bool:
	fitz = _pymupdf()
	if fitz is None:
		return False
	try:
		with fitz.open(src) as doc:
			if not doc.page_count:
				return False
			pix = doc[0].get_pixmap(dpi=dpi)
			pix.save(dest)
		return dest.exists() and dest.stat().st_size > 0
	except Exception as e:
		log.debug("pymupdf failed on %s: %s", src.name, e)
		return False


def _convert_with_pdftoppm(src: Path, dest: Path, dpi: int) -> bool:
	if not have_pdftoppm():
		return False
	# pdftoppm appends "-<page>" to the prefix, so render to a temp prefix and
	# then move the single page we asked for into place.
	prefix = dest.with_suffix("")
	try:
		subprocess.run(
			[
				"pdftoppm",
				"-png",
				"-r",
				str(dpi),
				"-f",
				"1",
				"-l",
				"1",
				"-singlefile",
				str(src),
				str(prefix),
			],
			check=True,
			capture_output=True,
			timeout=120,
		)
	except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
		log.debug("pdftoppm failed on %s: %s", src.name, e)
		return False
	produced = prefix.with_suffix(".png")
	if produced != dest and produced.exists():
		produced.replace(dest)
	return dest.exists() and dest.stat().st_size > 0


def normalise(src: Path, dest_dir: Path, stem: str, dpi: int = RENDER_DPI) -> Path | None:
	"""Write a render-ready copy of `src` into `dest_dir` as `<stem>.<ext>`.

	Returns the path actually written, or None if the format cannot be handled.

	The *returned extension is the real one*. Raster inputs are copied rather
	than re-encoded (re-encoding a JPEG only loses quality), so a `.jpg` source
	stays `.jpg` - naming it `.png` would put JPEG bytes behind a PNG extension
	and mislead anything downstream that trusts the suffix.
	"""
	dest_dir.mkdir(parents=True, exist_ok=True)
	ext = src.suffix.lower()

	if ext in RASTER_EXTS:
		dest = dest_dir / f"{stem}{ext}"
		if src.resolve() != dest.resolve():
			shutil.copy2(src, dest)
		return dest if dest.exists() else None

	if ext in VECTOR_PDF_EXTS:
		dest = dest_dir / f"{stem}.png"
		# pymupdf first: no subprocess, and it is the more reliable of the two.
		if _convert_with_pymupdf(src, dest, dpi) or _convert_with_pdftoppm(src, dest, dpi):
			return dest
		return None

	if ext in VECTOR_EPS_EXTS:
		# Needs ghostscript, which is not installed on this machine.
		log.debug("Cannot rasterise %s: no ghostscript", src.name)
		return None

	return None


def extract_pdf_figures(
	pdf: Path,
	out_dir: Path,
	min_pixels: int = 40_000,
	min_side: int = 200,
	max_aspect: float = 6.0,
) -> list[Path]:
	"""Fallback figure extraction: pull embedded images out of the compiled PDF.

	Used when no LaTeX source is available. Quality is lower than the source
	route - there are no captions and no ordering guarantees - so callers mark
	these papers as degraded.

	Three filters, all of which earned their place on a real paper whose
	extraction otherwise returned mostly decorative slivers (median 1057x249):

	min_pixels drops logos and icons; min_side drops banners that are large in
	area but unusably thin; max_aspect drops rules and separators, which are
	extreme in one dimension. A figure failing these would not be shown on a
	1920x1080 frame anyway.
	"""
	fitz = _pymupdf()
	if fitz is None:
		log.warning("pymupdf not installed; cannot extract PDF figures. `uv pip install pymupdf`")
		return []

	out_dir.mkdir(parents=True, exist_ok=True)
	written: list[Path] = []
	seen: set[int] = set()
	try:
		with fitz.open(pdf) as doc:
			for page_index, page in enumerate(doc):
				for img in page.get_images(full=True):
					xref = img[0]
					if xref in seen:
						continue  # the same logo repeats on every page
					seen.add(xref)
					try:
						info = doc.extract_image(xref)
					except Exception:
						continue
					w, h = info.get("width", 0), info.get("height", 0)
					if w * h < min_pixels or min(w, h) < min_side:
						continue
					if max(w, h) / max(min(w, h), 1) > max_aspect:
						continue
					ext = info.get("ext", "png")
					dest = out_dir / f"p{page_index + 1:02d}_x{xref}.{ext}"
					dest.write_bytes(info["image"])
					written.append(dest)
	except Exception as e:
		log.warning("PDF figure extraction failed for %s: %s", pdf.name, e)
	return written
