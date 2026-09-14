"""TTS adapters.

Two engines, chosen by what this machine can actually do:

  macos   `say` + `afinfo`, both built into macOS. Free, offline, no key. This is
          the working default because **no OpenAI key is configured**, which is
          what the spec's `tts-1` default requires. Quality is below a hosted
          neural voice, but it produces real audio with a real measured duration,
          which is what Stage 8 needs to build a timeline.
  openai  the spec's default. Wired and ready; raises a clear error until a key
          exists, rather than being absent and forcing a refactor later.

Every adapter measures the duration of what it produced rather than estimating
it. That is the one hard requirement of this interface (see base.py): Stage 8
syncs scene boundaries to real audio, never to the script's `est_seconds`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from .base import SpeechResult, TTSClient

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
	pass


# `<break time="0.8s" />` and friends. ElevenLabs interprets these; `say` and the
# OpenAI voices read them out character by character.
_SSML = re.compile(r"<\s*break\b[^>]*/?\s*>", re.IGNORECASE)


def strip_ssml(text: str) -> str:
	"""Remove speech markup an engine would otherwise pronounce.

	Cheap insurance rather than a live concern: no prompt emits break tags today.
	But the failure mode if one ever does is a published episode in which the
	narrator says "break time zero point eight s" out loud, which is worth five
	lines to make impossible.
	"""
	return " ".join(_SSML.sub(" ", text).split())


def measure_duration(path: Path) -> float:
	"""Measured length of an audio file, in seconds.

	Prefers `ffprobe`, falling back to `afinfo` (macOS, always present alongside
	`say`). Raises rather than guessing: a wrong duration silently desynchronises
	the whole episode timeline, which is worse than a loud failure here.

	**The order matters and used to be the other way round.** `afinfo` reports an
	*estimated* duration, and for MP3 it runs about 0.25% long. Per clip that is
	30ms and invisible; across the 29 clips of the 2026-08-12 episode it summed to
	1.09s of timeline that did not exist in the files, which is what pushed the
	late chapter marks past their real positions. `ffprobe` reads the container.
	"""
	if shutil.which("ffprobe"):
		try:
			out = subprocess.run(
				[
					"ffprobe",
					"-v",
					"error",
					"-show_entries",
					"format=duration",
					"-of",
					"default=noprint_wrappers=1:nokey=1",
					str(path),
				],
				capture_output=True,
				text=True,
				timeout=30,
				check=True,
			).stdout.strip()
			if out:
				return float(out)
		except (subprocess.SubprocessError, OSError, ValueError) as e:
			log.debug("ffprobe failed on %s: %s", path.name, e)

	if shutil.which("afinfo"):
		try:
			out = subprocess.run(
				["afinfo", str(path)], capture_output=True, text=True, timeout=30, check=True
			).stdout
			m = re.search(r"estimated duration:\s*([\d.]+)", out)
			if m:
				log.debug("Measured %s with afinfo; expect ~0.25%% of over-report", path.name)
				return float(m.group(1))
		except (subprocess.SubprocessError, OSError) as e:
			log.debug("afinfo failed on %s: %s", path.name, e)

	raise TTSError(
		f"Could not measure the duration of {path}. Install ffmpeg (for ffprobe) "
		f"or run on macOS where afinfo is available - Stage 8 cannot build a "
		f"timeline without real durations."
	)


class MacSayTTS(TTSClient):
	"""macOS `say`. Free, offline, no API key, no per-character cost."""

	provider = "macos"
	# `say` writes AIFF natively; ffmpeg reads it without conversion.
	suffix = ".aiff"

	def __init__(self, model: str = "say", voice: str = "Samantha", rate: int | None = 165):
		self.model = model
		self.default_voice = voice
		self.rate = rate

	def synthesize(
		self,
		text: str,
		out_path: Path,
		voice: str | None = None,
		settings: dict | None = None,
	) -> SpeechResult:
		if not shutil.which("say"):
			raise TTSError("`say` not found. This provider only works on macOS.")
		out_path = out_path.with_suffix(self.suffix)
		out_path.parent.mkdir(parents=True, exist_ok=True)

		cmd = ["say", "-v", voice or self.default_voice, "-o", str(out_path)]
		if self.rate:
			cmd += ["-r", str(self.rate)]
		cmd += ["--", strip_ssml(text)]
		try:
			subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
		except subprocess.CalledProcessError as e:
			raise TTSError(f"say failed: {e.stderr.strip() or e}") from e
		except (subprocess.SubprocessError, OSError) as e:
			raise TTSError(f"say failed: {e}") from e

		if not out_path.exists() or out_path.stat().st_size == 0:
			raise TTSError(f"say produced no audio at {out_path}")

		return SpeechResult(
			audio_path=out_path,
			duration_seconds=measure_duration(out_path),
			characters=len(text),
			provider=self.provider,
			model=self.model,
		)


class OpenAITTS(TTSClient):
	"""The spec's default engine. Billed per character; priced in pricing.yaml.

	`gpt-4o-mini-tts` is the default rather than `tts-1`: it is the newer and more
	natural-sounding of the two, which is the whole reason for leaving the local
	engine.
	"""

	provider = "openai"
	suffix = ".mp3"

	def __init__(self, model: str = "gpt-4o-mini-tts", voice: str = "onyx"):
		self.model = model
		self.default_voice = voice

	def synthesize(
		self,
		text: str,
		out_path: Path,
		voice: str | None = None,
		settings: dict | None = None,
	) -> SpeechResult:
		key = os.environ.get("OPENAI_API_KEY")
		if not key:
			raise TTSError(
				"openai requires OPENAI_API_KEY. It is unset or empty. Either add a "
				"key to .env or set tts.provider to `macos` in config.yaml."
			)
		try:
			from openai import OpenAI
		except ImportError as e:
			raise TTSError("openai TTS needs `uv pip install openai`") from e

		out_path = out_path.with_suffix(self.suffix)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		client = OpenAI(api_key=key)
		with client.audio.speech.with_streaming_response.create(
			model=self.model, voice=voice or self.default_voice, input=strip_ssml(text)
		) as resp:
			resp.stream_to_file(out_path)

		return SpeechResult(
			audio_path=out_path,
			duration_seconds=measure_duration(out_path),
			characters=len(text),
			provider=self.provider,
			model=self.model,
		)


class ElevenLabsTTS(TTSClient):
	"""The alternative the spec names explicitly. Highest quality, highest cost.

	Two things the first pass left on the table, both of which the owner review
	heard as "robotic":

	**`voice_settings` were never sent.** Every call ran at whatever defaults the
	voice carried. `stability` is the one that matters: high values make a voice
	consistent and flat, which is what a five-minute read at one pitch sounds
	like. Lower it and the delivery varies.

	**`eleven_turbo_v2_5` is the latency-optimised model**, not the quality one.
	Nothing here needs low latency - the run is batch.

	`<break time="0.4s" />` in the text is honoured, but only usefully above about
	half a second: a shorter break is absorbed by the pause the punctuation
	already produces. Measured on this account, not assumed.
	"""

	provider = "elevenlabs"
	suffix = ".mp3"

	# "Rachel" - a neutral narration voice from the default library.
	def __init__(
		self,
		model: str = "eleven_multilingual_v2",
		voice: str = "21m00Tcm4TlvDq8ikWAM",
		settings: dict | None = None,
	):
		self.model = model
		self.default_voice = voice
		self.settings = settings or {}

	def synthesize(
		self,
		text: str,
		out_path: Path,
		voice: str | None = None,
		settings: dict | None = None,
	) -> SpeechResult:
		key = os.environ.get("ELEVENLABS_API_KEY")
		if not key:
			raise TTSError(
				"elevenlabs requires ELEVENLABS_API_KEY. It is unset or empty. Either "
				"add a key to .env or set tts.provider to `macos` in config.yaml."
			)
		import httpx

		out_path = out_path.with_suffix(self.suffix)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		vid = voice or self.default_voice
		body: dict = {"text": text, "model_id": self.model}
		# Per-call settings override the client's, so Stage 7 can read a headline
		# number and a caveat differently without building a second client (D42).
		per_call = settings or self.settings
		if per_call:
			body["voice_settings"] = per_call
		resp = httpx.post(
			f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
			headers={"xi-api-key": key, "accept": "audio/mpeg"},
			json=body,
			timeout=180.0,
		)
		if resp.status_code != 200:
			raise TTSError(f"elevenlabs returned {resp.status_code}: {resp.text[:200]}")
		out_path.write_bytes(resp.content)

		return SpeechResult(
			audio_path=out_path,
			duration_seconds=measure_duration(out_path),
			characters=len(text),
			provider=self.provider,
			model=self.model,
		)


PROVIDERS = {"macos": MacSayTTS, "openai": OpenAITTS, "elevenlabs": ElevenLabsTTS}


def build_tts_client(
	provider: str | None,
	model: str | None = None,
	voice: str | None = None,
	settings: dict | None = None,
):
	if not provider:
		raise TTSError(
			f"tts.provider is not set in config.yaml. Available: {', '.join(sorted(PROVIDERS))}."
		)
	cls = PROVIDERS.get(provider)
	if cls is None:
		raise TTSError(
			f"Unknown TTS provider {provider!r}. Available: {', '.join(sorted(PROVIDERS))}."
		)
	kwargs = {}
	if model:
		kwargs["model"] = model
	if voice:
		kwargs["voice"] = voice
	# Only ElevenLabs takes per-voice settings; the others would reject the kwarg.
	if settings and cls is ElevenLabsTTS:
		kwargs["settings"] = settings
	return cls(**kwargs)
