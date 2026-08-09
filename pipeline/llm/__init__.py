"""LLM access layer.

Nothing in the pipeline calls a provider SDK directly. Everything goes through
`MeteredClient`, which is the single choke point where usage gets priced and
recorded. That is what makes the cost report trustworthy.
"""

from __future__ import annotations

import random
import time

from ..config import ModelSpec
from .base import LLMClient, LLMError, LLMResponse, MissingAPIKey, parse_json_response
from .cost import BudgetExceeded, CostTracker, Pricing
from .providers import PROVIDERS, build_client

__all__ = [
	"PROVIDERS",
	"BudgetExceeded",
	"CostTracker",
	"LLMClient",
	"LLMError",
	"LLMResponse",
	"MeteredClient",
	"MissingAPIKey",
	"Pricing",
	"build_client",
	"parse_json_response",
]


class MeteredClient:
	"""Wraps an LLMClient so every call is priced and logged against a stage."""

	def __init__(
		self,
		client: LLMClient,
		tracker: CostTracker,
		stage: str,
		max_retries: int = 3,
	) -> None:
		self.client = client
		self.tracker = tracker
		self.stage = stage
		self.max_retries = max_retries

	@classmethod
	def for_stage(
		cls,
		spec: ModelSpec,
		tracker: CostTracker,
		stage: str,
		max_retries: int = 3,
	) -> MeteredClient:
		return cls(build_client(spec.provider, spec.model), tracker, stage, max_retries)

	def complete(
		self,
		prompt: str,
		system: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.0,
	) -> LLMResponse:
		last: Exception | None = None
		for attempt in range(self.max_retries):
			try:
				resp = self.client.complete(
					prompt, system=system, max_tokens=max_tokens, temperature=temperature
				)
			except BudgetExceeded:
				raise
			except Exception as e:
				last = e
				if attempt == self.max_retries - 1:
					break
				# Jittered backoff; provider rate limits are the common case here.
				time.sleep((2**attempt) + random.random())
				continue

			# Only record usage for calls that actually returned. A failed call
			# still costs nothing on most providers, and counting it would make
			# the report overstate spend.
			self.tracker.record(
				stage=self.stage,
				provider=resp.provider,
				model=resp.model,
				input_units=resp.input_tokens,
				output_units=resp.output_tokens,
			)
			return resp

		raise LLMError(
			f"{self.client.provider}/{self.client.model} failed after "
			f"{self.max_retries} attempts in stage {self.stage!r}: {last}"
		) from last
