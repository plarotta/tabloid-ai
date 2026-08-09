"""Pydantic schemas for every artifact that crosses a stage boundary.

Rule: if data moves from one stage to the next, it goes through a model here and
gets written to disk. Stages never pass Python objects to each other directly -
they communicate only through `runs/<date>/<stage>/`. That is what makes
`--from <stage>` re-runs possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
	model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- Stage 1: fetch ----------------------------------------------------------


class Paper(StrictModel):
	"""One arXiv submission as returned by the API. No enrichment yet."""

	arxiv_id: str
	title: str
	abstract: str
	authors: list[str]
	categories: list[str]
	primary_category: str
	submitted: datetime
	updated: datetime
	comment: str | None = None
	pdf_url: str | None = None

	@field_validator("title", "abstract")
	@classmethod
	def _collapse_whitespace(cls, v: str) -> str:
		# arXiv wraps these fields across lines; downstream token counts and
		# prompt formatting both assume single-line text.
		return " ".join(v.split())


# --- Stage 2: shortlist ------------------------------------------------------


class PaperScore(StrictModel):
	"""Cheap-model judgement of a single paper, from title + abstract only."""

	arxiv_id: str
	novelty: float = Field(ge=0, le=10)
	interest: float = Field(ge=0, le=10)
	visual_potential: float = Field(ge=0, le=10)
	rationale: str = ""

	@property
	def overall(self) -> float:
		"""Weighted composite.

		Visual potential is weighted lowest because the Stage 2 model is guessing
		from an abstract - it cannot see the figures. Stage 3 replaces this guess
		with the real figure inventory.
		"""
		return 0.4 * self.novelty + 0.4 * self.interest + 0.2 * self.visual_potential


class ShortlistEntry(StrictModel):
	paper: Paper
	score: PaperScore


class ShortlistResult(StrictModel):
	"""Stage 2 output. `all_scores` is retained for every paper, not just the
	survivors, so shortlist quality can be audited after the fact."""

	generated_at: datetime
	total_candidates: int
	shortlist: list[ShortlistEntry]
	all_scores: list[PaperScore]


# --- Stage 3: enrich ---------------------------------------------------------


SourceQuality = Literal["latex", "pdf", "none"]


class FigureAsset(StrictModel):
	"""One figure, normalised to a file on disk under the paper's `figures/`.

	`converted` is False when the source figure could not be rasterised (EPS
	without ghostscript). The caption is still useful to Stages 4-5, so the entry
	is kept rather than dropped - but Stage 6 must not select it as a visual.
	"""

	number: int
	file: str | None = None
	original_ref: str = ""
	caption: str = ""
	label: str = ""
	converted: bool = True
	source_format: str = ""


class PaperSections(StrictModel):
	title: str = ""
	abstract: str = ""
	intro: str = ""
	conclusion: str = ""
	section_titles: list[str] = Field(default_factory=list)


class SignalsModel(StrictModel):
	github_url: str | None = None
	project_page: str | None = None
	huggingface: bool = False
	hf_upvotes: int = 0
	hf_lookup_ok: bool = False
	affiliations: list[str] = Field(default_factory=list)


class EnrichedPaper(StrictModel):
	"""Stage 3 output for one paper. Mirrors `enrich/<arxiv_id>/` on disk."""

	arxiv_id: str
	paper: Paper
	source_quality: SourceQuality
	figures: list[FigureAsset] = Field(default_factory=list)
	sections: PaperSections = Field(default_factory=PaperSections)
	signals: SignalsModel = Field(default_factory=SignalsModel)
	# Set when enrichment partially failed; ranking uses it to prefer papers we
	# can actually make a good segment from.
	notes: list[str] = Field(default_factory=list)

	@property
	def usable_figures(self) -> list[FigureAsset]:
		return [f for f in self.figures if f.converted and f.file]


class EnrichResult(StrictModel):
	generated_at: datetime
	papers: list[EnrichedPaper]
	failed: list[str] = Field(default_factory=list)


# --- Stage 4: rank -----------------------------------------------------------


class RankedPaper(StrictModel):
	arxiv_id: str
	rank: int = Field(ge=1)
	justification: str
	subfield: str = ""


class RankResult(StrictModel):
	"""Stage 4 output. `finalists` are the episode's papers; `substitutes` are
	the next-ranked candidates, kept so a paper that fails Stage 5 can be swapped
	out instead of shipping a short episode (spec section 5, failure policy)."""

	generated_at: datetime
	finalists: list[RankedPaper]
	substitutes: list[RankedPaper] = Field(default_factory=list)


# --- Stage 5: extract (PaperDigest) ------------------------------------------


class HeadlineResult(StrictModel):
	"""One reportable result.

	`source` is required and must name a specific table, figure or section: the
	spec's fidelity rule is that every number in the episode is traceable back to
	a place in the paper. A result with no citation is not usable.
	"""

	statement: str
	source: str
	numbers: str = ""


class KeyFigure(StrictModel):
	"""A figure worth showing on screen. `file` is validated against the figures
	Stage 3 actually produced - the model cannot invent one."""

	file: str
	caption: str = ""
	why_show: str = ""


class PaperDigest(StrictModel):
	"""Stage 5 output for one paper. This targeted schema - not a lossless
	summary of the paper - is what Stage 6 writes narration from."""

	arxiv_id: str
	one_sentence_claim: str
	problem_context: str
	method_summary: str
	headline_results: list[HeadlineResult] = Field(default_factory=list)
	key_figures: list[KeyFigure] = Field(default_factory=list)
	honest_caveats: list[str] = Field(default_factory=list)
	why_it_matters: str = ""


class ExtractResult(StrictModel):
	generated_at: datetime
	digests: list[PaperDigest]
	failed: list[str] = Field(default_factory=list)
	substituted: list[str] = Field(default_factory=list)


# --- Stage 6: script (SceneManifest) -----------------------------------------


VisualType = Literal["title_card", "figure", "bullet_slide", "result_callout"]


class Visual(StrictModel):
	type: VisualType
	figure_file: str | None = None
	title: str | None = None
	bullets: list[str] = Field(default_factory=list)
	highlight: str | None = None


class Scene(StrictModel):
	id: str
	narration: str
	visual: Visual
	# The script model's own guess. Stage 8 must build its timeline from measured
	# audio durations instead (spec Stage 7); this is only for pacing the script.
	est_seconds: float = Field(gt=0)


class SceneManifest(StrictModel):
	"""One paper's segment. Renderable standalone, so it opens with its own
	title card (spec Stage 6, for Shorts repurposing)."""

	arxiv_id: str
	scenes: list[Scene]

	@property
	def est_seconds(self) -> float:
		return sum(s.est_seconds for s in self.scenes)


class EpisodeMetadata(StrictModel):
	"""Everything the episode needs beyond the per-paper segments."""

	title: str
	description: str
	thumbnail_text_options: list[str] = Field(default_factory=list)
	cold_open: SceneManifest | None = None
	outro: SceneManifest | None = None


class ScriptResult(StrictModel):
	generated_at: datetime
	segments: list[SceneManifest]
	episode: EpisodeMetadata

	@property
	def est_seconds(self) -> float:
		total = sum(s.est_seconds for s in self.segments)
		for extra in (self.episode.cold_open, self.episode.outro):
			if extra is not None:
				total += extra.est_seconds
		return total


# --- Stage 7: voice ----------------------------------------------------------


class SceneAudio(StrictModel):
	"""One narrated scene. `duration_seconds` is **measured** from the produced
	audio, never the script's `est_seconds` - Stage 8 syncs to this."""

	scene_id: str
	audio_file: str
	duration_seconds: float = Field(gt=0)
	characters: int
	est_seconds: float = 0.0

	@property
	def drift_seconds(self) -> float:
		"""How far the script's estimate was from reality. Reported so the script
		prompt's pacing rules can be tuned against measured speech."""
		return self.duration_seconds - self.est_seconds


