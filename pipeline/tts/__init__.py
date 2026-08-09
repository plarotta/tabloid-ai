"""Stage 7 TTS: provider-agnostic interface plus adapters."""

from .base import SpeechResult, TTSClient
from .providers import PROVIDERS, TTSError, build_tts_client, measure_duration

__all__ = [
	"PROVIDERS",
	"SpeechResult",
	"TTSClient",
	"TTSError",
	"build_tts_client",
	"measure_duration",
]
