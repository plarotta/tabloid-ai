"""arXiv OAI-PMH harvester.

The second way into arXiv, added when the first one stopped letting us in.

`arxiv.py` talks to the Atom search API, which is the right interface for this
job and was the only one until 2026-09-13, when that endpoint began answering
every request from this address with 429 and kept doing so for more than a day.
The website, the RSS feeds and **this** endpoint all answered normally
throughout, so the block was scoped to the search API rather than to us. OAI-PMH
is the interface arXiv publishes for bulk harvesting, which is closer to what
Stage 1 actually does than a search query is.

Two things about it shape everything here.

**`from`/`until` filter on the record's datestamp, which is when the record was
last touched.** A harvest of one day returns everything modified that day,
including papers from 2013 whose metadata was corrected. So the datestamp range
is a net, not the window: what comes back is filtered here on the real
submission date.

**Announcement lags submission by about three days.** Measured on a live
harvest: a 2026-09-14 datestamp carried papers submitted on the 10th and 11th,
and nothing newer. A window is therefore never complete up to today, and the
marker has to follow the newest paper actually seen rather than the clock -
which is what D31 already made it do for the index lag on the other endpoint.

**Use `arXivRaw`, never `arXiv`.** The simpler `arXiv` metadata format has a
`<created>` field that looks like the submission date and is not: on the live
sample it disagreed with the paper's own identifier 331 times out of 960, and
spot checks showed why - `1912.08786` is a December 2019 paper and the field
read 2026-09-10, the date of its sixth version. Filtering a window on it would
have quietly presented years-old papers as this week's news, which is the exact
failure this pipeline cannot have. `arXivRaw` lists every version with its own
timestamp, so v1's date is the submission and it carries a time of day - which
also keeps the window marker at the precision D31 built it for.

Sets are coarse - `cs`, `stat` - so the five configured categories are filtered
here rather than by the server, and a paper in two harvested sets is de-duped by
id.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .schemas import Paper

log = logging.getLogger(__name__)

OAI_URL = "https://export.arxiv.org/oai2"

NS = {
	"oai": "http://www.openarchives.org/OAI/2.0/",
	"raw": "http://arxiv.org/OAI/arXivRaw/",
}

# arXiv asks that clients identify themselves, and it is worth keeping honest:
# when the search endpoint was refusing us, a request naming this pipeline got an
# immediate 429 while one wearing a browser user agent was dropped without a
# reply. Being identifiable is treated better, not worse.
USER_AGENT = "tabloid-ai/0.1 (arXiv paper video pipeline; contact via repo)"

# arXiv's OAI-PMH set for each category prefix we care about.
SET_FOR_PREFIX = {"cs": "cs", "stat": "stat", "math": "math", "eess": "eess"}


def sets_for_categories(categories: list[str]) -> list[str]:
	"""The smallest group of OAI sets covering every configured category.

	A cross-listed paper appears in the set of each of its categories, so
	harvesting `cs` alone would miss one that is primarily `stat.ML` and never
	cross-listed into cs.
	"""
	out: list[str] = []
	for c in categories:
		s = SET_FOR_PREFIX.get(c.split(".")[0])
		if s and s not in out:
			out.append(s)
	return out


def _rfc2822(raw: str | None) -> datetime | None:
	"""Parse one version's date. They arrive as `Thu, 18 Mar 2021 03:31:33 GMT`."""
	if not raw:
		return None
	try:
		dt = parsedate_to_datetime(raw.strip())
	except (TypeError, ValueError):
		return None
	return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _split_authors(raw: str | None) -> list[str]:
	"""`arXivRaw` gives one string where the Atom feed gives elements.

	Split on commas and a trailing "and", which is how arXiv writes them. Names
	carrying a comma of their own will split wrongly; that costs a malformed
	author string in a credit nobody reads, where getting the date wrong would
	cost an episode.
	"""
	if not raw:
		return []
	text = raw.replace("\n", " ")
	parts: list[str] = []
	for chunk in text.split(","):
		for piece in chunk.split(" and "):
			name = " ".join(piece.split())
			if name:
				parts.append(name)
	return parts


def parse_record(record: ET.Element) -> Paper | None:
	"""Convert one OAI `<record>` into a Paper, or None if it cannot be used.

	Deleted records carry a status on the header and no metadata at all, which is
	the usual reason this returns None.
	"""
	meta = record.find("oai:metadata/raw:arXivRaw", NS)
	if meta is None:
		return None

	def text(path: str) -> str | None:
		found = meta.findtext(path, namespaces=NS)
		return found.strip() if found else None

	arxiv_id = text("raw:id")
	title = text("raw:title")
	abstract = text("raw:abstract")
	if not (arxiv_id and title and abstract):
		return None

	# Every version, in the order arXiv lists them. v1 is the submission; the
	# last one is the current revision.
	dates = [
		d
		for d in (
			_rfc2822(v.findtext("raw:date", namespaces=NS))
			for v in meta.findall("raw:version", NS)
		)
		if d is not None
	]
	if not dates:
		return None

	# Space-separated, primary first - the same order the Atom feed reports.
	cats = (text("raw:categories") or "").split()
	if not cats:
		return None

	return Paper(
		arxiv_id=arxiv_id,
		title=title,
		abstract=abstract,
		authors=_split_authors(text("raw:authors")),
		categories=cats,
		primary_category=cats[0],
		submitted=dates[0],
		updated=max(dates),
		comment=text("raw:comments"),
		pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
	)


