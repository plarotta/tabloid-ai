"""Stage 7 TTS interface.

Interface only - no provider is wired in Phase 1 (see DECISIONS.md D3). It exists
now so Stage 6 can be written against a stable contract and so the engine choice
at Phase 4 is a single new file rather than a refactor.

The one non-obvious requirement: `synthesize` must return the *measured* duration
of the produced audio. Stage 8 builds the render timeline from real durations,
never from the script's `est_seconds`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SpeechResult:
	audio_path: Path
	duration_seconds: float
	characters: int
	provider: str
	model: str


class TTSClient(ABC):
	provider: str

	@abstractmethod
	def synthesize(self, text: str, out_path: Path, voice: str | None = None) -> SpeechResult:
		"""Render `text` to an audio file at `out_path` and measure its duration."""

	def synthesize_with_context(
		self,
		text: str,
		out_path: Path,
		voice: str | None = None,
		*,
		previous_text: str = "",
		next_text: str = "",
	) -> SpeechResult:
		"""Providers may use adjacent sentences for continuity; the default is unchanged."""
		return self.synthesize(text, out_path, voice)


def build_tts_client(provider: str | None, model: str | None = None) -> TTSClient:
	raise NotImplementedError(
		"No TTS provider is wired yet. Stage 7 is deferred to Phase 4 (DECISIONS.md D3). "
		"Add an adapter here and set `tts.provider` in config.yaml."
	)
