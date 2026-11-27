"""arXiv Atom API client.

Deliberately hand-rolled over the Atom feed rather than using a wrapper library:
the query shape is simple, and we need precise control over rate limiting and
retries to stay a good citizen of a free API.

Two things learned from probing the live API and encoded here:
  - The `http://` endpoint 301-redirects; use `https://` directly.
  - `submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]` filters server-side, and an
    OR of categories is already de-duplicated by arXiv. We de-dupe again anyway
    because paging can overlap when new papers land mid-crawl.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime

import httpx

from .schemas import Paper

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"

NS = {
	"atom": "http://www.w3.org/2005/Atom",
	"opensearch": "http://a9.com/-/spec/opensearch/1.1/",
	"arxiv": "http://arxiv.org/schemas/atom",
}

# arXiv asks that API clients identify themselves.
USER_AGENT = "tabloid-ai/0.1 (arXiv paper video pipeline; contact via repo)"


def build_search_query(categories: list[str], start: datetime, end: datetime) -> str:
	cats = " OR ".join(f"cat:{c}" for c in categories)
	window = f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
	return f"({cats}) AND {window}"


def _text(el: ET.Element, path: str) -> str | None:
	found = el.findtext(path, namespaces=NS)
	return found.strip() if found else None


def parse_entry(entry: ET.Element) -> Paper | None:
	"""Convert one Atom <entry> into a Paper. Returns None for entries missing
	fields we cannot proceed without."""
	raw_id = _text(entry, "atom:id")
	title = _text(entry, "atom:title")
	abstract = _text(entry, "atom:summary")
	published = _text(entry, "atom:published")
	updated = _text(entry, "atom:updated")
	if not (raw_id and title and abstract and published):
		return None

	# "http://arxiv.org/abs/2608.06377v1" -> "2608.06377"
	arxiv_id = raw_id.rsplit("/", 1)[-1]
	if "v" in arxiv_id:
		arxiv_id = arxiv_id.rsplit("v", 1)[0]

	categories = [c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")]
	primary_el = entry.find("arxiv:primary_category", NS)
	primary = (
		primary_el.get("term") if primary_el is not None else (categories[0] if categories else "")
	)

	pdf_url = None
	for link in entry.findall("atom:link", NS):
		if link.get("title") == "pdf":
			pdf_url = link.get("href")
			break

	return Paper(
		arxiv_id=arxiv_id,
		title=title,
		abstract=abstract,
		authors=[
			a.findtext("atom:name", namespaces=NS) or "" for a in entry.findall("atom:author", NS)
		],
		categories=categories,
		primary_category=primary or "",
		submitted=datetime.fromisoformat(published.replace("Z", "+00:00")),
		updated=datetime.fromisoformat((updated or published).replace("Z", "+00:00")),
		comment=_text(entry, "arxiv:comment"),
		pdf_url=pdf_url,
	)


def parse_feed(xml_text: str) -> tuple[list[Paper], int]:
	"""Return (papers, total_results)."""
	root = ET.fromstring(xml_text)
	total_raw = root.findtext("opensearch:totalResults", namespaces=NS)
	total = int(total_raw) if total_raw and total_raw.isdigit() else 0
	papers = []
	for entry in root.findall("atom:entry", NS):
		p = parse_entry(entry)
		if p is not None:
			papers.append(p)
	return papers, total


class ArxivClient:
	def __init__(
		self,
		page_size: int = 200,
		delay_seconds: float = 3.0,
		max_retries: int = 5,
		client: httpx.Client | None = None,
		rate_limit_backoff_seconds: float = 30.0,
	) -> None:
		self.page_size = page_size
		self.delay_seconds = delay_seconds
		self.max_retries = max_retries
		# The first wait after a 429, doubling from there. See `_backoff`.
		self.rate_limit_backoff_seconds = rate_limit_backoff_seconds
		self._client = client or httpx.Client(
			timeout=60.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
		)

	def _get_page(self, search_query: str, start: int) -> tuple[list[Paper], int]:
		params = {
			"search_query": search_query,
			"start": start,
			"max_results": self.page_size,
			"sortBy": "submittedDate",
			"sortOrder": "descending",
		}
		last: Exception | None = None
		for attempt in range(self.max_retries):
			try:
				resp = self._client.get(API_URL, params=params)
				resp.raise_for_status()
				papers, total = parse_feed(resp.text)
				# arXiv intermittently returns an empty page for a valid offset;
				# retrying the same offset usually fixes it.
				if not papers and start < total:
					raise RuntimeError(f"empty page at offset {start} of {total}")
				return papers, total
			except Exception as e:
				last = e
				if attempt == self.max_retries - 1:
					break  # nothing left to retry; sleeping only delays the error
				wait = self._backoff(e, attempt)
				log.warning("arXiv page %s failed (%s); retrying in %.1fs", start, e, wait)
				time.sleep(wait)
		raise RuntimeError(f"arXiv request failed after {self.max_retries} attempts: {last}")

	def _backoff(self, exc: Exception, attempt: int) -> float:
		"""How long to wait before retrying, by what went wrong.

		A flaky connection clears in seconds and a linear back-off is right for
		it. **Being rate limited is a different animal**: arXiv has already
		decided to refuse us, and asking again forty-five seconds later just
		spends the retry budget confirming it. Measured the hard way - four window
		probes in one afternoon earned a 429, and a full run then failed at Stage 1
		having exhausted all five attempts inside a minute (D40).

		`Retry-After` is honoured when the server sends one, since that is the
		server telling us the answer rather than us guessing it.
		"""
		linear = self.delay_seconds * (attempt + 1)
		resp = getattr(exc, "response", None)
		if resp is None or getattr(resp, "status_code", None) not in (429, 503):
			return linear

		retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
		if retry_after:
			try:
				return max(float(retry_after), linear)
			except (TypeError, ValueError):
				pass
		# 30s, 60s, 120s, 240s: minutes, because that is the unit a rate limit
		# resets in. Capped so a run cannot hang for the better part of an hour.
		return min(self.rate_limit_backoff_seconds * (2**attempt), 300.0)

	def search(
		self, categories: list[str], start: datetime, end: datetime, max_papers: int = 3000
	) -> Iterator[Paper]:
		"""Page through results, de-duplicating by arXiv ID (cross-listed papers
		appear once per matching category in some responses)."""
		query = build_search_query(categories, start, end)
		seen: set[str] = set()
		offset = 0
		total = None

		while True:
			papers, reported_total = self._get_page(query, offset)
			if total is None:
				total = reported_total
				log.info("arXiv reports %s papers in window", total)
			if not papers:
				break

			for p in papers:
				if p.arxiv_id in seen:
					continue
				seen.add(p.arxiv_id)
				yield p
				if len(seen) >= max_papers:
					log.warning("Hit max_papers=%s; truncating window", max_papers)
					return

			offset += len(papers)
			if offset >= total:
				break
			time.sleep(self.delay_seconds)

	def close(self) -> None:
		self._client.close()