def parse_page(xml: str) -> tuple[list[Paper], str | None]:
	"""Parse one ListRecords response into papers and the next resumption token.

	An empty `<resumptionToken/>` means this was the last page; arXiv sends the
	element either way, so its absence and its emptiness both end the harvest.
	"""
	root = ET.fromstring(xml)

	error = root.find("oai:error", NS)
	if error is not None:
		code = error.get("code", "")
		# An empty window is a legitimate answer, not a failure.
		if code == "noRecordsMatch":
			return [], None
		raise RuntimeError(f"OAI-PMH error {code}: {(error.text or '').strip()}")

	papers = []
	for record in root.findall("oai:ListRecords/oai:record", NS):
		p = parse_record(record)
		if p is not None:
			papers.append(p)

	token = root.findtext("oai:ListRecords/oai:resumptionToken", namespaces=NS)
	token = (token or "").strip()
	return papers, token or None


class OaiClient:
	def __init__(
		self,
		delay_seconds: float = 3.0,
		max_retries: int = 5,
		client: httpx.Client | None = None,
		rate_limit_backoff_seconds: float = 30.0,
	) -> None:
		self.delay_seconds = delay_seconds
		self.max_retries = max_retries
		self.rate_limit_backoff_seconds = rate_limit_backoff_seconds
		# Pages are megabytes and arXiv takes twenty seconds to assemble one, so
		# the timeout is generous where the Atom client's did not need to be.
		self._client = client or httpx.Client(
			timeout=180.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
		)

	def close(self) -> None:
		self._client.close()

	def _get(self, params: dict) -> str:
		last: Exception | None = None
		for attempt in range(self.max_retries):
			try:
				resp = self._client.get(OAI_URL, params=params)
				resp.raise_for_status()
				return resp.text
			except Exception as e:
				last = e
				if attempt == self.max_retries - 1:
					break
				wait = self._backoff(e, attempt)
				log.warning("OAI request failed (%s); retrying in %.1fs", e, wait)
				time.sleep(wait)
		raise RuntimeError(f"OAI request failed after {self.max_retries} attempts: {last}")

	def _backoff(self, exc: Exception, attempt: int) -> float:
		"""Wait by what went wrong, as in the Atom client (D40).

		OAI-PMH differs in one way that matters: a 503 carrying `Retry-After` is
		how this protocol does **flow control**, not how it reports trouble. arXiv
		uses it to pace a large harvest, so honouring that header exactly is the
		protocol working rather than a server in difficulty.
		"""
		linear = self.delay_seconds * (attempt + 1)
		resp = getattr(exc, "response", None)
		if resp is None or getattr(resp, "status_code", None) not in (429, 503):
			return linear

		retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
		if retry_after:
			try:
				return max(float(retry_after), self.delay_seconds)
			except (TypeError, ValueError):
				pass
		return min(self.rate_limit_backoff_seconds * (2**attempt), 300.0)

	def harvest(
		self,
		categories: list[str],
		start: datetime,
		end: datetime,
		max_papers: int = 3000,
	) -> Iterator[Paper]:
		"""Papers **submitted** in the window, harvested over datestamps.

		The datestamp net runs from the window start to the requested end, because
		a paper submitted on the first day of the window may not have been
		announced until several days later. The filter is on v1's timestamp, which
		is what the window is actually about.
		"""
		wanted = set(categories)
		seen: set[str] = set()
		kept = scanned = 0

		for oai_set in sets_for_categories(categories):
			params: dict = {
				"verb": "ListRecords",
				"metadataPrefix": "arXivRaw",
				"set": oai_set,
				"from": start.date().isoformat(),
				"until": end.date().isoformat(),
			}
			pages = 0
			while True:
				xml = self._get(params)
				papers, token = parse_page(xml)
				pages += 1
				scanned += len(papers)
				log.info(
					"OAI set %s page %s: %s records scanned, %s kept so far, %s",
					oai_set,
					pages,
					len(papers),
					kept,
					"more to come" if token else "last page",
				)

				for p in papers:
					if p.arxiv_id in seen:
						continue
					if not wanted & set(p.categories):
						continue
					if not (start <= p.submitted <= end):
						continue
					seen.add(p.arxiv_id)
					kept += 1
					yield p
					if kept >= max_papers:
						log.warning("Hit max_papers=%s; stopping the harvest", max_papers)
						return

				if not token:
					break
				# A resumption token replaces every other argument; sending them
				# alongside it is an error in the protocol.
				params = {"verb": "ListRecords", "resumptionToken": token}
				time.sleep(self.delay_seconds)

		log.info("Harvest scanned %s records, kept %s in window", scanned, kept)
