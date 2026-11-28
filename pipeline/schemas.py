"""Pydantic schemas for every artifact that crosses a stage boundary.

Rule: if data moves from one stage to the next, it goes through a model here and
gets written to disk. Stages never pass Python objects to each other directly -
they communicate only through `runs/<date>/<stage>/`. That is what makes
`--from <stage>` re-runs possible.
"""

from __future__ import annotations

import itertools
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


Template = Literal[
	# One or two values (D37, D42)
	"two_bar",
	"count_up",
	"split",
	"decay_curve",
	# One or two values on a scale (D46)
	"delta",
	"gap",
	"range",
	"multiplier",
	"fraction",
	"waffle",
	# Several values (D46)
	"ranking",
	"funnel",
	"trend",
	"stat_row",
	"scale",
	"stack",
	"slope",
	"threshold",
	# Labels only, no numbers (D46)
	"flow",
	"cycle",
	"versus",
]

# The templates that draw words and no numbers. Nothing in them can be checked
# against the digest, which is exactly their risk: a true label describing the
# wrong thing (D37's `note` failure, at the size of a whole slide).
LABEL_ONLY_TEMPLATES = frozenset({"flow", "cycle", "versus"})

# What a template needs, by field. Checked in `Comparison.check_template` so a
# malformed comparison is dropped on its own scene (D37) rather than handed to
# manim to fail on.
_ITEM_COUNTS: dict[str, tuple[int, int]] = {
	"ranking": (3, 6),
	"funnel": (3, 5),
	"trend": (3, 6),
	"stat_row": (2, 4),
	"scale": (1, 3),
	"stack": (2, 5),
	"slope": (2, 4),
	"threshold": (1, 5),
	"flow": (3, 5),
	"cycle": (3, 5),
}

# `versus` draws one of these between its two terms. ASCII on the way in, since
# that is what a model reliably types inside JSON; the glyph is the renderer's.
Relation = Literal["!=", "vs", "->", ">", "<", "~", "-", "+"]

# The axis a value sits on. An enum rather than free bounds on purpose: an axis
# the model could set is an axis the model could stretch until a two-point gap
# fills the frame, and that is the same lie as an invented baseline.
ScaleKind = Literal["auto", "percent", "unit", "signed_unit"]
SCALE_BOUNDS: dict[str, tuple[float, float]] = {
	"percent": (0.0, 100.0),
	"unit": (0.0, 1.0),
	"signed_unit": (-1.0, 1.0),
}


class ComparisonItem(StrictModel):
	"""One row, bar, point or box in a multi-value template."""

	label: str
	value: float | None = None
	# `slope` only: the item's value under the second condition.
	value_b: float | None = None

	@field_validator("label", mode="before")
	@classmethod
	def stringify(cls, v: object) -> object:
		if v is None:
			return ""
		return f"{v:g}" if isinstance(v, int | float) and not isinstance(v, bool) else v


