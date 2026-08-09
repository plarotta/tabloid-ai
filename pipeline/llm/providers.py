"""Concrete provider adapters.

SDKs are imported lazily inside each adapter so the pipeline runs with only the
providers you actually installed. `pip install -e '.[anthropic]'` is enough for
the default config.
"""

from __future__ import annotations

import os

from .base import LLMClient, LLMError, LLMResponse, MissingAPIKey


def _require_key(env_var: str, provider: str, explicit: str | None) -> str:
	key = explicit or os.environ.get(env_var, "")
	# An empty-but-set env var is a common footgun and would otherwise surface
	# as a confusing 401 several seconds later.
	if not key.strip():
		raise MissingAPIKey(
			f"{provider} requires {env_var}. It is unset or empty. "
			f"Copy .env.example to .env and fill it in."
		)
	return key


class AnthropicClient(LLMClient):
	provider = "anthropic"

	def __init__(self, model: str, api_key: str | None = None) -> None:
		super().__init__(model, api_key)
		try:
			from anthropic import Anthropic
		except ImportError as e:
			raise LLMError("anthropic SDK not installed. `pip install -e '.[anthropic]'`") from e
		self._client = Anthropic(api_key=_require_key("ANTHROPIC_API_KEY", "anthropic", api_key))

	def complete(
		self,
		prompt: str,
		system: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.0,
	) -> LLMResponse:
		kwargs = {
			"model": self.model,
			"max_tokens": max_tokens,
			"temperature": temperature,
			"messages": [{"role": "user", "content": prompt}],
		}
		if system:
			kwargs["system"] = system
		resp = self._client.messages.create(**kwargs)
		text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
		return LLMResponse(
			text=text,
			input_tokens=resp.usage.input_tokens,
			output_tokens=resp.usage.output_tokens,
			model=self.model,
			provider=self.provider,
		)


class OpenAIClient(LLMClient):
	provider = "openai"

	def __init__(self, model: str, api_key: str | None = None) -> None:
		super().__init__(model, api_key)
		try:
			from openai import OpenAI
		except ImportError as e:
			raise LLMError("openai SDK not installed. `pip install -e '.[openai]'`") from e
		self._client = OpenAI(api_key=_require_key("OPENAI_API_KEY", "openai", api_key))

	def complete(
		self,
		prompt: str,
		system: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.0,
	) -> LLMResponse:
		messages = []
		if system:
			messages.append({"role": "system", "content": system})
		messages.append({"role": "user", "content": prompt})
		resp = self._client.chat.completions.create(
			model=self.model,
			messages=messages,
			max_tokens=max_tokens,
			temperature=temperature,
		)
		usage = resp.usage
		return LLMResponse(
			text=resp.choices[0].message.content or "",
			input_tokens=usage.prompt_tokens if usage else 0,
			output_tokens=usage.completion_tokens if usage else 0,
			model=self.model,
			provider=self.provider,
		)


class GroqClient(LLMClient):
	provider = "groq"

	def __init__(self, model: str, api_key: str | None = None) -> None:
		super().__init__(model, api_key)
		try:
			from groq import Groq
		except ImportError as e:
			raise LLMError("groq SDK not installed. `pip install -e '.[groq]'`") from e
		self._client = Groq(api_key=_require_key("GROQ_API_KEY", "groq", api_key))

	def complete(
		self,
		prompt: str,
		system: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.0,
	) -> LLMResponse:
		messages = []
		if system:
			messages.append({"role": "system", "content": system})
		messages.append({"role": "user", "content": prompt})
		resp = self._client.chat.completions.create(
			model=self.model,
			messages=messages,
			max_tokens=max_tokens,
			temperature=temperature,
		)
		usage = resp.usage
		return LLMResponse(
			text=resp.choices[0].message.content or "",
			input_tokens=usage.prompt_tokens if usage else 0,
			output_tokens=usage.completion_tokens if usage else 0,
			model=self.model,
			provider=self.provider,
		)


PROVIDERS: dict[str, type[LLMClient]] = {
	"anthropic": AnthropicClient,
	"openai": OpenAIClient,
	"groq": GroqClient,
}


def build_client(provider: str, model: str, api_key: str | None = None) -> LLMClient:
	try:
		cls = PROVIDERS[provider]
	except KeyError:
		raise LLMError(f"Unknown provider {provider!r}. Known: {sorted(PROVIDERS)}") from None
	return cls(model, api_key)
