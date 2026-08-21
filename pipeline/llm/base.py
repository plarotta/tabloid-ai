"""Provider-agnostic LLM interface.

Adapters normalise three things that the providers disagree on: the shape of a
chat request, where the system prompt goes, and how token usage is reported.
Everything above this layer sees only `LLMClient.complete()` and `LLMResponse`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(RuntimeError):
	pass


class MissingAPIKey(LLMError):
	pass


class ProviderUnavailable(LLMError):
	"""The account cannot make calls at all - no credit, bad key, no permission.

	Distinguished from a plain `LLMError` because the difference decides what a
	stage should do about it. One failed call is worth retrying and often worth
	degrading around; an account that cannot serve *any* request will fail every
	remaining call in the run, and continuing past it spends money on the stages
	that do not need an API. On 2026-08-19 that cost a narration bill and a
	render for an episode with no title (DECISIONS.md, the 2026-08-19 run).
	"""


@dataclass(slots=True)
class LLMResponse:
	text: str
	input_tokens: int
	output_tokens: int
	model: str
	provider: str

	def json(self) -> object:
		"""Parse the response as JSON, tolerating the two things models do even
		when told not to: wrapping output in a markdown fence, and emitting prose
		around the payload."""
		return parse_json_response(self.text)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_response(text: str) -> object:
	text = text.strip()
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		pass

	m = _FENCE.search(text)
	if m:
		try:
			return json.loads(m.group(1).strip())
		except json.JSONDecodeError:
			pass

	# Last resort: slice from the first opening bracket to its matching close.
	for opener, closer in (("[", "]"), ("{", "}")):
		start = text.find(opener)
		end = text.rfind(closer)
		if start != -1 and end > start:
			try:
				return json.loads(text[start : end + 1])
			except json.JSONDecodeError:
				continue

	raise LLMError(f"Could not parse JSON from model output: {text[:400]!r}")


class LLMClient(ABC):
	"""One instance per (provider, model). Construction must not require network
	access, but must fail fast if the API key is absent."""

	provider: str

	def __init__(self, model: str, api_key: str | None = None) -> None:
		self.model = model
		self._api_key = api_key

	@abstractmethod
	def complete(
		self,
		prompt: str,
		system: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.0,
	) -> LLMResponse: ...
