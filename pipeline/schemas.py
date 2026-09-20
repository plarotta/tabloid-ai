"""Pydantic schemas for every artifact that crosses a stage boundary.

Rule: if data moves from one stage to the next, it goes through a model here and
gets written to disk. Stages never pass Python objects to each other directly -
they communicate only through `runs/<date>/<stage>/`. That is what makes
`--from <stage>` re-runs possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


VisualType = Literal[
	"title_card", "figure", "bullet_slide", "result_callout", "transition", "process", "contrast"
]

StoryBeat = Literal["hook", "context", "mechanism", "evidence", "caveat", "payoff"]


class Comparison(StrictModel):
	"""Parameters for an animated callout, chosen from a fixed set of templates.

	The model fills these in; it never writes animation code. That is the whole
	design: manim scenes that compile, read well and match the narration are hard
	to generate and impossible to validate by eye at scale, whereas four numbers
	and two labels can be checked in full (D36).

	Optional everywhere it appears. Absent - or invalid, or unrenderable - and the
	scene stays the static callout it is today.
	"""

	template: Literal["two_bar", "count_up", "split"]
	# The paper's own value; the one the accent colour is spent on.
	label_a: str
	value_a: float = Field(allow_inf_nan=False)
	unit: str = ""
	# The value being compared against. `two_bar` requires it; the others ignore
	# it. This is what BASE exists for (D36).
	label_b: str = ""
	value_b: float | None = Field(default=None, allow_inf_nan=False)
	# A short line under the figure - "10x tokens per parameter". Never a sentence.
	note: str = ""

	@field_validator("label_a", "label_b", "unit", "note", mode="before")
	@classmethod
	def stringify(cls, v: object) -> object:
		"""Accept a number, or a null, where a label was asked for.

		Both observed live on the first two runs of prompt v6: `note: 65` and
		`unit: null`, each costing an animation to a type error. The value is going
		onto a slide as text either way and an absent label simply does not draw,
		so refusing either buys nothing. `label_a` becoming empty is caught by
		`check_template` where it actually matters.
		"""
		if v is None:
			return ""
		if isinstance(v, bool):
			return str(v)
		return f"{v:g}" if isinstance(v, int | float) else v

	@model_validator(mode="after")
	def check_template(self) -> Comparison:
		if self.value_a <= 0:
			raise ValueError("value_a must be positive; a bar cannot have zero length")
		if self.template == "two_bar":
			if self.value_b is None or self.value_b <= 0:
				raise ValueError("two_bar needs a positive value_b to compare against")
			if not self.label_b:
				raise ValueError("two_bar needs label_b to say what value_b is")
			ratio = max(self.value_a, self.value_b) / min(self.value_a, self.value_b)
			if ratio > 500:
				raise ValueError(
					f"two_bar values differ by {ratio:.0f}x; the smaller bar would be "
					"invisible. Use count_up and put the ratio in `note`."
				)
		if self.template == "split" and not 0 < self.value_a < 100:
			raise ValueError("split expects value_a as a percentage between 0 and 100")
		return self

	def numbers(self) -> str:
		"""The claimed values, for checking against the digest they came from."""
		parts = [f"{self.value_a:g}"]
		if self.value_b is not None:
			parts.append(f"{self.value_b:g}")
		return ", ".join(parts)


class Visual(StrictModel):
	type: VisualType
	figure_file: str | None = None
	title: str | None = None
	bullets: list[str] = Field(default_factory=list)
	highlight: str | None = None
	# Only ever read for a `result_callout`, and only when render.animated_callouts
	# is on. See D36.
	comparison: Comparison | None = None
	# A specific table/figure/section from the digest, shown beside the evidence.
	source: str = ""


class Scene(StrictModel):
	id: str
	narration: str
	visual: Visual
	# The script model's own guess. Stage 8 must build its timeline from measured
	# audio durations instead (spec Stage 7); this is only for pacing the script.
	est_seconds: float = Field(gt=0)
	# Optional so cached scripts remain valid. Direction is metadata, never speech.
	beat: StoryBeat | None = None
	# None uses the configured default; zero is a deliberate continuous cut.
	pause_after: float | None = Field(default=None, ge=0, le=1.2, allow_inf_nan=False)


class SceneManifest(StrictModel):
	"""One paper's segment. Renderable standalone, so it opens with its own
	title card (spec Stage 6, for Shorts repurposing)."""

	arxiv_id: str
	scenes: list[Scene]

	@property
	def est_seconds(self) -> float:
		return sum(s.est_seconds for s in self.scenes)


class Transition(StrictModel):
	"""The bridge from whatever just played into the next paper.

	Deliberately *not* part of the segment it introduces. A segment that opened
	with "but a piece of paper is not the only way in" could not be published on
	its own, and spec Stage 6 requires that it can be. So a transition is an
	episode-level part, like the cold open and the outro: it is stitched into the
	episode cut and left out of `segment_<id>.mp4`.

	One scene, always. A bridge is a single sentence; giving it a scene list
	invites the model to write a second segment intro.
	"""

	# The paper this leads into. Matches a `SceneManifest.arxiv_id` in `segments`.
	into_arxiv_id: str
	narration: str
	# 2-5 words, burned onto the card. The next paper's angle, not its title.
	label: str = ""
	est_seconds: float = Field(gt=0)

	def manifest(self, index: int = 0, total: int = 0) -> SceneManifest:
		"""Adapt to the shape Stages 7 and 8 already consume.

		`index`/`total` become the chapter marker on the card ("02 / 03"); they
		are positional, so they are supplied by the caller rather than stored.
		"""
		marker = f"{index + 1:02d} / {total:02d}" if total else ""
		return SceneManifest(
			arxiv_id="episode",
			scenes=[
				Scene(
					id=f"t{index + 1}",
					narration=self.narration,
					visual=Visual(type="transition", title=self.label, highlight=marker),
					est_seconds=self.est_seconds,
				)
			],
		)


class EpisodeMetadata(StrictModel):
	"""Everything the episode needs beyond the per-paper segments."""

	title: str
	description: str
	thumbnail_text_options: list[str] = Field(default_factory=list)
	cold_open: SceneManifest | None = None
	# One per segment, in play order, each introducing the paper it names. The
	# first bridges the cold open into paper one - the seam the owner review
	# called out. Empty when the wrapper call failed.
	transitions: list[Transition] = Field(default_factory=list)
	outro: SceneManifest | None = None


class ScriptResult(StrictModel):
	generated_at: datetime
	segments: list[SceneManifest]
	episode: EpisodeMetadata

	def transition_manifest(self, transition: Transition) -> SceneManifest:
		"""Chapter numbers follow papers, while cached bridge scene ids stay stable."""
		order = [s.arxiv_id for s in self.segments]
		bridge_ids = [t.into_arxiv_id for t in self.episode.transitions]
		manifest = transition.manifest(bridge_ids.index(transition.into_arxiv_id), len(order))
		paper_index = order.index(transition.into_arxiv_id)
		manifest.scenes[0].visual.highlight = f"{paper_index + 1:02d} / {len(order):02d}"
		return manifest

	@property
	def est_seconds(self) -> float:
		total = sum(s.est_seconds for s in self.segments)
		total += sum(t.est_seconds for t in self.episode.transitions)
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
	# Keyed to the paper each one introduces via `SegmentAudio.arxiv_id`, so the
	# render and chapter arithmetic can place them without relying on order.
	transitions: list[SegmentAudio] = Field(default_factory=list)
	outro: SegmentAudio | None = None

	@property
	def duration_seconds(self) -> float:
		total = sum(s.duration_seconds for s in self.segments)
		total += sum(t.duration_seconds for t in self.transitions)
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


# --- Stage 10: upload --------------------------------------------------------


class UploadResult(StrictModel):
	"""What Stage 10 did, written to `output/upload.json`.

	Doubles as the idempotency record: a re-run that finds a `video_id` here with
	`dry_run` false must not upload again (spec Stage 10).
	"""

	generated_at: datetime
	dry_run: bool
	video_id: str | None = None
	video_url: str | None = None
	privacy_status: str = ""
	# RFC3339, as sent. Null when publishing immediately or when scheduling was
	# skipped.
	publish_at: str | None = None
	# YouTube's AI-disclosure flag. Always true for this channel - non-negotiable
	# per the spec, and asserted in the tests.
	contains_synthetic_media: bool = True
	thumbnail_set: bool = False
	processing_status: str = ""
	video_file: str = ""
	video_bytes: int = 0
	# Exactly what was (or would be) sent to videos.insert. In dry-run this is the
	# whole point of the artifact: it is reviewable before anything is published.
	request_body: dict = Field(default_factory=dict)
	warnings: list[str] = Field(default_factory=list)


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
