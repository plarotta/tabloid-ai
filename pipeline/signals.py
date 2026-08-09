"""Free external signals used to break ties during ranking.

All three are free and unauthenticated. None is load-bearing: every lookup
degrades to "unknown" rather than failing the paper, because a ranking signal is
not worth stopping an episode over.

Hugging Face is queried through `https://huggingface.co/api/papers/<id>` rather
than by scraping the daily-papers page as the spec suggested. It is an official
JSON endpoint, it answers per-paper (so no date-range bookkeeping), and it
carries the upvote count, which is a stronger signal than mere presence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

HF_PAPER_API = "https://huggingface.co/api/papers/{arxiv_id}"

USER_AGENT = "tabloid-ai/0.1 (arXiv paper video pipeline; contact via repo)"

# Deliberately anchored to github.com so we do not pick up arbitrary URLs. The
# trailing strip handles the very common "...github.com/org/repo." at a sentence
# end and markdown/LaTeX punctuation.
_GITHUB_RE = re.compile(
	r"https?://(?:www\.)?github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)", re.IGNORECASE
)

_PROJECT_PAGE_RE = re.compile(
	r"https?://[A-Za-z0-9.-]+\.github\.io/[A-Za-z0-9._/-]*", re.IGNORECASE
)


@dataclass(slots=True)
class PaperSignals:
	github_url: str | None = None
	project_page: str | None = None
	huggingface: bool = False
	hf_upvotes: int = 0
	hf_lookup_ok: bool = False
	affiliations: list[str] = field(default_factory=list)


def find_github(*texts: str | None) -> str | None:
	"""First GitHub repo URL mentioned across the given texts."""
	for text in texts:
		if not text:
			continue
		m = _GITHUB_RE.search(text)
		if m:
			org, repo = m.group(1), m.group(2).rstrip(".,;:)}]")
			if repo.lower() in {"com", "io"}:
				continue
			return f"https://github.com/{org}/{repo}"
	return None


def find_project_page(*texts: str | None) -> str | None:
	for text in texts:
		if not text:
			continue
		m = _PROJECT_PAGE_RE.search(text)
		if m:
			return m.group(0).rstrip(".,;:)}]")
	return None


def huggingface_lookup(arxiv_id: str, client: httpx.Client | None = None) -> tuple[bool, int, bool]:
	"""Return (featured, upvotes, lookup_ok).

	A 404 is a definitive "not featured" and counts as a successful lookup. Any
	other failure sets lookup_ok=False so the ranking prompt can be told the
	signal is missing rather than negative.
	"""
	owned = client is None
	client = client or httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
	try:
		resp = client.get(HF_PAPER_API.format(arxiv_id=arxiv_id))
		if resp.status_code == 404:
			return False, 0, True
		resp.raise_for_status()
		data = resp.json()
		if not isinstance(data, dict):
			return False, 0, True
		return True, int(data.get("upvotes") or 0), True
	except Exception as e:
		log.debug("Hugging Face lookup failed for %s: %s", arxiv_id, e)
		return False, 0, False
	finally:
		if owned:
			client.close()


def collect(
	arxiv_id: str,
	abstract: str | None = None,
	comment: str | None = None,
	intro: str | None = None,
	client: httpx.Client | None = None,
) -> PaperSignals:
	"""Gather every signal for one paper. Never raises."""
	featured, upvotes, ok = huggingface_lookup(arxiv_id, client)
	return PaperSignals(
		github_url=find_github(comment, abstract, intro),
		project_page=find_project_page(comment, abstract, intro),
		huggingface=featured,
		hf_upvotes=upvotes,
		hf_lookup_ok=ok,
	)
