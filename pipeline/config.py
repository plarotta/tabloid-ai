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
	# Skip papers a previous episode already used. The window marker prevents
	# overlap only while windows tile perfectly; once one is rewound, this is what
	# stops a paper headlining twice. See DECISIONS.md D32.
	exclude_covered: bool = True


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
	# Spec Stage 6: 60-90s per paper segment, ~150 wpm narration budget. 75 -> 70
	# on measurement: the 2026-08-19 segments hit their 75s budget exactly and
	# still ran 253s, because that budget is denominated in words at 150 wpm and
	# the voice reads at 140-147. 70 puts the episode at 4:46 (D34).
	target_segment_seconds: int = 70
	# Scenes are pace, not length: a scene is a slide, and the picture changes
	# only when the scene does. Episode one ran 7, 7 and 8 scenes at 12.1s a
	# slide; v3's cap of 6 stretched that to 13.9s without saving any runtime,
	# because the words stayed. Length is held by the word budget instead (D34).
	min_scenes_per_segment: int = 7
	# Hard ceiling, enforced in code - the prompt asked for 5-8 from v1 and the
	# 2026-08-12 run came back over budget anyway. Stating a budget is not the
	# same as holding one.
	max_scenes_per_segment: int = 8
	# Over budget by more than this and the part is sent back for a second,
	# shorter draft (prompts/condense/). Tighter than word_budget_tolerance
	# because it triggers a fix rather than a complaint - one extra Sonnet call
	# per over-long part, about a cent.
	condense_tolerance: float = 0.10
	# How many tightening passes a single part gets. One pass took the worst
	# 2026-08-13 segment from 279 words to 242 against a budget of 175 - short of
	# the target but clearly still moving, and the second pass is another cent.
	condense_max_passes: int = 2
	# Log an error when narration is still over budget by more than this *after*
	# the condense pass. Code never cuts words itself, so what survives both is a
	# signal for tuning the prompt.
	word_budget_tolerance: float = 0.25
	# The cold open names the series and the papers' shared thread before teasing
	# them, so it needs more room than the three flat hooks it used to be (D30).
	# 18 was never realistic for those beats - the 2026-08-13 open ran to 34s
	# against it. 21 is what the structure actually costs when each beat is held
	# to one scene, and the prompt now fixes the scene count to match (D34).
	cold_open_seconds: int = 21
	# One bridge line before each paper, including the first. Long enough to land
	# a sentence, short enough that it reads as punctuation rather than a scene.
	transition_seconds: int = 5
	# Two scenes: what the thread adds up to, then the sign-off. Was fixed in the
	# prompt text as "8-12 seconds"; here so the wrapper budget can be checked.
	outro_seconds: int = 10
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
	# Supported by ElevenLabs. Other providers keep their normal synthesize path.
	contextual_delivery: bool = True
	# Silence appended to every scene's clip, so there is a beat where the slide
	# changes. Lives in the audio file rather than the render timeline - see
	# `append_silence` in stages/voice.py.
	scene_gap_seconds: float = 0.35
	# The beat after a bridge line, before the next paper opens. Longer than a
	# scene gap because the bridge is now a bare signpost - the pause is what
	# separates two papers, where a written sentence used to (D35).
	bridge_pause_seconds: float = 1.0
	# Speaking rate for the local engine only. `say` defaults to ~175 wpm, which
	# reads as rushed; hosted engines set their own pace and ignore this.
	rate: int | None = 165
	words_per_minute: int = 150


class RenderConfig(StrictModel):
	# Old config files retain their established rendering. The shipped config
	# opts into editorial: timed builds without Manim, captions, or camera zooms.
	visual_style: Literal["classic", "editorial"] = "classic"
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
	# Burn the narration into the frame as captions. Built, measured and then
	# turned off on the owner's review - the look, not the mechanism, is what
	# was rejected. Costs a reserved band at the bottom of every slide. D33.
	captions: bool = False
	# Ken Burns: a slow zoom on each slide. Off for the same reason. Free in API
	# terms but not in CPU - every frame becomes distinct, so the encoder can no
	# longer coast through a static slide. D33.
	motion: bool = False
	# Animated callouts (D36). A `result_callout` the model supplied comparison
	# parameters for is rendered by manim instead of Pillow. Needs the `manim`
	# extra; without it, and on any render failure or timeout, the scene falls
	# back to the static callout. Needs `crossfade_seconds > 0` for the same
	# reason motion does - the hard-cut path concatenates images and has nowhere
	# to put a clip.
	animated_callouts: bool = True
	# Per clip. A template takes ~3s at 1080p30; this is the point at which a
	# scene is not worth waiting for and the still is used instead.
	animate_timeout_seconds: int = 120
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
