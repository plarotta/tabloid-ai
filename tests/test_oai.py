"""Stage 1's second interface: the OAI-PMH harvester (D45).

The fixtures are real records captured from a live harvest, trimmed to a couple
per page. `oai_page1.xml` deliberately pairs a September 2026 submission with
`1912.08786` - a December 2019 paper whose sixth version landed in the same
harvest. That pairing is the whole point of these tests: the metadata format
this module *does not* use reports the latter's date as 2026-09-10, and a window
filtered on it would have put a seven-year-old paper in this week's episode.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pipeline.oai import (
	OaiClient,
	parse_page,
	parse_record,
	sets_for_categories,
)

FIXTURES = Path(__file__).parent / "fixtures"
CATEGORIES = ["cs.LG", "cs.CL", "cs.CV", "cs.AI", "stat.ML"]


def fixture(name: str) -> str:
	return (FIXTURES / name).read_text(encoding="utf-8")


def test_sets_cover_every_configured_category():
	"""cs alone would miss a paper that is only ever stat.ML."""
	assert sets_for_categories(CATEGORIES) == ["cs", "stat"]
	assert sets_for_categories(["cs.LG", "cs.AI"]) == ["cs"]
	assert sets_for_categories(["q-bio.NC"]) == []


def test_submission_date_comes_from_v1_not_the_latest_version():
	"""The defect this module exists to avoid.

	1912.08786 was submitted in December 2019 and revised six times, most
	recently days before the harvest. Its submission date must be v1's.
	"""
	papers, _ = parse_page(fixture("oai_page1.xml"))
	trap = next(p for p in papers if p.arxiv_id == "1912.08786")
	assert trap.submitted == datetime(2019, 12, 18, 18, 36, 20, tzinfo=UTC)
	assert trap.updated > trap.submitted
	assert trap.updated.year == 2026


def test_a_submission_keeps_its_time_of_day():
	"""Dates carry a timestamp, which is what keeps the window marker precise."""
	papers, _ = parse_page(fixture("oai_page1.xml"))
	fresh = next(p for p in papers if p.arxiv_id == "2609.05801")
	assert fresh.submitted == datetime(2026, 9, 5, 1, 28, 43, tzinfo=UTC)
	assert fresh.title and fresh.abstract
	assert fresh.primary_category == fresh.categories[0]
	assert fresh.pdf_url == "https://arxiv.org/pdf/2609.05801"


def test_a_page_reports_its_resumption_token():
	_, token = parse_page(fixture("oai_page1.xml"))
	assert token == "TOKEN-PAGE-2"


def test_an_empty_resumption_token_ends_the_harvest():
	_, token = parse_page(fixture("oai_page2.xml"))
	assert token is None


def test_a_deleted_record_is_skipped_rather_than_crashing():
	"""Deleted records carry a header and no metadata at all."""
	papers, _ = parse_page(fixture("oai_page2.xml"))
	assert all(p.arxiv_id != "2609.99999" for p in papers)


def test_no_records_matched_is_an_answer_not_a_failure():
	papers, token = parse_page(fixture("oai_empty.xml"))
	assert papers == []
	assert token is None


def test_an_oai_error_other_than_empty_is_raised():
	bad = (
		'<?xml version="1.0" encoding="UTF-8"?>'
		'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
		'<error code="badArgument">nope</error></OAI-PMH>'
	)
	with pytest.raises(RuntimeError, match="badArgument"):
		parse_page(bad)


def test_authors_split_from_the_single_string_arxivraw_gives():
	"""The Atom feed gives author elements; this format gives one string."""
	oai = "{http://www.openarchives.org/OAI/2.0/}"
	root = ET.fromstring(fixture("oai_page2.xml"))
	paper = parse_record(root.find(f"{oai}ListRecords/{oai}record"))
	assert paper is not None
	assert paper.authors == ["Ada Lovelace", "Alan Turing", "Grace Hopper"]


class _FakeTransport(httpx.BaseTransport):
	"""Serves the two fixture pages, then records what was asked for."""

	def __init__(self) -> None:
		self.requests: list[dict] = []

	def handle_request(self, request: httpx.Request) -> httpx.Response:
		params = dict(request.url.params)
		self.requests.append(params)
		page = "oai_page2.xml" if "resumptionToken" in params else "oai_page1.xml"
		return httpx.Response(200, text=fixture(page), request=request)


def _client() -> tuple[OaiClient, _FakeTransport]:
	transport = _FakeTransport()
	return (
		OaiClient(delay_seconds=0, client=httpx.Client(transport=transport)),
		transport,
	)


def test_harvest_keeps_only_papers_submitted_inside_the_window():
	"""The old paper is in the harvest and must not be in the window."""
	client, _ = _client()
	papers = list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
		)
	)
	ids = [p.arxiv_id for p in papers]
	assert "2609.05801" in ids
	assert "1912.08786" not in ids, "a 2019 paper cannot be in a September 2026 window"


def test_harvest_drops_papers_outside_the_configured_categories():
	client, _ = _client()
	papers = list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
		)
	)
	assert all(p.arxiv_id != "2609.05999" for p in papers), "cs.DL is not configured"


def test_a_resumption_token_replaces_every_other_argument():
	"""Sending the token alongside set/from/until is an error in the protocol."""
	client, transport = _client()
	list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
		)
	)
	resumed = [r for r in transport.requests if "resumptionToken" in r]
	assert resumed, "the token page was requested"
	for r in resumed:
		assert set(r) == {"verb", "resumptionToken"}


def test_both_sets_are_harvested():
	client, transport = _client()
	list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
		)
	)
	asked = {r.get("set") for r in transport.requests if "set" in r}
	assert asked == {"cs", "stat"}


def test_max_papers_stops_the_harvest():
	client, _ = _client()
	papers = list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
			max_papers=1,
		)
	)
	assert len(papers) == 1


def test_a_paper_in_two_sets_is_yielded_once():
	"""cs and stat are harvested separately and a cross-listed paper is in both."""
	client, _ = _client()
	papers = list(
		client.harvest(
			CATEGORIES,
			datetime(2026, 9, 1, tzinfo=UTC),
			datetime(2026, 9, 14, tzinfo=UTC),
		)
	)
	ids = [p.arxiv_id for p in papers]
	assert len(ids) == len(set(ids))


def test_retry_after_is_honoured_over_the_polite_delay():
	"""A 503 with Retry-After is OAI-PMH flow control, not a server in trouble."""
	client, _ = _client()
	request = httpx.Request("GET", "https://example.invalid")
	resp = httpx.Response(503, headers={"Retry-After": "42"}, request=request)
	exc = httpx.HTTPStatusError("flow control", request=request, response=resp)
	assert client._backoff(exc, 0) == 42.0
