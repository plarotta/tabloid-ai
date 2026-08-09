"""Stage 3 - pull LaTeX source, figures, captions and section text per paper.

No LLM calls, so this stage is free and can be iterated on without spending.
That is deliberate: it is the fiddliest stage in the pipeline, and being able to
re-run it against real papers at zero cost is what makes it tractable.

Papers are enriched concurrently but politely - arXiv is a free service, and the
delay between requests is per-worker.

Failure policy: one paper failing to enrich must not stop the run, because Stage
4 only needs enough good candidates to pick 3 from ~15. A paper that fails is
recorded in `failed` and excluded. The stage raises only if too few papers
survive to make a credible ranking.
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .. import figures as figlib
from .. import signals as siglib
from ..eprint import EPrintError, EPrintFetcher
from ..latex import parse_source, resolve_graphics_file
from ..paths import read_json, write_json
from ..schemas import (
	EnrichedPaper,
	EnrichResult,
	FigureAsset,
	Paper,
	PaperSections,
	ShortlistEntry,
	SignalsModel,
)
from ..stage import Stage, StageError

log = logging.getLogger(__name__)

# Section text is sent to Stage 4 for every candidate; the tail of a long
# introduction rarely changes a ranking and the tokens add up across ~15 papers.
MAX_SECTION_CHARS = 6000


class EnrichStage(Stage):
	name = "enrich"

	def paper_dir(self, arxiv_id: str) -> Path:
		return self.paths.enriched_dir / arxiv_id.replace("/", "_")

	def is_complete(self) -> bool:
		return (self.paths.enriched_dir / "enrich.json").exists()

	def load(self) -> EnrichResult:
		return EnrichResult.model_validate(read_json(self.paths.enriched_dir / "enrich.json"))

	# --- per-paper work ------------------------------------------------------

	def _collect_figures(
		self, parsed, source_dir: Path, fig_dir: Path, notes: list[str]
	) -> list[FigureAsset]:
		"""Resolve, normalise and copy every figure into the paper's figures/."""
		search = []
		assets: list[FigureAsset] = []
		unconverted = 0

		for ref in parsed.figures:
			# A figure environment can hold several panels; the first is the one
			# that represents it, and extra panels are rarely usable standalone.
			primary = ref.graphics[0] if ref.graphics else ""
			src = resolve_graphics_file(primary, source_dir, search) if primary else None
			asset = FigureAsset(
				number=ref.number,
				original_ref=primary,
				caption=ref.caption,
				label=ref.label,
				source_format=(src.suffix.lower().lstrip(".") if src else ""),
				converted=False,
				file=None,
			)
			if src is None:
				log.debug("Figure %s: unresolved reference %r", ref.number, primary)
				assets.append(asset)
				unconverted += 1
				continue

			written = figlib.normalise(src, fig_dir, f"fig{ref.number:02d}")
			if written is not None:
				asset.file = f"figures/{written.name}"
				asset.converted = True
			else:
				unconverted += 1
				log.debug("Figure %s (%s) could not be rasterised", ref.number, src.name)
			assets.append(asset)

		if unconverted:
			notes.append(f"{unconverted} of {len(parsed.figures)} figures could not be rasterised")
		return assets

	def _from_pdf(
		self, arxiv_id: str, work_dir: Path, fig_dir: Path, notes: list[str]
	) -> list[FigureAsset]:
		"""Fallback when there is no LaTeX source: pull embedded images from the
		compiled PDF. No captions are available, which is why this is marked as
		lower quality."""
		fetcher = EPrintFetcher(delay_seconds=self.config.enrich.request_delay_seconds)
		try:
			pdf = fetcher.fetch_pdf(arxiv_id, work_dir / f"{arxiv_id}.pdf")
		except EPrintError as e:
			notes.append(f"PDF fallback failed: {e}")
			return []
		finally:
			fetcher.close()

		raw = figlib.extract_pdf_figures(pdf, work_dir / "pdf_figures")
		assets = []
		for i, path in enumerate(raw[: self.config.enrich.max_figures], start=1):
			written = figlib.normalise(path, fig_dir, f"fig{i:02d}")
			if written is not None:
				assets.append(
					FigureAsset(
						number=i,
						file=f"figures/{written.name}",
						original_ref=path.name,
						caption="",  # no captions available from this route
						converted=True,
						source_format=path.suffix.lower().lstrip("."),
					)
				)
		notes.append("No LaTeX source; figures extracted from PDF without captions")
		return assets

	def _enrich_one(self, entry: ShortlistEntry, http: httpx.Client) -> EnrichedPaper | None:
		paper = entry.paper
		out_dir = self.paper_dir(paper.arxiv_id)
		fig_dir = out_dir / "figures"
		work_dir = out_dir / "_work"
		notes: list[str] = []

		# A previous run may have left a partial directory behind.
		if fig_dir.exists():
			shutil.rmtree(fig_dir, ignore_errors=True)
		fig_dir.mkdir(parents=True, exist_ok=True)

		quality = "none"
		assets: list[FigureAsset] = []
		sections = PaperSections(title=paper.title, abstract=paper.abstract)

		fetcher = EPrintFetcher(delay_seconds=self.config.enrich.request_delay_seconds)
		try:
			source_dir, kind = fetcher.fetch_source(paper.arxiv_id, work_dir)
		except EPrintError as e:
			log.warning("%s: no e-print source (%s)", paper.arxiv_id, e)
			notes.append(f"e-print download failed: {e}")
			source_dir, kind = None, "none"
		finally:
			fetcher.close()

		if source_dir is not None and kind in {"tar", "tex"}:
			parsed = parse_source(source_dir)
			if parsed.root is None:
				notes.append("Archive contained no usable .tex root")
			else:
				quality = "latex"
				assets = self._collect_figures(parsed, source_dir, fig_dir, notes)
				sections = PaperSections(
					# Prefer the arXiv metadata title/abstract: they are clean text
					# already, where the LaTeX versions carry markup.
					title=paper.title or parsed.title,
					abstract=paper.abstract or parsed.abstract,
					intro=parsed.intro[:MAX_SECTION_CHARS],
					conclusion=parsed.conclusion[:MAX_SECTION_CHARS],
					section_titles=parsed.sections,
				)
				if not parsed.intro:
					notes.append("No introduction section found")
				if not parsed.conclusion:
					notes.append("No conclusion section found")
				# Stage 5 needs the whole paper and the e-print source is deleted
				# below, so this is the only chance to capture it.
				if parsed.body:
					(out_dir / "fulltext.txt").write_text(parsed.body, encoding="utf-8")
				else:
					notes.append("No full text extracted; Stage 5 will be abstract-only")

		if quality == "none" or not any(a.converted for a in assets):
			pdf_assets = self._from_pdf(paper.arxiv_id, work_dir, fig_dir, notes)
			if pdf_assets:
				# Keep LaTeX captions if we have them; only the images come from
				# the PDF route.
				if quality == "latex" and assets:
					notes.append("LaTeX figures unusable; substituted PDF-extracted images")
				assets = pdf_assets if quality != "latex" else assets + pdf_assets
				quality = "pdf" if quality == "none" else quality

		if quality == "none" and not assets:
			log.warning("%s: enrichment produced nothing usable", paper.arxiv_id)
			return None

		sig = siglib.collect(
			paper.arxiv_id,
			abstract=paper.abstract,
			comment=paper.comment,
			intro=sections.intro,
			client=http,
		)

		enriched = EnrichedPaper(
			arxiv_id=paper.arxiv_id,
			paper=paper,
			source_quality=quality,
			figures=assets[: self.config.enrich.max_figures],
			sections=sections,
			signals=SignalsModel(
				github_url=sig.github_url,
				project_page=sig.project_page,
				huggingface=sig.huggingface,
				hf_upvotes=sig.hf_upvotes,
				hf_lookup_ok=sig.hf_lookup_ok,
			),
			notes=notes,
		)

		# Per-paper artifacts, as the spec lays them out.
		write_json(
			out_dir / "meta.json",
			json.loads(enriched.model_dump_json(exclude={"sections"})),
		)
		write_json(out_dir / "sections.json", json.loads(sections.model_dump_json()))
		write_json(out_dir / "signals.json", json.loads(enriched.signals.model_dump_json()))
		write_json(
			fig_dir / "captions.json",
			[json.loads(a.model_dump_json()) for a in enriched.figures],
		)

		if not self.config.enrich.keep_source:
			shutil.rmtree(work_dir, ignore_errors=True)

		log.info(
			"%s: %s source, %s/%s figures usable%s",
			paper.arxiv_id,
			quality,
			len(enriched.usable_figures),
			len(enriched.figures),
			f", {len(notes)} note(s)" if notes else "",
		)
		return enriched

	# --- stage ---------------------------------------------------------------

	def run(self) -> EnrichResult:
		from .shortlist import ShortlistStage

		shortlist = ShortlistStage(self.ctx).load().shortlist
		if self.ctx.paper_filter:
			shortlist = [e for e in shortlist if e.paper.arxiv_id == self.ctx.paper_filter]
			if not shortlist:
				raise StageError(f"Paper {self.ctx.paper_filter!r} is not in the shortlist.")

		cfg = self.config.enrich
		log.info("Enriching %s papers (concurrency %s)", len(shortlist), cfg.max_concurrency)

		http = httpx.Client(timeout=20.0, headers={"User-Agent": siglib.USER_AGENT})
		results: list[EnrichedPaper | None] = []
		try:
			with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
				futures = [pool.submit(self._enrich_one, e, http) for e in shortlist]
				for entry, fut in zip(shortlist, futures, strict=True):
					try:
						results.append(fut.result())
					except Exception as e:
						log.warning("%s: enrichment raised (%s)", entry.paper.arxiv_id, e)
						results.append(None)
		finally:
			http.close()

		papers = [r for r in results if r is not None]
		failed = [e.paper.arxiv_id for e, r in zip(shortlist, results, strict=True) if r is None]

		if len(papers) < cfg.min_enriched:
			raise StageError(
				f"Only {len(papers)} of {len(shortlist)} papers enriched, below "
				f"min_enriched={cfg.min_enriched}. Ranking would have too few "
				f"candidates. Failed: {', '.join(failed) or 'none'}"
			)
		if failed:
			log.warning("%s paper(s) failed to enrich: %s", len(failed), ", ".join(failed))

		result = EnrichResult(generated_at=datetime.now(UTC), papers=papers, failed=failed)
		write_json(self.paths.enriched_dir / "enrich.json", json.loads(result.model_dump_json()))
		with_figs = sum(1 for p in papers if p.usable_figures)
		log.info("Enriched %s papers; %s have at least one usable figure", len(papers), with_figs)
		return result


__all__ = ["EnrichStage", "Paper"]