class Comparison(StrictModel):
	"""Parameters for an animated callout, chosen from a fixed set of templates.

	The model fills these in; it never writes animation code. That is the whole
	design: manim scenes that compile, read well and match the narration are hard
	to generate and impossible to validate by eye at scale, whereas a handful of
	numbers and labels can be checked in full (D36, D37).

	The template set grew from four to twenty-one in D46, each new one fitted to a
	claim shape that recurred across twelve episodes' static callouts. The field
	set grew by much less: most templates reuse `value_a`/`value_b`, and the
	multi-value ones share `items`.

	Optional everywhere it appears. Absent - or invalid, or unrenderable - and the
	scene stays the static callout it is today.
	"""

	template: Template
	# The paper's own value; the one the accent colour is spent on.
	label_a: str = ""
	value_a: float | None = Field(default=None, allow_inf_nan=False)
	unit: str = ""
	# The value being compared against. This is what BASE exists for (D36).
	label_b: str = ""
	value_b: float | None = Field(default=None, allow_inf_nan=False)
	# A short line under the figure - "10x tokens per parameter". Never a sentence.
	note: str = ""
	# `decay_curve` only: how many steps the fall is drawn across. Checked against
	# the digest like every other value, because the x-axis is a claim too (D42).
	steps: float | None = None
	# Multi-value templates. The first item is the paper's and wears the accent.
	items: list[ComparisonItem] = Field(default_factory=list)
	# `threshold`, and optionally `range`: a reference line the values sit against.
	threshold: float | None = None
	threshold_label: str = ""
	# `gap`, `range`, `scale`: the axis. See ScaleKind.
	scale: ScaleKind = "auto"
	# `versus` only.
	relation: Relation = "vs"
	label_c: str = ""

	@field_validator(
		"label_a", "label_b", "label_c", "unit", "note", "threshold_label", mode="before"
	)
	@classmethod
	def stringify(cls, v: object) -> object:
		"""Accept a number, or a null, where a label was asked for.

		Both observed live on the first two runs of prompt v6: `note: 65` and
		`unit: null`, each costing an animation to a type error. The value is going
		onto a slide as text either way and an absent label simply does not draw,
		so refusing either buys nothing. A label a template needs is caught by
		`check_template` where it actually matters.
		"""
		if v is None:
			return ""
		if isinstance(v, bool):
			return str(v)
		return f"{v:g}" if isinstance(v, int | float) else v

	@model_validator(mode="after")
	def check_template(self) -> Comparison:
		t = self.template
		need = _requirements(self)
		if need:
			raise ValueError(f"{t} {need}")
		return self

	def values(self) -> list[float]:
		"""Every number this comparison draws, for checking against the digest."""
		out = [v for v in (self.value_a, self.value_b, self.steps, self.threshold) if v is not None]
		for it in self.items:
			out += [v for v in (it.value, it.value_b) if v is not None]
		return out

	def numbers(self) -> str:
		"""The claimed values, for logs and for the digest check's message."""
		return ", ".join(f"{v:g}" for v in self.values())


def _ratio_ok(values: list[float], limit: float = 100.0) -> bool:
	"""Whether the smallest of `values` would still be visible beside the largest."""
	pos = [abs(v) for v in values if v]
	return not pos or max(pos) / min(pos) <= limit


