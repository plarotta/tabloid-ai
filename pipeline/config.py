"""Loading and validation of config.yaml.

Config is validated up front so a typo in a model name fails before the run
spends money, not three stages in.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .paths import REPO_ROOT


class StrictModel(BaseModel):
	model_config = ConfigDict(extra="forbid")


class ModelSpec(StrictModel):
	provider: str
	model: str
	max_tokens: int = 4096
	temperature: float = 0.0


class FetchConfig(StrictModel):
	categories: list[str]
	fallback_window_days: int = 4
	page_size: int = 200
	request_delay_seconds: float = 3.0
	max_retries: int = 5
	max_papers: int = 3000


class ShortlistConfig(StrictModel):
	stage_model: str = "shortlist"
	batch_size: int = 20
	shortlist_size: int = 15
	max_concurrency: int = 4
	min_score: float = 5.0


class EnrichConfig(StrictModel):
	# arXiv is a free service; this stage makes one e-print request per paper.
	request_delay_seconds: float = 3.0
	max_concurrency: int = 4
	max_figures: int = 12
	# Below this many successfully enriched papers, ranking has too thin a field
	# to pick a credible top 3 and the run stops.
	min_enriched: int = 5
	# Keep the unpacked LaTeX under _work/ for debugging. Off by default: the
	# sample tarballs ran 2-9 MB each, which is 100 MB+ per run across a shortlist.
	keep_source: bool = False


class RankConfig(StrictModel):
	stage_model: str = "rank"
	finalists: int = 3
	substitution_depth: int = 6
	# Drop candidates with no usable figure before ranking. The episode format is
	# built around real paper figures, so a paper we cannot show is a weak pick
	# regardless of how good the result is.
	require_figures: bool = True


class ExtractConfig(StrictModel):
	stage_model: str = "extract"
	# Papers run ~20k-230k chars. The tail is usually appendices, which do not
	# carry headline results, so cap rather than pay for the whole of a long one.
	max_fulltext_chars: int = 90_000
	# Swap in the next-ranked substitute when a finalist fails twice (spec S5).
	substitute_on_failure: bool = True
	min_digests: int = 2
	# Check every number in a headline result against the paper text and drop the
	# result if it does not appear. A model that cites a real table and invents
	# its contents passes the citation check; this catches that.
	verify_numbers: bool = True


class ScriptConfig(StrictModel):
	stage_model: str = "script"
	max_concurrency: int = 3
	# Spec Stage 6: 60-90s per paper segment, ~150 wpm narration budget.
	target_segment_seconds: int = 75
	cold_open_seconds: int = 12
	words_per_minute: int = 150


class BudgetConfig(StrictModel):
	ceiling_usd: float = 20.0
	stage_targets_usd: dict[str, float] = Field(default_factory=dict)


class TTSConfig(StrictModel):
	provider: str | None = None
	model: str | None = None
	voice: str | None = None
	# Speaking rate for the local engine only. `say` defaults to ~175 wpm, which
	# reads as rushed; hosted engines set their own pace and ignore this.
	rate: int | None = 165
	words_per_minute: int = 150


class RenderConfig(StrictModel):
	width: int = 1920
	height: int = 1080
	fps: int = 30
	codec: str = "h264"
	# Cross-dissolve between scenes. Each slide is held for its measured duration
	# *plus* this, and the xfade consumes the overlap, so total runtime is
	# unchanged and audio stays in sync. 0 disables.
	crossfade_seconds: float = 0.4
	# Off by default: no licensed track could be sourced, and the synthesised
	# fallback reads as hum once it is audible. Drop a track at assets/music/ and
	# enable this. See DECISIONS.md D21.
	background_music: bool = False
	# Level of the bed relative to full scale. -18 sits clearly under narration
	# (which peaks near -2) while still being audible.
	music_db: float = -18.0


class Config(StrictModel):
	fetch: FetchConfig
	shortlist: ShortlistConfig
	enrich: EnrichConfig = Field(default_factory=EnrichConfig)
	rank: RankConfig
	extract: ExtractConfig = Field(default_factory=ExtractConfig)
	script: ScriptConfig = Field(default_factory=ScriptConfig)
	budget: BudgetConfig
	models: dict[str, ModelSpec]
	tts: TTSConfig = Field(default_factory=TTSConfig)
	render: RenderConfig = Field(default_factory=RenderConfig)

	def model_for(self, stage_key: str) -> ModelSpec:
		try:
			return self.models[stage_key]
		except KeyError:
			raise KeyError(
				f"No model configured for stage {stage_key!r}. "
				f"Add it under `models:` in config.yaml. "
				f"Configured: {sorted(self.models)}"
			) from None


def load_config(path: Path | None = None) -> Config:
	path = path or REPO_ROOT / "config.yaml"
	if not path.exists():
		raise FileNotFoundError(f"Config not found at {path}")
	return Config.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
