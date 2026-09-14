"""Cost accounting.

Every billed call in the pipeline is recorded here. Two properties matter:

1. Append-only. Each call is flushed to `calls.jsonl` as it happens, so a run
   that crashes halfway still leaves an accurate record of what it spent.
2. Loud about gaps. A model missing from pricing.yaml is priced at zero but
   flagged `unpriced`, and the flag is surfaced in the report. A cost report
   that silently undercounts is worse than no cost report.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..paths import REPO_ROOT, write_json
from ..schemas import CallRecord, CostReport, StageCost


class BudgetExceeded(RuntimeError):
	"""Raised when cumulative spend crosses the configured ceiling."""


class Pricing:
	"""Lookup table from pricing.yaml.

	Units are USD per 1M tokens (text), per 1M characters (audio), or per image
	(image). Images are the odd one out because that is how they are actually
	billed - a flat rate per picture at a given size and quality - and pretending
	otherwise would put a made-up token count in the ledger."""

	def __init__(self, table: dict) -> None:
		self._text = table.get("text", {}) or {}
		self._audio = table.get("audio", {}) or {}
		self._image = table.get("image", {}) or {}
		self.verified_on = table.get("verified_on")

	@classmethod
	def load(cls, path: Path | None = None) -> Pricing:
		path = path or REPO_ROOT / "pricing.yaml"
		if not path.exists():
			raise FileNotFoundError(f"pricing.yaml not found at {path}")
		return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

	def rate(self, kind: str, provider: str, model: str) -> dict[str, float] | None:
		table = {"text": self._text, "audio": self._audio, "image": self._image}.get(
			kind, self._text
		)
		return (table.get(provider) or {}).get(model)

	def price(
		self, kind: str, provider: str, model: str, input_units: int, output_units: int
	) -> tuple[float, bool]:
		"""Return (cost_usd, unpriced). Unpriced calls cost 0.0 and are flagged."""
		rate = self.rate(kind, provider, model)
		if rate is None:
			return 0.0, True
		if kind == "image":
			# `input_units` is a count of pictures, not of tokens.
			return input_units * float(rate.get("per_image", 0.0)), False
		cost = (input_units / 1_000_000) * float(rate.get("input", 0.0))
		cost += (output_units / 1_000_000) * float(rate.get("output", 0.0))
		return cost, False


class CostTracker:
	"""Thread-safe recorder. Shortlist runs batches concurrently, so record()
	must tolerate parallel callers."""

	def __init__(
		self,
		run_id: str,
		calls_path: Path,
		pricing: Pricing | None = None,
		ceiling_usd: float = 20.0,
		stage_targets: dict[str, float] | None = None,
	) -> None:
		self.run_id = run_id
		self.calls_path = calls_path
		self.pricing = pricing or Pricing.load()
		self.ceiling_usd = ceiling_usd
		self.stage_targets = stage_targets or {}
		self._lock = threading.Lock()
		# Seed from the append-only log so the tracker represents the whole *run*,
		# not just this process. Without this, resuming with `--from <stage>`
		# rebuilds cost_report.json from only the stages that ran this time and
		# silently discards everything spent earlier - and the ceiling is checked
		# against a fraction of true spend.
		self.records: list[CallRecord] = self._load_existing()

	def _load_existing(self) -> list[CallRecord]:
		if not self.calls_path.exists():
			return []
		records = []
		for line in self.calls_path.read_text(encoding="utf-8").splitlines():
			line = line.strip()
			if not line:
				continue
			try:
				records.append(CallRecord.model_validate_json(line))
			except Exception:
				# A partially written final line from a killed run is not worth
				# failing over; the rest of the trail is still accurate.
				continue
		return records

	def record(
		self,
		stage: str,
		provider: str,
		model: str,
		input_units: int,
		output_units: int,
		kind: str = "text",
	) -> CallRecord:
		cost, unpriced = self.pricing.price(kind, provider, model, input_units, output_units)
		rec = CallRecord(
			timestamp=datetime.now(UTC),
			stage=stage,
			provider=provider,
			model=model,
			kind=kind,
			input_units=input_units,
			output_units=output_units,
			cost_usd=cost,
			unpriced=unpriced,
		)
		with self._lock:
			self.records.append(rec)
			with self.calls_path.open("a", encoding="utf-8") as fh:
				fh.write(rec.model_dump_json() + "\n")
			total = sum(r.cost_usd for r in self.records)
		if total > self.ceiling_usd:
			raise BudgetExceeded(
				f"Run {self.run_id} spent ${total:.2f}, over the ${self.ceiling_usd:.2f} "
				f"ceiling. Raise budget.ceiling_usd in config.yaml or investigate "
				f"stage {stage!r}."
			)
		return rec

	@property
	def total_usd(self) -> float:
		with self._lock:
			return sum(r.cost_usd for r in self.records)

	def stage_total(self, stage: str) -> float:
		with self._lock:
			return sum(r.cost_usd for r in self.records if r.stage == stage)

	def build_report(self) -> CostReport:
		with self._lock:
			records = list(self.records)
		stages: dict[str, StageCost] = {}
		for r in records:
			s = stages.get(r.stage)
			if s is None:
				target = self.stage_targets.get(r.stage)
				s = StageCost(
					stage=r.stage,
					calls=0,
					input_units=0,
					output_units=0,
					cost_usd=0.0,
					target_usd=target,
				)
				stages[r.stage] = s
			s.calls += 1
			s.input_units += r.input_units
			s.output_units += r.output_units
			s.cost_usd += r.cost_usd
		for s in stages.values():
			s.cost_usd = round(s.cost_usd, 6)
			s.over_target = s.target_usd is not None and s.cost_usd > s.target_usd

		total = round(sum(r.cost_usd for r in records), 6)
		return CostReport(
			run_id=self.run_id,
			generated_at=datetime.now(UTC),
			total_usd=total,
			ceiling_usd=self.ceiling_usd,
			over_ceiling=total > self.ceiling_usd,
			unpriced_calls=sum(1 for r in records if r.unpriced),
			by_stage=[stages[k] for k in sorted(stages)],
		)

	def write_report(self, path: Path) -> CostReport:
		report = self.build_report()
		write_json(path, json.loads(report.model_dump_json()))
		return report
