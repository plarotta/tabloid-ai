"""Command line entry point.

pipeline run                      # full pipeline, new run for today
pipeline run --from shortlist     # replay from a stage using cached artifacts
pipeline run --run 2026-08-07     # target an existing run directory
pipeline cost --run 2026-08-07    # print the cost report
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import load_config
from .llm import MissingAPIKey
from .llm.cost import BudgetExceeded, CostTracker, Pricing
from .paths import STAGE_ORDER, RunPaths, new_run_id, read_json
from .stage import StageContext, StageError
from .stages import (
	EnrichStage,
	ExtractStage,
	FetchStage,
	PackageStage,
	RankStage,
	RenderStage,
	ScriptStage,
	ShortlistStage,
	VoiceStage,
)

app = typer.Typer(add_completion=False, help="arXiv paper video pipeline")
console = Console()

# Stages implemented so far. Later phases append here.
STAGES = {
	"fetch": FetchStage,
	"shortlist": ShortlistStage,
	"enrich": EnrichStage,
	"rank": RankStage,
	"extract": ExtractStage,
	"script": ScriptStage,
	"voice": VoiceStage,
	"render": RenderStage,
	"package": PackageStage,
}


def _setup_logging(verbose: bool) -> None:
	logging.basicConfig(
		level=logging.DEBUG if verbose else logging.INFO,
		format="%(message)s",
		datefmt="[%X]",
		handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
	)


def _build_context(run_id: str, config_path: Path | None, paper: str | None) -> StageContext:
	config = load_config(config_path)
	paths = RunPaths(run_id)
	tracker = CostTracker(
		run_id=run_id,
		calls_path=paths.calls_jsonl,
		pricing=Pricing.load(),
		ceiling_usd=config.budget.ceiling_usd,
		stage_targets=config.budget.stage_targets_usd,
	)
	return StageContext(config=config, paths=paths, tracker=tracker, paper_filter=paper)


def _print_cost(report) -> None:
	table = Table(title=f"Cost report - {report.run_id}", header_style="bold")
	table.add_column("Stage")
	table.add_column("Calls", justify="right")
	table.add_column("In", justify="right")
	table.add_column("Out", justify="right")
	table.add_column("Cost", justify="right")
	table.add_column("Target", justify="right")
	for s in report.by_stage:
		target = f"${s.target_usd:.2f}" if s.target_usd is not None else "-"
		style = "yellow" if s.over_target else None
		table.add_row(
			s.stage,
			str(s.calls),
			f"{s.input_units:,}",
			f"{s.output_units:,}",
			f"${s.cost_usd:.4f}",
			target,
			style=style,
		)
	table.add_section()
	table.add_row(
		"TOTAL", "", "", "", f"${report.total_usd:.4f}", f"${report.ceiling_usd:.2f}", style="bold"
	)
	console.print(table)
	if report.unpriced_calls:
		console.print(
			f"[yellow]{report.unpriced_calls} call(s) had no pricing.yaml entry and are "
			f"counted as $0. Total is an underestimate.[/yellow]"
		)


@app.command()
def run(
	from_stage: str = typer.Option(
		"fetch", "--from", help=f"Start from this stage. One of: {', '.join(STAGE_ORDER)}"
	),
	run_id: str | None = typer.Option(None, "--run", help="Run ID (default: today, YYYY-MM-DD)"),
	paper: str | None = typer.Option(
		None, "--paper", help="Restrict per-paper stages to one arXiv ID"
	),
	config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
	verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
	"""Run the pipeline, reusing cached artifacts for stages before --from."""
	_setup_logging(verbose)
	load_dotenv()

	if from_stage not in STAGE_ORDER:
		console.print(f"[red]Unknown stage {from_stage!r}. Known: {', '.join(STAGE_ORDER)}[/red]")
		raise typer.Exit(2)

	run_id = run_id or new_run_id()
	ctx = _build_context(run_id, config_path, paper)
	console.print(f"[bold]Run {run_id}[/bold]  starting from [cyan]{from_stage}[/cyan]")

	start_index = STAGE_ORDER.index(from_stage)
	planned = [s for s in STAGE_ORDER[start_index:] if s in STAGES]
	if not planned:
		console.print(
			f"[yellow]No implemented stages at or after {from_stage!r}. "
			f"Implemented so far: {', '.join(STAGES)}.[/yellow]"
		)
		raise typer.Exit(1)

	try:
		for name in planned:
			stage = STAGES[name](ctx)
			console.rule(f"[bold cyan]{name}")
			stage.run()
	except BudgetExceeded as e:
		console.print(f"[red bold]Budget exceeded:[/red bold] {e}")
		ctx.tracker.write_report(ctx.paths.cost_report_json)
		raise typer.Exit(3) from e
	except MissingAPIKey as e:
		# Configuration problem, not a bug - a stack trace here is just noise.
		console.print(f"[red bold]Missing credentials:[/red bold] {e}")
		ctx.tracker.write_report(ctx.paths.cost_report_json)
		raise typer.Exit(2) from e
	except StageError as e:
		console.print(f"[red bold]Stage failed:[/red bold] {e}")
		ctx.tracker.write_report(ctx.paths.cost_report_json)
		raise typer.Exit(1) from e

	report = ctx.tracker.write_report(ctx.paths.cost_report_json)
	_print_cost(report)
	console.print(f"[green]Artifacts in {ctx.paths.root}[/green]")

	# Note: state.json is only advanced once the pipeline produces a full episode
	# (Phase 5). Advancing it now would skip papers on the next run.


@app.command()
def cost(
	run_id: str | None = typer.Option(None, "--run", help="Run ID (default: today)"),
) -> None:
	"""Print the cost report for a run."""
	from .schemas import CostReport

	paths = RunPaths(run_id or new_run_id())
	if not paths.cost_report_json.exists():
		console.print(f"[red]No cost report at {paths.cost_report_json}[/red]")
		raise typer.Exit(1)
	_print_cost(CostReport.model_validate(read_json(paths.cost_report_json)))


@app.command()
def shortlist(
	run_id: str | None = typer.Option(None, "--run", help="Run ID (default: today)"),
	limit: int = typer.Option(15, "--limit"),
) -> None:
	"""Show the shortlist from a completed run."""
	from .schemas import ShortlistResult

	paths = RunPaths(run_id or new_run_id())
	if not paths.shortlist_json.exists():
		console.print(f"[red]No shortlist at {paths.shortlist_json}[/red]")
		raise typer.Exit(1)

	result = ShortlistResult.model_validate(read_json(paths.shortlist_json))
	table = Table(title=f"Shortlist - {paths.run_id} ({result.total_candidates} candidates)")
	table.add_column("#", justify="right")
	table.add_column("Score", justify="right")
	table.add_column("arXiv ID")
	table.add_column("Title", max_width=64)
	for i, entry in enumerate(result.shortlist[:limit], 1):
		table.add_row(str(i), f"{entry.score.overall:.1f}", entry.paper.arxiv_id, entry.paper.title)
	console.print(table)


@app.command()
def rank(
	run_id: str | None = typer.Option(None, "--run", help="Run ID (default: today)"),
) -> None:
	"""Show the finalists and substitutes from a completed run."""
	from .schemas import EnrichResult, RankResult

	paths = RunPaths(run_id or new_run_id())
	rank_path = paths.stage_dir("rank") / "rank.json"
	if not rank_path.exists():
		console.print(f"[red]No ranking at {rank_path}[/red]")
		raise typer.Exit(1)

	result = RankResult.model_validate(read_json(rank_path))

	# Titles live in the enrich artifact; the ranking stores only IDs.
	titles: dict[str, str] = {}
	enrich_path = paths.enriched_dir / "enrich.json"
	if enrich_path.exists():
		enriched = EnrichResult.model_validate(read_json(enrich_path))
		titles = {p.arxiv_id: p.paper.title for p in enriched.papers}

	table = Table(title=f"Ranking - {paths.run_id}")
	table.add_column("#", justify="right")
	table.add_column("Role")
	table.add_column("arXiv ID")
	table.add_column("Subfield")
	table.add_column("Title", max_width=52)
	for entry in result.finalists:
		table.add_row(
			str(entry.rank),
			"[green]finalist[/green]",
			entry.arxiv_id,
			entry.subfield or "-",
			titles.get(entry.arxiv_id, ""),
		)
	for entry in result.substitutes:
		table.add_row(
			str(entry.rank),
			"[dim]substitute[/dim]",
			entry.arxiv_id,
			entry.subfield or "-",
			titles.get(entry.arxiv_id, ""),
			style="dim",
		)
	console.print(table)

	for entry in result.finalists:
		console.print(f"\n[bold]{entry.rank}. {entry.arxiv_id}[/bold]")
		console.print(f"  {entry.justification}")


def main() -> None:
	try:
		app()
	except KeyboardInterrupt:
		console.print("[yellow]Interrupted[/yellow]")
		sys.exit(130)


if __name__ == "__main__":
	main()
