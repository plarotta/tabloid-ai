"""Source-grounded one-paper review. --generate bills LLM; --narrate bills TTS.

The digest is curated from the paper, not an extraction-stage validation.
Cached script/audio may be reviewed independently of upstream discovery.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from pipeline.config import load_config
from pipeline.demo import render_demo
from pipeline.llm import MeteredClient
from pipeline.llm.cost import CostTracker, Pricing
from pipeline.paths import RunPaths, write_json
from pipeline.prompts import load_prompt
from pipeline.schemas import HeadlineResult, PaperDigest, SceneManifest
from pipeline.stage import StageContext
from pipeline.stages.script import ScriptStage, scenes_to_markdown

SOURCE = "https://arxiv.org/html/1706.03762v7"
DIGEST = PaperDigest(
	arxiv_id="1706.03762",
	one_sentence_claim="Multiple attention heads improve translation over one head in this ablation.",
	problem_context="Attention Is All You Need evaluates Transformer components on English-to-German translation.",
	method_summary="Section 3.2.2: project representations into parallel attention heads, then concatenate and project their outputs. Section 6.2 varies head count while keeping computation constant by changing each head's dimensions.",
	headline_results=[
		HeadlineResult(
			statement="Table 3: eight heads score 25.8 BLEU, one head scores 24.9, sixteen score 25.8 and thirty-two score 25.4. More heads are not monotonically better.",
			source="Table 3, rows base and (A)",
			numbers="8, 25.8, 1, 24.9, 16, 25.8, 32, 25.4",
		)
	],
	honest_caveats=[
		"Section 6.2 uses newstest2013 development data and no checkpoint averaging. Head dimensions change too; this is not deleting heads from a fixed trained model. The optimum is not universal."
	],
	why_it_matters="Multiple attention views help in this setting, but head count is a design choice to test.",
)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--output", type=Path, default=Path("demos/out/transformer"))
	choices = parser.add_mutually_exclusive_group()
	choices.add_argument("--generate", action="store_true")
	choices.add_argument(
		"--curated", action="store_true", help="Use the reviewed source-grounded fixture"
	)
	audio_options = parser.add_mutually_exclusive_group()
	audio_options.add_argument("--narrate", action="store_true")
	audio_options.add_argument("--reuse-audio", action="store_true")
	args = parser.parse_args()
	load_dotenv()
	output = args.output.resolve()
	output.mkdir(parents=True, exist_ok=True)
	manifest_path = output / "review-manifest.json"
	if args.curated:
		manifest_path.write_text(Path(__file__).with_name("transformer_review.json").read_text())
	if args.generate:
		cfg = load_config()
		cfg.script.target_segment_seconds = 45
		cfg.script.min_scenes_per_segment = 7
		cfg.script.max_scenes_per_segment = 8
		cfg.script.condense_max_passes = 1
		paths = RunPaths("preview", runs_dir=output)
		tracker = CostTracker("preview", paths.calls_jsonl, Pricing.load(), ceiling_usd=1.0)
		context = StageContext(config=cfg, paths=paths, tracker=tracker)
		spec = cfg.model_for(cfg.script.stage_model)
		client = MeteredClient.for_stage(spec, tracker, "script", max_retries=1)
		manifest = ScriptStage(context)._write_segment(client, load_prompt("script"), DIGEST, None)
		tracker.write_report(paths.cost_report_json)
		if manifest is None:
			raise SystemExit("Script generation failed; no narration was requested.")
		write_json(manifest_path, json.loads(manifest.model_dump_json()))
		(output / "script-review.md").write_text(scenes_to_markdown(manifest, "Attention heads"))
		print("Generated script for review. Re-run without --generate to render it.")
	else:
		if not manifest_path.exists():
			raise SystemExit("Generate and review a script first with --generate.")
		manifest = SceneManifest.model_validate_json(manifest_path.read_text())
		print(
			render_demo(
				output, narrate=args.narrate, manifest=manifest, reuse_audio=args.reuse_audio
			)
		)
	(output / "SOURCES.md").write_text(
		f"# Source\n\n{SOURCE}\n\nSections 3.2.2 and 6.2; Table 3, base and rows (A).\n"
		"This is a curated digest plus model-written script, not a live extraction-stage test.\n"
	)


if __name__ == "__main__":
	main()
