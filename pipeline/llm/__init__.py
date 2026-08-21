"""LLM access layer.

Nothing in the pipeline calls a provider SDK directly. Everything goes through
`MeteredClient`, which is the single choke point where usage gets priced and
recorded. That is what makes the cost report trustworthy.
"""

from __future__ import annotations

import random
import re
import time

from ..config import ModelSpec
from .base import (
	LLMClient,
	LLMError,
	LLMResponse,
	MissingAPIKey,
	ProviderUnavailable,
	parse_json_response,
)
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
	"ProviderUnavailable",
	"build_client",
	"parse_json_response",
]


# Account-level failures, which every remaining call in the run will hit too.
# Matched on the provider's own words because the status code does not separate
# them: Anthropic returns 400 for an exhausted credit balance, the same code as
# a malformed request.
_ACCOUNT_FAILURE = re.compile(
	r"credit balance is too low|insufficient[ _]quota|billing|payment required"
	r"|invalid[ _]api[ _]key|authentication[ _]error|permission[ _]denied",
	re.IGNORECASE,
)


def is_account_failure(exc: Exception) -> bool:
	"""Whether an exception means the account cannot serve any request."""
	if getattr(exc, "status_code", None) in (401, 402, 403):
		return True
	return bool(_ACCOUNT_FAILURE.search(str(exc)))


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
				# Retrying this one is money and minutes spent to be told the same
				# thing three times, and every later call fails the same way.
				if is_account_failure(e):
					raise ProviderUnavailable(
						f"{self.client.provider} cannot serve requests in stage {self.stage!r}: {e}"
					) from e
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
