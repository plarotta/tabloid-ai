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
STYLE = (
	"Abstract non-representational texture for a video title card background. "
	"Very dark near-black ground (#161A21) with restrained cool blue accents. "
	"Soft geometric forms, fine grain, deep negative space, out-of-focus depth. "
	"Editorial and quiet, not futuristic, not neon, not a tech stock image. "
	"Absolutely no text, letters, numbers, charts, graphs, plots, diagrams, "
	"arrows, UI, screens, robots, brains, people, hands or logos. "
	"The left two thirds must stay almost empty and very dark so white type can "
	"sit on it."
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
	model: str = "gpt-image-1",
	size: str = "1536x1024",
	quality: str = "medium",
	timeout: int = 120,
) -> Path | None:
	"""Return a backdrop for `subject`, or None to draw the flat card instead.

	Cached by prompt hash, so re-rendering a run is free and two title cards that
	somehow ask for the same thing pay once.
	"""
	if not available():
		return None

	prompt = prompt_for(subject)
	key = hashlib.sha256(f"{model}|{size}|{quality}|{prompt}".encode()).hexdigest()[:16]
	cache_dir.mkdir(parents=True, exist_ok=True)
	cached = cache_dir / f"{key}.png"
	if cached.exists() and cached.stat().st_size > 0:
		log.debug("Backdrop cache hit for %r", subject[:40])
		return cached

	try:
		from openai import OpenAI

		client = OpenAI(timeout=timeout)
		resp = client.images.generate(
			model=model, prompt=prompt, size=size, quality=quality, n=1
		)
		payload = resp.data[0].b64_json if resp.data else None
		if not payload:
			log.warning("Image model returned no data; the title card stays flat")
			return None
		cached.write_bytes(base64.b64decode(payload))
	except Exception as e:
		# Includes refusals, rate limits, timeouts and SDK changes. A title card
		# without a backdrop is the slide this pipeline shipped for four episodes.
		log.warning("Backdrop generation failed (%s); the title card stays flat", e)
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
	log.info("Backdrop generated for %r", subject[:48])
	return cached
