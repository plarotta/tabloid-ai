"""The comparison templates (D37, D46): what each accepts, what it refuses, and
that every one of them renders.

The render test is the Linux check D37 left open. It skips without manim on a
developer machine, but CI sets TABLOID_REQUIRE_MANIM=1, so a runner missing
cairo or pango fails here instead of shipping every callout as a still.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from pipeline.schemas import Comparison, SceneManifest, Template

# One real claim per template, taken from the static callouts of shipped
# episodes - the shapes these templates were built for.
EXAMPLES = {
	"two_bar": dict(
		template="two_bar",
		label_a="Diffusion, this paper",
		value_a=200,
		label_b="Language models, Chinchilla",
		value_b=20,
		unit="tokens per parameter",
		note="10x tokens per parameter",
	),
	"count_up": dict(
		template="count_up", label_a="ARC-AGI-3 levels cleared", value_a=97.8, unit="%"
	),
	"split": dict(
		template="split",
		label_a="Complete most harmful objectives",
		value_a=72,
		label_b="Refuse or fail",
	),
	"decay_curve": dict(
		template="decay_curve",
		label_a="Refusal rate, Qwen",
		value_a=93,
		value_b=58,
		steps=100,
		unit="%",
	),
	"delta": dict(
		template="delta",
		label_a="Before attack",
		value_a=58,
		label_b="After benign fine-tuning",
		value_b=32.5,
		unit="%",
		note="HarmBench refusal",
	),
	"gap": dict(
		template="gap",
		label_a="Best agent",
		value_a=57,
		label_b="Humans",
		value_b=85,
		unit="%",
		scale="percent",
		note="Tasks solved",
	),
	"range": dict(
		template="range",
		label_a="Attack success on frontier models",
		value_a=27,
		value_b=29,
		unit="%",
		scale="percent",
	),
	"multiplier": dict(
		template="multiplier",
		label_a="Response to forces on the object",
		value_a=70,
		label_b="Background force",
		note="70x smaller response",
	),
	"fraction": dict(
		template="fraction", label_a="models shifted three or more ranks", value_a=9, value_b=10
	),
	"waffle": dict(
		template="waffle", label_a="of studies used a trial-grade design", value_a=2.5, unit="%"
	),
	"ranking": dict(
		template="ranking",
		label_a="IMO problems solved",
		items=[
			dict(label="RL checkpoint", value=19),
			dict(label="Supervised fine-tune", value=17),
			dict(label="Base model", value=13),
		],
	),
	"funnel": dict(
		template="funnel",
		label_a="Generative AI in healthcare, 2023-2026",
		items=[
			dict(label="PubMed records", value=11628),
			dict(label="Empirical studies", value=2400),
			dict(label="Trial-grade", value=290),
		],
	),
	"trend": dict(
		template="trend",
		label_a="Benchmark-production gap",
		unit="%",
		items=[
			dict(label="10 steps", value=42),
			dict(label="25 steps", value=36),
			dict(label="50 steps", value=30),
			dict(label="100 steps", value=24),
		],
	),
	"stat_row": dict(
		template="stat_row",
		items=[
			dict(label="questions audited", value=48662),
			dict(label="benchmarks", value=8),
			dict(label="models", value=10),
		],
	),
	"scale": dict(
		template="scale",
		label_a="Cosine similarity to the pain direction",
		scale="signed_unit",
		items=[dict(label="fear", value=0.12), dict(label="negative emotion", value=0.21)],
	),
	"stack": dict(
		template="stack",
		label_a="What causes the first mistake",
		unit="%",
		items=[
			dict(label="Reasoning and planning", value=46),
			dict(label="Action faults", value=24),
			dict(label="Perception", value=18),
		],
	),
	"slope": dict(
		template="slope",
		label_a="After sham relief",
		label_b="After real relief",
		unit="%",
		items=[
			dict(label="Qwen 7B", value=97, value_b=72),
			dict(label="Qwen 14B", value=88, value_b=24),
		],
	),
	"threshold": dict(
		template="threshold",
		label_a="Baseline accuracy",
		unit="%",
		threshold=60,
		threshold_label="Drift flips at",
		items=[
			dict(label="GPT-4o", value=78),
			dict(label="Claude", value=71),
			dict(label="Llama 70B", value=58),
			dict(label="Mistral 7B", value=41),
		],
	),
	"flow": dict(
		template="flow",
		label_a="How a force in the image moves the object",
		items=[
			dict(label="Force in the image"),
			dict(label="Decoder Jacobian"),
			dict(label="Generalized force"),
			dict(label="Lagrangian update"),
		],
	),
	"cycle": dict(
		template="cycle",
		label_a="No scored action until the twin agrees",
		items=[
			dict(label="Validate every transition"),
			dict(label="Explore when stuck"),
			dict(label="Plan when validated"),
		],
	),
	"versus": dict(
		template="versus",
		label_a="Reliability",
		relation="!=",
		label_b="Validity",
		label_c="A reproducible benchmark can still measure the wrong thing",
	),
	"versus_minus": dict(
		template="versus",
		label_a="Delegated authority",
		relation="-",
		label_b="Retained evidence",
		label_c="The verifiability gap",
	),
}


def test_every_template_has_an_example_and_a_renderer():
	"""The schema, the renderer and this file must name the same templates."""
	declared = set(get_args(Template))
	src = Path("pipeline/render/manim_scenes.py").read_text()
	tree = ast.parse(src)
	rendered = next(
		{e.value for e in node.value.args[0].elts}
		for node in ast.walk(tree)
		if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TEMPLATES"
	)
	methods = {
		f.name
		for c in tree.body
		if isinstance(c, ast.ClassDef) and c.name == "Clip"
		for f in c.body
		if isinstance(f, ast.FunctionDef)
	}
	assert declared == rendered
	assert declared <= methods
	assert declared == {e["template"] for e in EXAMPLES.values()}


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_every_example_validates(name):
	Comparison.model_validate(EXAMPLES[name])


@pytest.mark.parametrize(
	("params", "reason"),
	[
		(
			dict(template="ranking", items=[dict(label="a", value=1), dict(label="b", value=2)]),
			"3-6 items",
		),
		(
			dict(
				template="ranking",
				items=[
					dict(label="a", value=1000),
					dict(label="b", value=2),
					dict(label="c", value=3),
				],
			),
			"100x",
		),
		(
			dict(
				template="funnel",
				items=[
					dict(label="a", value=10),
					dict(label="b", value=20),
					dict(label="c", value=5),
				],
			),
			"narrow",
		),
		(
			dict(template="stack", items=[dict(label="a", value=60), dict(label="b", value=50)]),
			"past 100",
		),
		(dict(template="threshold", items=[dict(label="a", value=60)]), "needs threshold"),
		(
			dict(
				template="slope",
				label_a="x",
				label_b="y",
				items=[dict(label="a", value=1), dict(label="b", value=2, value_b=3)],
			),
			"value_b on every",
		),
		(dict(template="gap", value_a=120, value_b=50, scale="percent"), "outside the percent"),
		(dict(template="scale", items=[dict(label="a", value=-0.3)]), "signed_unit"),
		(dict(template="range", value_a=9, value_b=3), "low end"),
		(dict(template="fraction", value_a=9.5, value_b=10), "whole numbers"),
		(dict(template="fraction", value_a=11, value_b=10), "value_a <= value_b"),
		(dict(template="multiplier", value_a=500, label_b="one"), "1.5x to 100x"),
		(
			dict(
				template="flow", items=[dict(label="a", value=1), dict(label="b"), dict(label="c")]
			),
			"labels only",
		),
		(dict(template="versus", label_a="a"), "label_a and label_b"),
		(dict(template="delta", value_a=5), "needs value_b"),
		(
			dict(template="two_bar", label_a="a", value_a=5, items=[dict(label="x", value=1)]),
			"takes no items",
		),
	],
)
def test_a_malformed_comparison_is_refused_with_a_reason(params, reason):
	with pytest.raises(ValueError, match=reason):
		Comparison.model_validate(params)


def test_every_drawn_value_is_offered_for_checking():
	c = Comparison.model_validate(EXAMPLES["slope"])
	assert sorted(c.values()) == [24, 72, 88, 97]
	c = Comparison.model_validate(EXAMPLES["threshold"])
	assert 60 in c.values()
	assert Comparison.model_validate(EXAMPLES["flow"]).values() == []


# --- the digest check, across the new shapes --------------------------------


def _manifest(params):
	return SceneManifest.model_validate(
		{
			"arxiv_id": "2609.00001",
			"scenes": [
				{
					"id": "s1",
					"narration": "A line.",
					"visual": {"type": "result_callout", "highlight": "x", "comparison": params},
					"est_seconds": 5,
				}
			],
		}
	)


def test_one_invented_item_drops_the_whole_chart(ctx):
	"""A ranking of three real values and one guessed one is a chart of a guess."""
	from pipeline.stages.script import ScriptStage

	digest = "The RL checkpoint solved 19 problems against 17 for SFT."  # no 13
	out = ScriptStage(ctx)._check_comparisons(_manifest(EXAMPLES["ranking"]), digest)
	assert out.scenes[0].visual.comparison is None


def test_a_threshold_is_checked_like_any_other_value(ctx):
	from pipeline.stages.script import ScriptStage

	digest = "Models at 78, 71, 58 and 41 percent baseline accuracy."  # no 60
	out = ScriptStage(ctx)._check_comparisons(_manifest(EXAMPLES["threshold"]), digest)
	assert out.scenes[0].visual.comparison is None


def test_a_small_count_written_as_a_word_still_matches(ctx):
	from pipeline.stages.script import ScriptStage

	digest = "Nine of ten models shifted by at least three ranks."
	out = ScriptStage(ctx)._check_comparisons(_manifest(EXAMPLES["fraction"]), digest)
	assert out.scenes[0].visual.comparison is not None


def test_one_is_never_matched_as_a_word(ctx):
	"""Every digest says "one" somewhere; it cannot back a claimed 1."""
	from pipeline.stages.script import _states

	assert not _states(1, "one of the models")
	assert _states(1, "1 of the models")


def test_a_thousands_separator_does_not_hide_a_value():
	"""Ep. 10's rerun lost a stat_row to "2,518" not matching 2518 (D46)."""
	from pipeline.stages.script import _states

	assert _states(2518, "collected 2,518 real agent trajectories")
	assert _states(1234567, "1,234,567 tokens")
	assert not _states(34, "sections 3,4 and 5")


