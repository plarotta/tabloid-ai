from __future__ import annotations

import pytest

from pipeline.llm.base import LLMError, parse_json_response
from pipeline.prompts import load_prompt


def test_loads_latest_shortlist_prompt():
	p = load_prompt("shortlist")
	assert p.version >= 1
	assert "novelty" in p.system
	assert "{{papers}}" in p.user


def test_render_fills_placeholders():
	p = load_prompt("shortlist")
	system, user = p.render(n_papers=2, papers="paper blocks")
	assert "paper blocks" in user
	assert "{{" not in system + user


def test_render_rejects_missing_placeholder():
	p = load_prompt("shortlist")
	with pytest.raises(ValueError, match="unfilled placeholders"):
		p.render(n_papers=2)


def test_missing_stage_raises():
	with pytest.raises(FileNotFoundError):
		load_prompt("does-not-exist")


def test_json_braces_in_prompt_survive_render():
	"""The shortlist prompt contains a literal JSON example; rendering must not
	choke on its braces the way str.format would."""
	system, _ = load_prompt("shortlist").render(n_papers=1, papers="x")
	assert '"arxiv_id"' in system


@pytest.mark.parametrize(
	"raw",
	[
		'[{"a": 1}]',
		'```json\n[{"a": 1}]\n```',
		'Here is the output:\n```\n[{"a": 1}]\n```',
		'Sure! [{"a": 1}] Let me know if you need more.',
	],
)
def test_parse_json_tolerates_model_formatting(raw):
	assert parse_json_response(raw) == [{"a": 1}]


def test_parse_json_raises_on_garbage():
	with pytest.raises(LLMError):
		parse_json_response("there is no json here at all")