class SegmentAudio(StrictModel):
	arxiv_id: str
	scenes: list[SceneAudio]

	@property
	def duration_seconds(self) -> float:
		return sum(s.duration_seconds for s in self.scenes)


class VoiceResult(StrictModel):
	generated_at: datetime
	provider: str
	model: str
	voice: str | None = None
	segments: list[SegmentAudio]
	cold_open: SegmentAudio | None = None
	outro: SegmentAudio | None = None

	@property
	def duration_seconds(self) -> float:
		total = sum(s.duration_seconds for s in self.segments)
		for extra in (self.cold_open, self.outro):
			if extra is not None:
				total += extra.duration_seconds
		return total


# --- Stage 8: render ---------------------------------------------------------


class RenderedSegment(StrictModel):
	arxiv_id: str
	video_file: str
	duration_seconds: float
	scenes: int


class RenderResult(StrictModel):
	generated_at: datetime
	segments: list[RenderedSegment]
	episode_file: str | None = None
	duration_seconds: float = 0.0
	width: int = 1920
	height: int = 1080
	fps: int = 30


# --- Stage 9: package --------------------------------------------------------


class PackageResult(StrictModel):
	"""The upload-ready bundle. Written to `output/metadata.json`, which doubles
	as the manifest and as the copy-paste source for the YouTube upload form."""

	generated_at: datetime
	title: str = ""
	description: str = ""
	thumbnail_text_options: list[str] = Field(default_factory=list)
	episode_file: str = ""
	segment_files: list[str] = Field(default_factory=list)
	thumbnail_file: str | None = None
	duration_seconds: float = 0.0
	# Chapter marks derived from measured audio, so they land on the cuts.
	chapters: list[dict] = Field(default_factory=list)
	total_cost_usd: float = 0.0


# --- Cost accounting ---------------------------------------------------------


class CallRecord(StrictModel):
	"""One billed API call. Written append-only so a crashed run still leaves an
	accurate partial cost trail."""

	timestamp: datetime
	stage: str
	provider: str
	model: str
	kind: Literal["text", "audio"] = "text"
	input_units: int
	output_units: int
	cost_usd: float
	# Set when the model/provider pair is missing from pricing.yaml. The call
	# still runs; it is priced at zero and flagged here so the report cannot
	# silently understate spend.
	unpriced: bool = False


class StageCost(StrictModel):
	stage: str
	calls: int
	input_units: int
	output_units: int
	cost_usd: float
	target_usd: float | None = None
	over_target: bool = False


class CostReport(StrictModel):
	run_id: str
	generated_at: datetime
	total_usd: float
	ceiling_usd: float
	over_ceiling: bool
	unpriced_calls: int
	by_stage: list[StageCost]