def test_a_negative_value_matches_its_magnitude(ctx):
	from pipeline.stages.script import _states

	assert _states(-0.3, "a cosine of \u22120.3")


def test_a_label_only_template_has_nothing_to_check(ctx):
	from pipeline.stages.script import ScriptStage

	out = ScriptStage(ctx)._check_comparisons(_manifest(EXAMPLES["flow"]), "no numbers here")
	assert out.scenes[0].visual.comparison is not None


# --- every template renders -------------------------------------------------


def _manim_or_skip():
	from pipeline.render import animate

	if animate.manim_available():
		return
	if os.environ.get("TABLOID_REQUIRE_MANIM"):
		pytest.fail("TABLOID_REQUIRE_MANIM is set but manim is not installed")
	pytest.skip("manim not installed")


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_every_template_renders_to_its_exact_length(name, tmp_path):
	"""Rendered small and short; what is under test is that the scene builds
	on this machine and lands within a frame or two of the length it was given."""
	_manim_or_skip()
	from pipeline.render import animate
	from pipeline.render.slides import SlideContext
	from pipeline.schemas import Visual

	visual = Visual.model_validate(
		{"type": "result_callout", "highlight": "x", "comparison": EXAMPLES[name]}
	)
	ctx = SlideContext(width=320, height=180, arxiv_id="2609.00001", scene_total=4)
	clip = animate.render_clip(visual, ctx, 3.0, tmp_path, name, fps=10, timeout=120)
	assert clip is not None, f"{name} fell back to a still - see the warning above"
	out = subprocess.run(
		["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
		capture_output=True,
		text=True,
	)
	if out.returncode == 0 and out.stdout.strip():
		# Stage 8 trims a long clip with -t, so over is safe; short is not.
		assert 2.95 <= float(out.stdout) <= 3.5


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_the_editorial_engine_never_crashes_on_a_template(name):
	"""PR #1's renderer knows three templates; the other eighteen must fall back
	to a still there, not take the episode down (D48)."""
	from pipeline.render import editorial
	from pipeline.render.slides import SlideContext
	from pipeline.schemas import Scene

	scene = Scene.model_validate(
		{
			"id": "s1",
			"narration": "A line.",
			"est_seconds": 4,
			"visual": {"type": "result_callout", "highlight": "x", "comparison": EXAMPLES[name]},
		}
	)
	ctx = SlideContext(width=640, height=360, arxiv_id="2609.00001")
	editorial.EditorialScene(scene, ctx, 3.0).poster()
