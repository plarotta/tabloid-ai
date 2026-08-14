"""Loading and validation of config.yaml.

Config is validated up front so a typo in a model name fails before the run
spends money, not three stages in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
	# Hard ceiling on scenes per segment, enforced in code. The prompt has asked
	# for 5-8 since v1 and the 2026-08-12 run came back with 8, 7 and 7 at ~110s
	# each against a 75s target - stating a budget is not the same as holding one.
	max_scenes_per_segment: int = 6
	# Log an error when a segment's narration exceeds its word budget by more
	# than this. Words cannot be trimmed safely in code, so this is a signal for
	# tuning the prompt rather than an automatic fix.
	word_budget_tolerance: float = 0.25
	# The cold open names the series and the papers' shared thread before teasing
	# them, so it needs more room than the three flat hooks it used to be (D30).
	cold_open_seconds: int = 18
	# One bridge line before each paper, including the first. Long enough to land
	# a sentence, short enough that it reads as punctuation rather than a scene.
	transition_seconds: int = 5
	words_per_minute: int = 150


class BudgetConfig(StrictModel):
	ceiling_usd: float = 20.0
	stage_targets_usd: dict[str, float] = Field(default_factory=dict)


class TTSConfig(StrictModel):
	provider: str | None = None
	model: str | None = None
	voice: str | None = None
	# Engine-specific delivery settings, passed through untouched. ElevenLabs
	# takes stability / similarity_boost / style / use_speaker_boost; `stability`
	# is the one that governs how flat the read is. Empty means the voice's own
	# defaults, which is what the first episodes shipped with.
	voice_settings: dict = Field(default_factory=dict)
	# Silence appended to every scene's clip, so there is a beat where the slide
	# changes. Lives in the audio file rather than the render timeline - see
	# `append_silence` in stages/voice.py.
	scene_gap_seconds: float = 0.35
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
	# Cross-dissolve at the seams *between* parts (cold open, transitions,
	# segments, outro). Longer than the within-segment fade so a part boundary
	# reads as a section break rather than another scene change. Unlike the
	# within-segment fade this one costs a full re-encode of the episode, because
	# a filtergraph and a stream copy are mutually exclusive; 0 restores the
	# stream-copy concat. See DECISIONS.md D26.
	episode_crossfade_seconds: float = 0.6
	# Off by default: no licensed track could be sourced, and the synthesised
	# fallback reads as hum once it is audible. Drop a track at assets/music/ and
	# enable this. See DECISIONS.md D21.
	background_music: bool = False
	# Level of the bed relative to full scale. -18 sits clearly under narration
	# (which peaks near -2) while still being audible.
	music_db: float = -18.0


class UploadConfig(StrictModel):
	# Off until the Google Cloud project passes the YouTube API compliance audit
	# (SPEC.md Stage 10). Disabled means dry-run: validate and log, never call the
	# API. This default is deliberate - flipping it is an owner decision.
	enabled: bool = False
	# `private` + `publish_at` is the scheduled-publish idiom; YouTube only honours
	# publishAt on a private video.
	privacy_status: Literal["private", "unlisted", "public"] = "private"
	publish_time_local: str = "12:00"
	timezone: str = "America/New_York"
	# 28 = Science & Technology, 27 = Education.
	category_id: str = "28"
	tags: list[str] = Field(default_factory=list)
	made_for_kids: bool = False
	language: str = "en"
	# If the run finishes after the day's publish time, schedule this many minutes
	# out instead of silently missing the slot. See DECISIONS.md D24.
	late_publish_grace_minutes: int = 15
	# Resumable-upload chunk. 8 MB balances retry cost against request overhead.
	chunk_size_mb: int = 8
	max_retries: int = 5
	poll_timeout_seconds: float = 900.0
	poll_interval_seconds: float = 20.0


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
	upload: UploadConfig = Field(default_factory=UploadConfig)

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
