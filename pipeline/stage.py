"""Shared stage contract.

A stage reads artifacts written by earlier stages, does its work, and writes its
own artifacts. `is_complete()` lets the runner skip stages that already have
valid output, which is what makes `--from` cheap.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .config import Config
from .llm.cost import CostTracker
from .paths import RunPaths


class StageError(RuntimeError):
	"""A stage failed in a way that should stop the run.

	The spec requires loud failure over silent partial output, so stages raise
	this rather than returning degraded results.
	"""


@dataclass
class StageContext:
	config: Config
	paths: RunPaths
	tracker: CostTracker
	# Set by --paper to restrict per-paper stages to a single arXiv ID.
	paper_filter: str | None = None


class Stage(ABC):
	name: str

	def __init__(self, ctx: StageContext) -> None:
		self.ctx = ctx
		self.config = ctx.config
		self.paths = ctx.paths
		self.tracker = ctx.tracker

	@abstractmethod
	def run(self) -> Any:
		"""Do the work and write artifacts. Return value is for the caller's
		convenience only - the artifact on disk is the real output."""

	@abstractmethod
	def is_complete(self) -> bool:
		"""True if this stage's artifacts already exist and parse."""

	@abstractmethod
	def load(self) -> Any:
		"""Read this stage's artifacts back from disk."""

	def ensure(self) -> Any:
		"""Load cached output if present, otherwise run."""
		if self.is_complete():
			return self.load()
		return self.run()