def _requirements(c: Comparison) -> str:
	"""What `c` is missing for its template, as the end of a sentence, or ''."""
	t, a, b, items = c.template, c.value_a, c.value_b, c.items
	vals = [it.value for it in items]

	lo_n, hi_n = _ITEM_COUNTS.get(t, (0, 0))
	if hi_n and not lo_n <= len(items) <= hi_n:
		return f"needs {lo_n}-{hi_n} items, got {len(items)}"
	if t not in _ITEM_COUNTS and items:
		return "takes no items; use value_a/value_b"
	if t in _ITEM_COUNTS and any(not it.label for it in items):
		return "needs a label on every item"

	if t in LABEL_ONLY_TEMPLATES:
		if any(v is not None for v in vals) or a is not None or b is not None:
			return "draws labels only; put numbers in a numeric template"
		if any(len(it.label) > 40 for it in items):
			return "labels must be short (40 characters at most)"
		if t == "versus" and not (c.label_a and c.label_b):
			return "needs label_a and label_b"
		return ""

	if t in _ITEM_COUNTS:
		if any(v is None for v in vals):
			return "needs a value on every item"
		if t == "slope" and any(it.value_b is None for it in items):
			return "needs value_b on every item"
		if t == "slope" and not (c.label_a and c.label_b):
			return "needs label_a and label_b to name the two conditions"
		if t in ("ranking", "funnel", "stat_row", "stack") and any(v <= 0 for v in vals):
			return "needs positive values"
		if t in ("ranking", "funnel", "threshold") and not _ratio_ok([*vals, c.threshold or 0]):
			return "values differ by more than 100x; the smallest bar would be invisible"
		if t == "funnel" and any(x < y for x, y in itertools.pairwise(vals)):
			return "needs values that only narrow, stage to stage"
		if t == "stack" and sum(vals) > 100.5:
			return f"segments sum to {sum(vals):g}, past 100 percent"
		if t == "threshold" and c.threshold is None:
			return "needs threshold"
		if t == "trend" and any(v < 0 for v in vals):
			return "needs values of zero or more"
		if t == "scale":
			return _on_scale(c, vals)
		return ""

	# One or two values.
	if a is None:
		return "needs value_a"
	if t in ("two_bar", "count_up", "decay_curve", "multiplier", "waffle") and a <= 0:
		return "needs a positive value_a; a bar cannot have zero length"
	if t == "two_bar":
		if b is None or b <= 0:
			return "needs a positive value_b to compare against"
		if not c.label_b:
			return "needs label_b to say what value_b is"
		ratio = max(a, b) / min(a, b)
		if ratio > 500:
			return (
				f"values differ by {ratio:.0f}x; the smaller bar would be "
				"invisible. Use count_up and put the ratio in `note`."
			)
	if t == "split" and not 0 < a < 100:
		return "expects value_a as a percentage between 0 and 100"
	if t in ("delta", "gap", "range"):
		if b is None:
			return "needs value_b"
		if a == b:
			return "needs two different values"
		if t == "range" and a > b:
			return "needs value_a as the low end and value_b as the high end"
		if t in ("gap", "range"):
			return _on_scale(c, [a, b, c.threshold] if c.threshold is not None else [a, b])
		if min(a, b) < 0:
			return "needs values of zero or more"
	if t == "multiplier" and not 1.5 <= a <= 100:
		return "draws 1.5x to 100x; past that the blocks are too small to count"
	if t == "multiplier" and not c.label_b:
		return "needs label_b to name the single unit"
	if t == "fraction":
		if b is None or a != int(a) or b != int(b):
			return "needs whole numbers: value_a of value_b"
		if not 0 <= a <= b <= 100 or b < 2:
			return "needs 0 <= value_a <= value_b <= 100"
	if t == "waffle" and not 0 < a <= 100:
		return "expects value_a as a percentage between 0 and 100"
	return ""


def _on_scale(c: Comparison, vals: list[float]) -> str:
	if c.scale == "auto":
		return "" if all(v >= 0 for v in vals) else "needs scale signed_unit for negative values"
	lo, hi = SCALE_BOUNDS[c.scale]
	if any(not lo <= v <= hi for v in vals):
		return f"has a value outside the {c.scale} scale ({lo:g} to {hi:g})"
	return ""


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
	# Panels of a `figure` the camera visits in order, by the caption's letters
	# ("b", then "c"). Only letters Stage 6 offered for that figure survive (D50).
	focus: list[str] = Field(default_factory=list)

	@field_validator("focus", mode="before")
	@classmethod
	def _focus_none_is_empty(cls, v):
		# Models write `"focus": null` on a scene with nothing to point at.
		if v is None:
			return []
		return [v] if isinstance(v, str) else v


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
	# Numbers this scene says or shows that its digest neither states nor
	# derives (D49). Flagged for review, never rewritten: speech cannot be fixed
	# by deleting a figure from it.
	unverified_numbers: list[str] = Field(default_factory=list)
	# A second model's reading of this scene against the digest (D50): claims
	# it found unsupported, as "quote: problem". Flagged, never rewritten.
	fact_flags: list[str] = Field(default_factory=list)


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
	# Which episode of the series this is, stamped on the first packaging and
	# reused on every later one. Read live from `state.json` it would drift: a
	# re-render months later would relabel a finished episode with whatever
	# number the series had reached (D38).
	episode_number: int | None = None
	title: str = ""
	description: str = ""
	thumbnail_text_options: list[str] = Field(default_factory=list)
	episode_file: str = ""
	segment_files: list[str] = Field(default_factory=list)
	thumbnail_file: str | None = None
	# The other options, rendered for YouTube's Test & Compare (up to three
	# thumbnails per video), and a sheet of all of them at feed sizes (D50).
	thumbnail_variants: list[str] = Field(default_factory=list)
	thumbnail_preview: str | None = None
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
	kind: Literal["text", "audio", "image"] = "text"
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
