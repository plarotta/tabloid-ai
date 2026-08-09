"""Downloading and unpacking arXiv e-print archives.

`https://arxiv.org/e-print/<id>` returns whatever the author submitted, and that
is not always a tarball. Observed and handled here:

  - gzipped tar  - the common case, and what all three sample papers used
  - bare gzipped .tex - single-file submissions
  - PDF - authors who submitted only a PDF; no LaTeX to parse
  - HTML error page - withdrawn papers and rate-limit responses

Unpacking is guarded against path traversal: a tar entry may name `../../etc`,
and we extract from a source we do not control.
"""

from __future__ import annotations

import gzip
import logging
import tarfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

EPRINT_URL = "https://arxiv.org/e-print/{arxiv_id}"
PDF_URL = "https://arxiv.org/pdf/{arxiv_id}"

USER_AGENT = "tabloid-ai/0.1 (arXiv paper video pipeline; contact via repo)"

# arXiv is a free service and this stage hits it once per shortlisted paper.
DEFAULT_DELAY = 3.0


class EPrintError(RuntimeError):
	"""The archive could not be retrieved or unpacked."""


def _is_within(base: Path, target: Path) -> bool:
	try:
		target.resolve().relative_to(base.resolve())
		return True
	except (ValueError, OSError):
		return False


def safe_extract_tar(archive: Path, dest: Path) -> int:
	"""Extract a tarball, skipping entries that escape `dest` or are not regular
	files. Returns the number of files written."""
	dest.mkdir(parents=True, exist_ok=True)
	written = 0
	with tarfile.open(archive, "r:*") as tf:
		for member in tf.getmembers():
			if not (member.isfile() or member.isdir()):
				continue  # skip links/devices entirely
			out = dest / member.name
			if not _is_within(dest, out):
				log.warning("Skipping unsafe tar entry %r", member.name)
				continue
			try:
				tf.extract(member, dest, filter="data")
			except TypeError:  # filter= added in 3.12; kept for older runtimes
				tf.extract(member, dest)
			except Exception as e:
				log.debug("Could not extract %r: %s", member.name, e)
				continue
			if member.isfile():
				written += 1
	return written


def unpack(archive: Path, dest: Path) -> str:
	"""Unpack `archive` into `dest`, returning the detected kind.

	Returns one of: "tar", "tex" (single gzipped source), "pdf", "unknown".
	"""
	dest.mkdir(parents=True, exist_ok=True)
	head = archive.read_bytes()[:5]

	if head[:4] == b"%PDF":
		target = dest / f"{archive.stem}.pdf"
		target.write_bytes(archive.read_bytes())
		return "pdf"

	if tarfile.is_tarfile(archive):
		safe_extract_tar(archive, dest)
		return "tar"

	if head[:2] == b"\x1f\x8b":
		# gzip, but not a tar: a single-file submission.
		try:
			raw = gzip.decompress(archive.read_bytes())
		except OSError as e:
			raise EPrintError(f"Corrupt gzip archive: {e}") from e
		if raw[:4] == b"%PDF":
			(dest / f"{archive.stem}.pdf").write_bytes(raw)
			return "pdf"
		(dest / "main.tex").write_bytes(raw)
		return "tex"

	raise EPrintError(f"Unrecognised e-print format (starts with {head!r})")


class EPrintFetcher:
	def __init__(
		self,
		delay_seconds: float = DEFAULT_DELAY,
		max_retries: int = 3,
		client: httpx.Client | None = None,
	) -> None:
		self.delay_seconds = delay_seconds
		self.max_retries = max_retries
		self._client = client or httpx.Client(
			timeout=120.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
		)

	def _download(self, url: str, dest: Path) -> Path:
		last: Exception | None = None
		for attempt in range(self.max_retries):
			try:
				resp = self._client.get(url)
				resp.raise_for_status()
				body = resp.content
				if not body:
					raise EPrintError("empty response body")
				# arXiv serves an HTML page for withdrawn papers and rate limits.
				ctype = resp.headers.get("content-type", "")
				if "text/html" in ctype:
					raise EPrintError(f"got HTML instead of an archive ({url})")
				dest.write_bytes(body)
				return dest
			except Exception as e:
				last = e
				wait = self.delay_seconds * (attempt + 1)
				log.warning("Download failed (%s); retrying in %.1fs", e, wait)
				time.sleep(wait)
		raise EPrintError(f"Failed to download {url} after {self.max_retries} attempts: {last}")

	def fetch_source(self, arxiv_id: str, work_dir: Path) -> tuple[Path, str]:
		"""Download and unpack the e-print. Returns (source_dir, kind)."""
		work_dir.mkdir(parents=True, exist_ok=True)
		archive = work_dir / f"{arxiv_id}.archive"
		self._download(EPRINT_URL.format(arxiv_id=arxiv_id), archive)
		source_dir = work_dir / "source"
		kind = unpack(archive, source_dir)
		archive.unlink(missing_ok=True)
		return source_dir, kind

	def fetch_pdf(self, arxiv_id: str, dest: Path) -> Path:
		"""Download the compiled PDF, used for the figure-extraction fallback."""
		dest.parent.mkdir(parents=True, exist_ok=True)
		return self._download(PDF_URL.format(arxiv_id=arxiv_id), dest)

	def close(self) -> None:
		self._client.close()
