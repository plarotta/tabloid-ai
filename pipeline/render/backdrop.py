"""Generated backdrops for title cards.

A title card holds for four seconds carrying one line of type, and it was the
emptiest frame in the episode. This puts an image behind that type.

Three constraints shape everything here, and the first is editorial rather than
technical:

  - **It must never look like evidence.** This is a channel about papers, and a
    generated picture that reads as a figure, a chart or a photograph would be
    taken for one. The prompt asks for abstract texture and bans every
    representational form; the composite then pushes it far enough back that it
    reads as a ground, not as content.
  - **It must never cost a re-render.** Stage 8 is otherwise free and gets run
    repeatedly while tuning. Backdrops are cached on disk by the hash of the
    prompt that made them, so the second render of a run pays nothing.
  - **It must never break a render.** No key, no package, a refusal, a timeout,
    a malformed response: all of them return None and the flat card is drawn.

See DECISIONS.md D39.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Bans first, because an image model will reach for a chart the moment the
# subject sounds scientific, and a chart on this channel is a claim.
# The opener's visual family (D51): paper sheets turning into filaments of
# light in the series palette, so the wrapper's cards read as part of the same
# title sequence. Still abstract, still banned from looking like evidence (D39).
STYLE = (
	"Cinematic abstract 3D artwork in the style of a premium science-documentary "
	"title sequence. A few thin white paper sheets drift through deep near-black "
	"navy space (#161A21) and dissolve into glowing filaments of light in electric "
	"blue (#7AA2F7) with touches of mint (#58D4AC), rose (#F77E9C) and violet "
	"(#AA92FF). Volumetric light, shallow depth of field, subtle film grain. "
	"Absolutely no text, letters, numbers, charts, graphs, plots, diagrams, "
	"arrows, UI, screens, robots, brains, people, hands or logos. "
	"All the light and detail sits in the right third; the left two thirds stay "
	"dark, calm negative space for white type."
)


def prompt_for(subject: str) -> str:
	"""The image prompt for a title card, built from the card's own words.

	Deterministic: no model is asked to write it. A second LLM call would add
	cost, latency and one more thing to go wrong, for a prompt whose useful part
	is the fixed style block anyway.
	"""
	subject = " ".join((subject or "").split())[:180]
	mood = f"Loosely evoking, without depicting: {subject}. " if subject else ""
	return f"{mood}{STYLE}"


def available() -> bool:
	"""Whether a backdrop could be generated at all."""
	if not os.environ.get("OPENAI_API_KEY"):
		return False
	try:
		import importlib.util

		return importlib.util.find_spec("openai") is not None
	except (ImportError, ValueError):
		return False


def generate(
	subject: str,
	cache_dir: Path,
	tracker=None,
	stage: str = "render",
	model: str = "gpt-image-2",
	size: str = "1536x1024",
	quality: str = "medium",
	timeout: int = 120,
) -> Path | None:
	"""Return a backdrop for `subject`, or None to draw the flat card instead.

	Cached by prompt hash, so re-rendering a run is free and two title cards that
	somehow ask for the same thing pay once.
	"""
	return generate_image(
		prompt_for(subject),
		cache_dir,
		tracker=tracker,
		stage=stage,
		model=model,
		size=size,
		quality=quality,
		timeout=timeout,
		label=subject,
	)


def generate_image(
	prompt: str,
	cache_dir: Path,
	tracker=None,
	stage: str = "render",
	model: str = "gpt-image-2",
	size: str = "1536x1024",
	quality: str = "medium",
	timeout: int = 120,
	label: str = "",
) -> Path | None:
	"""One generated image for `prompt`, cached by hash; None on any failure.

	Shared by the title-card backdrops (D39) and the thumbnail art (D52).
	"""
	if not available():
		return None

	key = hashlib.sha256(f"{model}|{size}|{quality}|{prompt}".encode()).hexdigest()[:16]
	cache_dir.mkdir(parents=True, exist_ok=True)
	cached = cache_dir / f"{key}.png"
	if cached.exists() and cached.stat().st_size > 0:
		log.debug("Image cache hit for %r", (label or prompt)[:40])
		return cached

	try:
		from openai import OpenAI

		client = OpenAI(timeout=timeout)
		resp = client.images.generate(model=model, prompt=prompt, size=size, quality=quality, n=1)
		payload = resp.data[0].b64_json if resp.data else None
		if not payload:
			log.warning("Image model returned no data for %r", (label or prompt)[:40])
			return None
		cached.write_bytes(base64.b64decode(payload))
	except Exception as e:
		if "does not exist" in str(e):
			# Not a transient failure and not the key: OpenAI retires image models.
			# This is how gpt-image-1 failed silently for every episode (D51).
			log.error(
				"Image model %r no longer exists for this key; set render.backdrop_model "
				"to a current one (GET /v1/models lists them)",
				model,
			)
			return None
		# Includes refusals, rate limits, timeouts and SDK changes. Every caller
		# has a fallback that shipped for episodes before images existed.
		log.warning("Image generation failed (%s)", e)
		return None

	if tracker is not None:
		tracker.record(
			stage=stage,
			provider="openai",
			model=model,
			input_units=1,  # priced per image, not per token
			output_units=0,
			kind="image",
		)
	log.info("Image generated for %r", (label or prompt)[:48])
	return cached
