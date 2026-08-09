"""Parsing is tested against a real captured API response, not a hand-written one."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from pipeline.arxiv import ArxivClient, build_search_query, parse_feed
from tests.conftest import FIXTURES


@pytest.fixture
def feed_xml() -> str:
	return (FIXTURES / "arxiv_page.xml").read_text(encoding="utf-8")


def test_build_search_query_shape():
	q = build_search_query(
		["cs.LG", "cs.CL"], datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)
	)
	assert q == "(cat:cs.LG OR cat:cs.CL) AND submittedDate:[202608030000 TO 202608070000]"


def test_parse_real_feed(feed_xml: str):
	papers, total = parse_feed(feed_xml)
	assert total == 895
	assert len(papers) == 2

	p = papers[0]
	assert p.arxiv_id == "2608.06377"  # version suffix stripped
	assert p.title.startswith("Learning When to Trust")
	assert "cs.CL" in p.categories
	assert p.primary_category == "cs.CL"
	assert len(p.authors) == 7
	assert p.submitted.year == 2026


def test_title_and_abstract_whitespace_collapsed(feed_xml: str):
	papers, _ = parse_feed(feed_xml)
	for p in papers:
		assert "\n" not in p.title
		assert "  " not in p.abstract


def test_comment_field_captured_for_github_signals(feed_xml: str):
	papers, _ = parse_feed(feed_xml)
	# Stage 3 mines this field for repo links.
	assert any(p.comment and "github" in p.comment.lower() for p in papers)


def test_empty_feed_yields_nothing():
	xml = (
		'<?xml version="1.0" encoding="UTF-8"?>'
		'<feed xmlns="http://www.w3.org/2005/Atom"'
		' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
		"<opensearch:totalResults>0</opensearch:totalResults>"
		"</feed>"
	)
	papers, total = parse_feed(xml)
	assert papers == []
	assert total == 0


def test_entry_missing_required_fields_is_skipped():
	xml = (
		'<?xml version="1.0" encoding="UTF-8"?>'
		'<feed xmlns="http://www.w3.org/2005/Atom"'
		' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
		"<opensearch:totalResults>1</opensearch:totalResults>"
		"<entry><id>http://arxiv.org/abs/1234.5678v1</id>"
		"<title>No abstract here</title></entry>"
		"</feed>"
	)
	papers, _ = parse_feed(xml)
	assert papers == []


def test_search_dedupes_cross_listed_papers(feed_xml: str):
	"""The same paper appearing twice across pages must be yielded once."""
	calls = {"n": 0}

	def handler(request: httpx.Request) -> httpx.Response:
		calls["n"] += 1
		# Serve the same two papers on every page.
		return httpx.Response(200, text=feed_xml)

	transport = httpx.MockTransport(handler)
	client = ArxivClient(page_size=2, delay_seconds=0.0, client=httpx.Client(transport=transport))
	papers = list(
		client.search(
			["cs.LG"],
			datetime(2026, 8, 3, tzinfo=UTC),
			datetime(2026, 8, 7, tzinfo=UTC),
			max_papers=50,
		)
	)
	assert len({p.arxiv_id for p in papers}) == len(papers) == 2


def test_search_respects_max_papers(feed_xml: str):
	def handler(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, text=feed_xml)

	client = ArxivClient(
		page_size=2, delay_seconds=0.0, client=httpx.Client(transport=httpx.MockTransport(handler))
	)
	papers = list(
		client.search(
			["cs.LG"],
			datetime(2026, 8, 3, tzinfo=UTC),
			datetime(2026, 8, 7, tzinfo=UTC),
			max_papers=1,
		)
	)
	assert len(papers) == 1
