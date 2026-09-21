"""Command line entry point.

pipeline run                      # full pipeline, new run for today
pipeline run --from shortlist     # replay from a stage using cached artifacts
pipeline run --run 2026-08-07     # target an existing run directory
pipeline cost --run 2026-08-07    # print the cost report
pipeline youtube-auth             # one-time OAuth, prints a refresh token
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
from .paths import STAGE_ORDER, RunPaths, new_run_id, read_json, write_json
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
	UploadStage,
	VoiceStage,
)

app = typer.Typer(add_completion=False, help="arXiv paper video pipeline")
console = Console()

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
	"upload": UploadStage,
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
	through: str | None = typer.Option(
		None, "--through", help="Stop after this stage (e.g. script to review before narration)"
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
	if through is not None and (
		through not in STAGE_ORDER or STAGE_ORDER.index(through) < STAGE_ORDER.index(from_stage)
	):
		console.print("[red]--through must name a stage at or after --from.[/red]")
		raise typer.Exit(2)

	run_id = run_id or new_run_id()
	ctx = _build_context(run_id, config_path, paper)
	console.print(f"[bold]Run {run_id}[/bold]  starting from [cyan]{from_stage}[/cyan]")

	start_index = STAGE_ORDER.index(from_stage)
	stop_index = STAGE_ORDER.index(through) + 1 if through else len(STAGE_ORDER)
	planned = [s for s in STAGE_ORDER[start_index:stop_index] if s in STAGES]
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

	# A prior package may exist on disk. A new --through script invocation has
	# not covered that fetch window, regardless of what an earlier run left.
	advanced = _advance_window(ctx, from_stage, paper) if "package" in planned else None
	if advanced is not None:
		console.print(f"[green]Next run's window starts at {advanced:%Y-%m-%d %H:%M} UTC[/green]")


def _advance_window(ctx: StageContext, from_stage: str, paper: str | None):
	"""Move `state.json` forward, but only after a run that earned it.

	The marker is what stops consecutive runs re-covering the same papers, so it
	moves only when this invocation actually fetched a window *and* carried it all
	the way to a packaged episode. A partial replay (`--from script`) or a
	single-paper run has not covered a new window and must leave it alone.

	It moves to the newest submission the fetch actually saw, not to now and not
	to the requested window end: the minutes a run spends in the LLM stages, and
	the hours arXiv's index runs behind, would otherwise become permanent holes in
	coverage (D31).

	The same run also records the papers it used, so a later window that overlaps
	this one cannot give any of them a second segment (D32).
	"""
	from .state import mark_covered, mark_successful_run

	if from_stage != "fetch" or paper:
		return None
	if not PackageStage(ctx).is_complete():
		return None
	end = FetchStage(ctx).window_end()
	if end is None:
		return None
	mark_successful_run(end)

	try:
		used = [m.arxiv_id for m in ScriptStage(ctx).load().segments]
	except Exception as e:  # a packaged run without a readable script is odd, not fatal
		console.print(f"[yellow]Could not record which papers this episode used ({e})[/yellow]")
		used = []
	if used:
		mark_covered(used)
		console.print(f"[green]Recorded {len(used)} paper(s) as covered[/green]")
	return end


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
def review(
	run_id: str | None = typer.Option(None, "--run", help="Run ID (default: today)"),
	config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
	"""Inspect story beats and pacing, without making any API calls."""
	from .editorial import pacing_report
	from .schemas import ScriptResult, VoiceResult

	paths = RunPaths(run_id or new_run_id())
	script_path = paths.stage_dir("script") / "script.json"
	if not script_path.exists():
		console.print("[red]No script found. Run through the script stage first.[/red]")
		raise typer.Exit(1)
	script = ScriptResult.model_validate(read_json(script_path))
	voice_path = paths.stage_dir("voice") / "voice.json"
	voice = VoiceResult.model_validate(read_json(voice_path)) if voice_path.exists() else None
	if voice and voice.generated_at < script.generated_at:
		console.print("[yellow]The script is newer than the audio; timing is estimated.[/yellow]")
		voice = None
	cfg = load_config(config_path)
	report = pacing_report(
		script, voice, scene_gap=cfg.tts.scene_gap_seconds, bridge_gap=cfg.tts.bridge_pause_seconds
	)
	write_json(paths.root / "pacing.json", report)
	table = Table(title=f"Edit review · {report['timing']} · {report['duration_seconds']:.1f}s")
	for title in ("Part", "Scene", "Beat", "Visual", "Seconds", "Timing"):
		table.add_column(title)
	for row in report["scenes"]:
		table.add_row(
			row["part"],
			row["scene"],
			row["beat"] or "—",
			row["visual"],
			f"{row['duration_seconds']:.1f}",
			row["timing"],
		)
	console.print(table)
	for issue in report["issues"]:
		console.print(f"[yellow]{issue['part']}/{issue['scene']}:[/yellow] {issue['message']}")
	if not report["issues"]:
		console.print("[green]No pacing flags. Watch the render to judge the edit.[/green]")


@app.command()
def demo(
	output: Path = typer.Option(Path("demos/out/editorial"), "--output"),
	height: int = typer.Option(720, "--height", min=180, max=2160),
	narrate: bool = typer.Option(
		False, "--narrate", help="Use configured TTS (billed); default is silent"
	),
) -> None:
	"""Render a self-contained style preview; no research or LLM calls."""
	from .demo import render_demo

	_setup_logging(False)
	load_dotenv()
	if height % 2 or round(height * 16 / 9) % 2:
		console.print("[red]Choose an even 16:9 resolution, such as 360, 720, or 1080.[/red]")
		raise typer.Exit(2)
	try:
		path = render_demo(output.resolve(), height, narrate=narrate)
	except (StageError, BudgetExceeded) as e:
		console.print(f"[red]{e}[/red]")
		raise typer.Exit(1) from e
	console.print(
		f"[green]{'Narrated' if narrate else 'Silent'} illustrative preview: {path}[/green]"
	)


@app.command()
def youtube_auth(
	client_secrets: Path | None = typer.Option(
		None, "--client-secrets", help="Google OAuth client JSON. Defaults to env vars."
	),
	port: int = typer.Option(0, "--port", help="Local callback port (0 = pick one)"),
) -> None:
	"""Run the one-time interactive OAuth flow and print a refresh token.

	Every later run is non-interactive: Stage 10 exchanges the refresh token for
	an access token itself. Store the printed value as a secret - it is a
	long-lived credential for the channel.
	"""
	_setup_logging(False)
	load_dotenv()

	from .youtube import ENV_CLIENT_ID, ENV_CLIENT_SECRET, SCOPE

	try:
		from google_auth_oauthlib.flow import InstalledAppFlow
	except ImportError:
		# The escape matters: rich would otherwise read [youtube] as markup and
		# print an install command that does not install anything.
		console.print(
			r"[red]This needs the google client libraries:[/red] uv pip install -e '.\[youtube]'"
		)
		raise typer.Exit(2) from None

	if client_secrets:
		if not client_secrets.exists():
			console.print(f"[red]No client secrets file at {client_secrets}[/red]")
			raise typer.Exit(2)
		flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=[SCOPE])
	else:
		import os

		cid, secret = os.environ.get(ENV_CLIENT_ID), os.environ.get(ENV_CLIENT_SECRET)
		if not cid or not secret:
			console.print(
				f"[red]Set {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} in .env, or pass "
				f"--client-secrets.[/red]"
			)
			raise typer.Exit(2)
		flow = InstalledAppFlow.from_client_config(
			{
				"installed": {
					"client_id": cid,
					"client_secret": secret,
					"auth_uri": "https://accounts.google.com/o/oauth2/auth",
					"token_uri": "https://oauth2.googleapis.com/token",
					"redirect_uris": ["http://localhost"],
				}
			},
			scopes=[SCOPE],
		)

	console.print(f"Requesting the [cyan]{SCOPE}[/cyan] scope. A browser window will open.")
	# access_type=offline + prompt=consent is what actually returns a refresh
	# token; without the prompt Google reuses a prior grant and omits it.
	creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")

	if not creds.refresh_token:
		console.print(
			"[yellow]Google returned no refresh token. Revoke this app's access at "
			"https://myaccount.google.com/permissions and try again.[/yellow]"
		)
		raise typer.Exit(1)

	console.print("\n[green]Add this to .env (and to your CI secrets):[/green]")
	console.print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
	console.print(
		"\n[dim]Uploads stay locked to private until the Google Cloud project passes the "
		"YouTube API compliance audit.[/dim]"
	)


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
