"""LaTeX parsing tests.

Two layers:

  - Unit tests over hand-written snippets, each pinning one messiness pattern.
  - Integration tests over `tests/fixtures/eprints/*.tar.gz`, which hold the
    real .tex files from three published papers (figure binaries replaced with
    stubs to keep them small). Every expectation below was verified against the
    genuine sources first; these are regression tests, not guesses.

The three fixture papers were chosen for how differently they are put together:

  2505.09388  root is `colm2024_conference.tex`, not main.tex; one \\input is
              commented out and must NOT be followed
  2506.15841  conventional main.tex + latex/ subdir; \\textcolor in captions
  2507.06261  every section pulled in via two-argument \\subimport
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from pipeline.latex import (
	clean_body,
	clean_caption,
	extract_figures,
	extract_sections,
	flatten_includes,
	match_brace,
	parse_source,
	resolve_graphics_file,
	strip_comments,
	unescape,
)

FIXTURES = Path(__file__).parent / "fixtures" / "eprints"


# --- brace matching ----------------------------------------------------------


def test_match_brace_handles_nesting():
	text = r"\caption{outer \textbf{inner} tail}rest"
	content, end = match_brace(text, text.index("{"))
	assert content == r"outer \textbf{inner} tail"
	assert text[end:] == "rest"


def test_match_brace_ignores_escaped_braces():
	text = r"{a \{ b \} c}"
	content, _ = match_brace(text, 0)
	assert content == r"a \{ b \} c"


def test_match_brace_unterminated_returns_remainder():
	# A truncated tarball should degrade, not raise.
	content, end = match_brace("{never closed", 0)
	assert content == "never closed"
	assert end == len("{never closed")


# --- comments ----------------------------------------------------------------


def test_strip_comments_removes_line_comments():
	assert strip_comments("keep % drop\nnext") == "keep \nnext"


def test_strip_comments_preserves_escaped_percent():
	assert strip_comments(r"98\% accurate % but drop this") == r"98\% accurate "


def test_commented_out_figure_is_not_parsed():
	"""A commented `\\begin{figure}` must not open a phantom environment."""
	text = strip_comments(
		"% \\begin{figure}\n"
		"% \\includegraphics{ghost.pdf}\n"
		"% \\end{figure}\n"
		"\\begin{figure}\\includegraphics{real.pdf}\\caption{Real}\\end{figure}"
	)
	figs = extract_figures(text)
	assert len(figs) == 1
	assert figs[0].graphics == ["real.pdf"]


# --- captions ----------------------------------------------------------------


def test_clean_caption_unwraps_nested_formatting():
	raw = r"\textbf{Comparison of \textbf{bold} and \underline{underlined} scores}"
	assert clean_caption(raw) == "Comparison of bold and underlined scores"


def test_clean_caption_drops_colour_argument():
	"""`\\textcolor{red}{x}` must not leak its colour into the prose.

	Regression: this produced "redexisting" on a real paper.
	"""
	assert clean_caption(r"the \textcolor{red}{existing} agent") == "the existing agent"


def test_clean_caption_strips_labels_and_citations():
	raw = r"Results\label{fig:x} from \cite{smith2024} on ImageNet"
	assert "label" not in clean_caption(raw)
	assert "smith2024" not in clean_caption(raw)


def test_href_keeps_anchor_text_drops_url():
	"""`\\href{URL}{anchor}` must not run the URL into the prose.

	Regression: a real paper produced
	"...showPreview=truetake a video of a lecture".
	"""
	out = clean_caption(r"see \href{https://x.com/a?b=true}{take a video} here")
	assert out == "see take a video here"
	assert "x.com" not in out


def test_href_in_body_keeps_anchor_text():
	out = clean_body(r"visit \href{https://example.com/path?q=1}{the demo page} now")
	assert "example.com" not in out
	assert "the demo page" in out


def test_citation_removal_does_not_leave_space_before_punctuation():
	"""Stripping \\cite{} used to leave "robustness to noise ."."""
	assert clean_body(r"improves robustness to noise \cite{smith2024}.") == (
		"improves robustness to noise."
	)


def test_unescape_restores_literals():
	assert unescape(r"lp\_ship12l \& 98\%") == "lp_ship12l & 98%"


def test_longest_caption_wins_over_subfigure_panels():
	block = (
		r"\begin{subfigure}\caption{(a)}\end{subfigure}"
		r"\includegraphics{x.pdf}"
		r"\caption{The real caption describing the whole figure}"
	)
	text = r"\begin{figure}" + block + r"\end{figure}"
	figs = extract_figures(text)
	assert figs[0].caption == "The real caption describing the whole figure"


# --- figures -----------------------------------------------------------------


def test_inline_logos_outside_figure_env_are_excluded():
	"""Title-block logos are `\\includegraphics` too, and must not be figures."""
	text = (
		r"\includegraphics[height=24pt]{logo/qwen-logo.pdf}"
		r"\begin{figure}\includegraphics{figures/real.pdf}\caption{A}\end{figure}"
	)
	figs = extract_figures(text)
	assert len(figs) == 1
	assert figs[0].graphics == ["figures/real.pdf"]


def test_starred_figure_env_is_found():
	text = r"\begin{figure*}\includegraphics{wide.pdf}\caption{Wide}\end{figure*}"
	figs = extract_figures(text)
	assert len(figs) == 1 and figs[0].env == "figure*"


def test_figure_without_graphics_is_skipped():
	# A tikz-only figure has a caption but no file we could show.
	text = r"\begin{figure}\begin{tikzpicture}\end{tikzpicture}\caption{Drawn}\end{figure}"
	assert extract_figures(text) == []


def test_figures_are_numbered_in_document_order():
	text = (
		r"\begin{figure}\includegraphics{a.pdf}\caption{A}\end{figure}"
		r"\begin{figure}\includegraphics{b.pdf}\caption{B}\end{figure}"
	)
	figs = extract_figures(text)
	assert [f.number for f in figs] == [1, 2]
	assert [f.caption for f in figs] == ["A", "B"]


# --- graphics resolution -----------------------------------------------------


def test_resolve_graphics_without_extension(tmp_path: Path):
	(tmp_path / "figures").mkdir()
	target = tmp_path / "figures" / "plot.pdf"
	target.write_bytes(b"%PDF")
	assert resolve_graphics_file("figures/plot", tmp_path) == target


def test_resolve_graphics_with_wrong_extension(tmp_path: Path):
	"""Sources often say .eps while shipping .pdf."""
	(tmp_path / "f").mkdir()
	target = tmp_path / "f" / "chart.pdf"
	target.write_bytes(b"%PDF")
	assert resolve_graphics_file("f/chart.eps", tmp_path) == target


def test_resolve_graphics_falls_back_to_basename(tmp_path: Path):
	nested = tmp_path / "deep" / "nested"
	nested.mkdir(parents=True)
	target = nested / "moved.png"
	target.write_bytes(b"\x89PNG")
	assert resolve_graphics_file("old/path/moved.png", tmp_path) == target


def test_resolve_graphics_missing_returns_none(tmp_path: Path):
	assert resolve_graphics_file("nope.pdf", tmp_path) is None


# --- includes ----------------------------------------------------------------


def test_flatten_follows_input(tmp_path: Path):
	(tmp_path / "main.tex").write_text(r"\begin{document}\input{body}\end{document}")
	(tmp_path / "body.tex").write_text("BODY TEXT")
	assert "BODY TEXT" in flatten_includes(tmp_path / "main.tex", tmp_path)


def test_flatten_follows_two_argument_subimport(tmp_path: Path):
	"""`\\subimport{dir}{file}` - the form that made a real paper parse as empty."""
	(tmp_path / "sec").mkdir()
	(tmp_path / "main.tex").write_text(r"\subimport{sec}{intro}")
	(tmp_path / "sec" / "intro.tex").write_text("INTRO TEXT")
	assert "INTRO TEXT" in flatten_includes(tmp_path / "main.tex", tmp_path)


def test_flatten_survives_include_cycle(tmp_path: Path):
	(tmp_path / "a.tex").write_text(r"A \input{b}")
	(tmp_path / "b.tex").write_text(r"B \input{a}")
	out = flatten_includes(tmp_path / "a.tex", tmp_path)
	assert "A" in out and "B" in out  # terminates rather than recursing forever


def test_flatten_tolerates_missing_include(tmp_path: Path):
	(tmp_path / "main.tex").write_text(r"KEEP \input{gone} TAIL")
	out = flatten_includes(tmp_path / "main.tex", tmp_path)
	assert "KEEP" in out and "TAIL" in out


# --- sections ----------------------------------------------------------------


def test_extract_sections_splits_on_headings():
	text = r"\section{Introduction}intro body\section{Method}method body"
	secs = extract_sections(text)
	assert "intro body" in secs["Introduction"]
	assert "method body" in secs["Method"]
	assert "method body" not in secs["Introduction"]


def test_clean_body_drops_float_environments():
	body = r"prose \begin{table}\begin{tabular}1&2\\3&4\end{tabular}\end{table} more prose"
	out = clean_body(body)
	assert "prose" in out and "more prose" in out
	assert "tabular" not in out


# --- real-source integration -------------------------------------------------


def _extract(pid: str, tmp_path: Path) -> Path:
	dest = tmp_path / pid
	with tarfile.open(FIXTURES / f"{pid}.tar.gz") as tf:
		tf.extractall(dest, filter="data")
	return dest


@pytest.mark.parametrize(
	("pid", "root_name", "min_figures", "wants_intro", "wants_conclusion"),
	[
		("2505.09388", "colm2024_conference.tex", 2, True, True),
		("2506.15841", "main.tex", 7, True, True),
		("2507.06261", "main.tex", 17, True, True),
	],
)
def test_real_sources_parse(
	pid: str,
	root_name: str,
	min_figures: int,
	wants_intro: bool,
	wants_conclusion: bool,
	tmp_path: Path,
):
	parsed = parse_source(_extract(pid, tmp_path))
	assert parsed.root is not None and parsed.root.name == root_name
	assert len(parsed.figures) >= min_figures
	assert bool(parsed.intro) is wants_intro
	assert bool(parsed.conclusion) is wants_conclusion
	# Every figure we report must carry a caption; a caption-less figure is
	# useless to Stages 5-6, which cite it.
	assert all(f.caption for f in parsed.figures)


def test_real_source_ignores_commented_include(tmp_path: Path):
	"""2505.09388 has `%\\input{content/experiments.tex}`.

	Its figures must not appear: the submitted paper does not contain them.
	"""
	root = _extract("2505.09388", tmp_path)
	parsed = parse_source(root)
	refs = " ".join(g for f in parsed.figures for g in f.graphics)
	assert "passkey_retrieval" not in refs
	assert "inference_speed" not in refs
	assert len(parsed.figures) == 2


def test_real_source_subimport_paper_finds_body(tmp_path: Path):
	"""2507.06261 reaches all its content only through \\subimport."""
	parsed = parse_source(_extract("2507.06261", tmp_path))
	assert len(parsed.sections) > 20
	assert "Introduction" in parsed.sections


def test_real_source_graphics_all_resolve(tmp_path: Path):
	"""Every figure reference in the fixtures must resolve to a real file."""
	for pid in ("2505.09388", "2506.15841", "2507.06261"):
		root = _extract(pid, tmp_path / pid)
		parsed = parse_source(root)
		unresolved = [
			f.graphics[0]
			for f in parsed.figures
			if f.graphics and resolve_graphics_file(f.graphics[0], root) is None
		]
		assert not unresolved, f"{pid}: unresolved {unresolved}"
