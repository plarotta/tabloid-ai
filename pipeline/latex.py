"""LaTeX source parsing: figures, captions, and section text.

The spec calls this the fiddliest part of the pipeline, and probing real arXiv
e-prints confirmed it. Three findings from real papers drive the design here:

1. **Multi-file is the norm.** All three sample papers split their sections into
   separate files pulled in with `\\input{}`. A parser that reads only the root
   file finds almost none of the figures or section text, so we flatten the
   include graph before parsing anything.

2. **The root file is not reliably `main.tex`.** One sample used
   `colm2024_conference.tex`. We locate the root by looking for
   `\\documentclass` + `\\begin{document}` rather than trusting a filename.

3. **Captions nest braces heavily.** A real caption from the sample set:
   `\\caption{\\textbf{Comparison ... shown in \\textbf{bold} and \\underline{...}}}`
   A `\\{[^}]*\\}` regex truncates this at the first inner `}`. Every brace group
   here is matched by counting depth, never by regex.

A fourth point is structural: `\\includegraphics` also appears *outside* figure
environments for inline logos (`\\includegraphics[height=24pt]{logo/qwen.pdf}`).
Scoping extraction to `\\begin{figure}` blocks drops those without a filter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# figure/figure*/wrapfigure/SCfigure all appeared or are common enough to expect.
FIGURE_ENVS = ("figure", "figure*", "wrapfigure", "SCfigure", "subfigure")

# Extensions arXiv sources actually ship, in the order we prefer them when an
# \includegraphics path omits its extension (TeX resolves these by search order).
GRAPHICS_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".gif")

# `\import`/`\subimport` take TWO arguments (`{dir}{file}`) and were found in a
# real sample (the Gemini 2.5 report pulls in every section that way). Matching
# only the single-argument forms silently loses the entire body of such papers.
_INCLUDE_RE = re.compile(r"\\(input|include|subfile|subimport\*?|import\*?)\s*(?=\{)")
_TWO_ARG_INCLUDES = {"import", "import*", "subimport", "subimport*"}
_GRAPHICS_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{")

MAX_INCLUDE_DEPTH = 8


def strip_comments(text: str) -> str:
	"""Remove LaTeX line comments, preserving escaped `\\%`.

	Done before any structural parsing so a commented-out `\\begin{figure}` cannot
	open a phantom environment that swallows the rest of the document.
	"""
	out = []
	for line in text.splitlines():
		result = []
		i = 0
		while i < len(line):
			ch = line[i]
			if ch == "\\" and i + 1 < len(line):
				# Keep any escaped char as-is; this is what protects \%.
				result.append(line[i : i + 2])
				i += 2
				continue
			if ch == "%":
				break
			result.append(ch)
			i += 1
		out.append("".join(result))
	return "\n".join(out)


def match_brace(text: str, open_index: int) -> tuple[str, int]:
	"""Return (contents, index_after_close) for the brace group at `open_index`.

	Counts depth so nested groups survive intact. Escaped braces do not change
	depth. If the group never closes, returns everything to the end of the text -
	a truncated tarball should degrade, not crash.
	"""
	if open_index >= len(text) or text[open_index] != "{":
		raise ValueError(f"No opening brace at index {open_index}")
	depth = 0
	i = open_index
	while i < len(text):
		ch = text[i]
		if ch == "\\" and i + 1 < len(text):
			i += 2
			continue
		if ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0:
				return text[open_index + 1 : i], i + 1
		i += 1
	return text[open_index + 1 :], len(text)


def find_root_tex(source_dir: Path) -> Path | None:
	"""Locate the document root.

	Preference order: a file with both `\\documentclass` and `\\begin{document}`,
	then any file with `\\begin{document}`, breaking ties toward a `main`-ish name
	and then by size. Filename alone is not trusted - a real sample root was
	`colm2024_conference.tex`.
	"""
	candidates: list[tuple[int, int, int, Path]] = []
	for path in sorted(source_dir.rglob("*.tex")):
		try:
			text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
		except OSError:
			continue
		has_class = "\\documentclass" in text
		has_begin = "\\begin{document}" in text
		if not has_begin:
			continue
		# Lower sorts first: prefer documentclass, then a main-like name.
		rank = 0 if has_class else 1
		name_rank = 0 if path.stem.lower() in {"main", "paper", "ms", "arxiv"} else 1
		candidates.append((rank, name_rank, -path.stat().st_size, path))

	if not candidates:
		# Some submissions are a bare fragment with no \begin{document}.
		texs = sorted(source_dir.rglob("*.tex"), key=lambda p: -p.stat().st_size)
		return texs[0] if texs else None
	candidates.sort()
	return candidates[0][3]


def flatten_includes(
	root: Path, source_dir: Path, _depth: int = 0, _seen: set | None = None
) -> str:
	"""Inline `\\input{}` / `\\include{}` recursively, returning one document.

	Cycle- and depth-guarded: a self-including file would otherwise loop forever.
	A missing include is left as an empty string rather than failing the paper -
	unresolvable includes are usually macro files we do not need.
	"""
	_seen = _seen if _seen is not None else set()
	try:
		resolved = root.resolve()
	except OSError:
		return ""
	if resolved in _seen or _depth > MAX_INCLUDE_DEPTH:
		return ""
	_seen.add(resolved)

	try:
		text = strip_comments(root.read_text(encoding="utf-8", errors="replace"))
	except OSError:
		return ""

	def resolve(name: str) -> Path | None:
		name = name.strip().lstrip("./")
		if not name:
			return None
		for cand in (name, f"{name}.tex"):
			# Include paths are relative to the root file, then to the source root.
			for base in (root.parent, source_dir):
				p = base / cand
				if p.is_file():
					return p
		return None

	out = []
	pos = 0
	for m in _INCLUDE_RE.finditer(text):
		if m.start() < pos:  # inside an argument already consumed
			continue
		cmd = m.group(1)
		try:
			first, after = match_brace(text, m.end())
		except ValueError:
			continue

		if cmd in _TWO_ARG_INCLUDES:
			# \subimport{dir}{file} -> dir/file
			try:
				second, after = match_brace(text, after)
			except ValueError:
				continue
			name = f"{first.strip().rstrip('/')}/{second.strip()}"
		else:
			name = first

		out.append(text[pos : m.start()])
		target = resolve(name)
		if target is not None:
			out.append(flatten_includes(target, source_dir, _depth + 1, _seen))
		else:
			log.debug("Unresolved include %r in %s", name, root.name)
		pos = after
	out.append(text[pos:])
	return "".join(out)


def graphics_paths(text: str) -> list[str]:
	"""Parse `\\graphicspath{{dir1/}{dir2/}}` into a list of directories."""
	m = _GRAPHICSPATH_RE.search(text)
	if not m:
		return []
	group, _ = match_brace(text, m.end() - 1)
	return [d.strip() for d in re.findall(r"\{([^{}]*)\}", group) if d.strip()]


def clean_caption(raw: str) -> str:
	"""Turn caption LaTeX into readable prose.

	Captions are the single highest-value text in the enrichment output - Stage 4
	ranks partly on them and Stage 5 cites them - so this unwraps formatting
	commands rather than deleting their contents.
	"""
	text = raw
	# Two-argument commands whose FIRST argument is not prose. Handled before the
	# generic control-sequence strip, which would otherwise leave the discarded
	# argument glued to the text: `\textcolor{red}{existing}` -> "redexisting".
	text = _strip_first_arg(text)

	# Drop commands that carry no reader-visible text, with their arguments.
	for cmd in ("label", "vspace", "hspace", "centering", "small", "footnotesize", "captionsetup"):
		text = re.sub(r"\\" + cmd + r"\s*\{[^{}]*\}", "", text)
		text = re.sub(r"\\" + cmd + r"\b", "", text)

	# Unwrap formatting commands, keeping the text inside. Repeated because these
	# nest: \textbf{a \underline{b}}.
	keep = (
		"textbf|textit|emph|texttt|textsc|textrm|mathrm|mathbf|text|underline|"
		"uline|url|footnotesize|small|large"
	)
	for _ in range(6):
		new = re.sub(r"\\(?:" + keep + r")\s*\{", "{", text)
		if new == text:
			break
		text = new

	text = text.replace("\\\\", " ")
	text = re.sub(r"\\(?:cite|citep|citet|ref|autoref|cref|Cref)\s*\{[^{}]*\}", "", text)
	text = re.sub(r"\\[a-zA-Z@]+\s*", " ", text)  # any remaining control sequences
	text = text.replace("{", "").replace("}", "")
	text = text.replace("~", " ").replace("$", "")
	text = unescape(text)
	# Removing \cite{...} leaves the space that preceded it: "to noise ." Tidy it,
	# so the text handed to Stages 4-5 reads as prose rather than as output.
	text = re.sub(r"\s+([.,;:!?)\]])", r"\1", text)
	text = re.sub(r"([(\[])\s+", r"\1", text)
	text = re.sub(r"\s+", " ", text)
	return text.strip(" .,;:").strip()


_ESCAPES = {
	r"\_": "_",
	r"\&": "&",
	r"\%": "%",
	r"\#": "#",
	r"\$": "$",
	r"\{": "{",
	r"\}": "}",
}


def unescape(text: str) -> str:
	"""Restore LaTeX-escaped literals (`lp\\_ship12l` -> `lp_ship12l`).

	Runs last, after brace stripping: unescaping `\\{` any earlier would produce a
	brace that the structural cleanup then deletes along with its contents.
	"""
	for src, dst in _ESCAPES.items():
		text = text.replace(src, dst)
	return text


def _strip_first_arg(text: str) -> str:
	"""Rewrite `\\cmd{arg1}{arg2}` -> `{arg2}` for commands whose first argument is
	a colour or span count rather than prose."""
	pattern = re.compile(
		# `href` belongs here, not in the unwrap list: `\href{URL}{anchor}` must
		# keep the anchor and drop the URL, or the two run together as
		# "...showPreview=truetake a video of a lecture" (seen on a real paper).
		r"\\(?:textcolor|colorbox|multicolumn|multirow|href)\s*\{[^{}]*\}\s*"
		r"(?:\{[^{}]*\}\s*)?(?=\{)"
	)
	for _ in range(6):
		new = pattern.sub("", text)
		if new == text:
			break
		text = new
	return text


@dataclass(slots=True)
class FigureRef:
	"""One figure environment: the graphics it includes and its caption."""

	env: str
	graphics: list[str] = field(default_factory=list)
	caption: str = ""
	label: str = ""
	order: int = 0

	@property
	def number(self) -> int:
		"""1-based position in the document, which is what LaTeX numbering yields
		for non-starred figures and what a caption's "Figure N" refers to."""
		return self.order + 1


def _find_env_blocks(text: str, env: str) -> list[str]:
	"""Return the body of each `\\begin{env}...\\end{env}`, nesting-aware."""
	blocks = []
	begin = f"\\begin{{{env}}}"
	end = f"\\end{{{env}}}"
	pos = 0
	while True:
		start = text.find(begin, pos)
		if start == -1:
			return blocks
		body_start = start + len(begin)
		depth = 1
		i = body_start
		while i < len(text) and depth > 0:
			nb = text.find(begin, i)
			ne = text.find(end, i)
			if ne == -1:
				break
			if nb != -1 and nb < ne:
				depth += 1
				i = nb + len(begin)
			else:
				depth -= 1
				i = ne + len(end)
				if depth == 0:
					blocks.append(text[body_start:ne])
		pos = i if i > start else start + len(begin)


def extract_caption(block: str) -> str:
	"""Longest `\\caption{...}` in a block.

	Longest rather than first because subfigure blocks carry short per-panel
	captions ("(a)") alongside the real figure caption, and the informative one is
	essentially always the longer.
	"""
	best = ""
	for m in re.finditer(r"\\caption(?:of\s*\{[^{}]*\})?\s*(?:\[[^\]]*\])?\s*\{", block):
		try:
			raw, _ = match_brace(block, m.end() - 1)
		except ValueError:
			continue
		cleaned = clean_caption(raw)
		if len(cleaned) > len(best):
			best = cleaned
	return best


def extract_graphics(block: str) -> list[str]:
	"""Every `\\includegraphics` target in a block, in order."""
	out = []
	for m in _GRAPHICS_RE.finditer(block):
		try:
			path, _ = match_brace(block, m.end() - 1)
		except ValueError:
			continue
		path = path.strip()
		# Paths are occasionally wrapped in a macro or carry a leading ./
		path = re.sub(r"^\./", "", path)
		if path and "#" not in path:  # #1 means we are inside a macro definition
			out.append(path)
	return out


def extract_figures(text: str) -> list[FigureRef]:
	"""All figure environments in document order.

	Only `\\includegraphics` inside a figure environment is collected, which is
	what keeps inline title-block logos out of the figure inventory.
	"""
	found: list[tuple[int, FigureRef]] = []
	for env in FIGURE_ENVS:
		if env == "subfigure":
			continue  # handled inside its parent figure block
		begin = f"\\begin{{{env}}}"
		for block in _find_env_blocks(text, env):
			graphics = extract_graphics(block)
			if not graphics:
				continue
			caption = extract_caption(block)
			label_m = re.search(r"\\label\s*\{([^}]*)\}", block)
			pos = text.find(begin + block) if block else -1
			found.append(
				(
					pos if pos >= 0 else len(found),
					FigureRef(
						env=env,
						graphics=graphics,
						caption=caption,
						label=label_m.group(1) if label_m else "",
					),
				)
			)
	found.sort(key=lambda t: t[0])
	figures = []
	for i, (_, fig) in enumerate(found):
		fig.order = i
		figures.append(fig)
	return figures


def resolve_graphics_file(
	ref: str, source_dir: Path, search_dirs: list[str] | None = None
) -> Path | None:
	"""Find the file an `\\includegraphics` reference points at.

	Handles the two forms real papers use: a path with an extension, and a path
	without one that TeX resolves by trying known extensions. Falls back to a
	basename search because some sources reference files that have been moved.
	"""
	ref = ref.strip().lstrip("./")
	if not ref:
		return None

	bases = [source_dir]
	for d in search_dirs or []:
		bases.append(source_dir / d.strip("/"))

	for base in bases:
		direct = base / ref
		if direct.is_file():
			return direct
		# Extension omitted in the source.
		for ext in GRAPHICS_EXTS:
			cand = base / (ref + ext)
			if cand.is_file():
				return cand
		# Extension present but the on-disk file uses a different one.
		stem = Path(ref)
		for ext in GRAPHICS_EXTS:
			cand = base / stem.with_suffix(ext)
			if cand.is_file():
				return cand

	# Last resort: match on basename anywhere in the tree.
	target = Path(ref).name
	target_stem = Path(target).stem
	for path in source_dir.rglob("*"):
		if not path.is_file():
			continue
		if path.name == target or (
			path.stem == target_stem and path.suffix.lower() in GRAPHICS_EXTS
		):
			return path
	return None


# --- Section text -----------------------------------------------------------

_SECTION_RE = re.compile(r"\\(?:sub)*section\*?\s*(?:\[[^\]]*\])?\s*\{")

INTRO_PAT = re.compile(r"^\s*\d*\.?\s*introduction\b", re.IGNORECASE)
CONCLUSION_PAT = re.compile(
	r"^\s*\d*\.?\s*(conclusion|conclusions|concluding remarks|"
	r"discussion and conclusion|summary)\b",
	re.IGNORECASE,
)
# Tried only when nothing matches CONCLUSION_PAT. Several strong papers (the
# Gemini 2.5 report among them) close with "Discussion" and have no section
# named Conclusion at all; taking it is much better than shipping no closing text.
CONCLUSION_FALLBACK_PAT = re.compile(
	r"^\s*\d*\.?\s*(discussion|limitations and future work|future work|outlook)\b",
	re.IGNORECASE,
)


def clean_body(raw: str) -> str:
	"""Reduce section body LaTeX to plain prose for an LLM to read.

	Floats are dropped rather than flattened: a table's cell-by-cell contents are
	noise once the caption is already captured separately, and they are a large
	share of the token count.
	"""
	text = _strip_first_arg(raw)
	floats = ("figure*", "figure", "table*", "table", "tabular", "align", "equation", "wrapfigure")
	for env in floats:
		text = re.sub(
			r"\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}",
			" ",
			text,
			flags=re.DOTALL,
		)
	text = re.sub(r"\\(?:cite|citep|citet|citeauthor)\s*(?:\[[^\]]*\])*\s*\{[^{}]*\}", "", text)
	text = re.sub(r"\\(?:ref|autoref|cref|Cref|eqref)\s*\{[^{}]*\}", "", text)
	text = re.sub(r"\\label\s*\{[^{}]*\}", "", text)
	text = re.sub(r"\\(?:footnote|thanks)\s*\{", " (", text)

	keep = "textbf|textit|emph|texttt|textsc|text|underline|mathrm|mathbf"
	for _ in range(6):
		new = re.sub(r"\\(?:" + keep + r")\s*\{", "{", text)
		if new == text:
			break
		text = new

	text = re.sub(r"\\begin\{[^{}]*\}|\\end\{[^{}]*\}", " ", text)
	text = re.sub(r"\\item\b", " - ", text)
	text = text.replace("\\\\", " ")
	text = re.sub(r"\\[a-zA-Z@]+\s*", " ", text)
	text = text.replace("{", "").replace("}", "").replace("~", " ")
	text = unescape(text)
	# Removing \cite{...} leaves the space that preceded it: "to noise ." Tidy it,
	# so the text handed to Stages 4-5 reads as prose rather than as output.
	text = re.sub(r"\s+([.,;:!?)\]])", r"\1", text)
	text = re.sub(r"([(\[])\s+", r"\1", text)
	text = re.sub(r"[ \t]+", " ", text)
	text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
	return text.strip()


def extract_sections(text: str) -> dict[str, str]:
	"""Split the document into `{heading: body}` at top-level section boundaries."""
	headings: list[tuple[str, int, int]] = []
	for m in _SECTION_RE.finditer(text):
		try:
			title, after = match_brace(text, m.end() - 1)
		except ValueError:
			continue
		headings.append((clean_caption(title), m.start(), after))

	sections: dict[str, str] = {}
	for i, (title, _, body_start) in enumerate(headings):
		body_end = headings[i + 1][1] if i + 1 < len(headings) else len(text)
		if title:
			sections[title] = text[body_start:body_end]
	return sections


def _first_match(sections: dict[str, str], pattern: re.Pattern) -> str:
	for title, body in sections.items():
		if pattern.match(title.strip()):
			return clean_body(body)
	return ""


@dataclass(slots=True)
class ParsedSource:
	root: Path | None
	figures: list[FigureRef]
	intro: str
	conclusion: str
	abstract: str
	title: str
	sections: list[str]
	# Whole document as prose, section headings inline. Stage 5 needs the full
	# paper to extract results faithfully, and the e-print source is deleted after
	# enrichment - so if it is not captured here it cannot be recovered without
	# re-downloading.
	body: str = ""


def parse_source(source_dir: Path) -> ParsedSource:
	"""Parse an extracted e-print directory into figures + section text."""
	root = find_root_tex(source_dir)
	if root is None:
		return ParsedSource(None, [], "", "", "", "", [])

	text = flatten_includes(root, source_dir)
	sections = extract_sections(text)

	abstract = ""
	for env in ("abstract",):
		blocks = _find_env_blocks(text, env)
		if blocks:
			abstract = clean_body(blocks[0])
			break

	title = ""
	tm = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", text)
	if tm:
		try:
			title = clean_caption(match_brace(text, tm.end() - 1)[0])
		except ValueError:
			title = ""

	# Rebuild the document as prose with its headings, in document order. Done
	# from the already-split sections so headings survive - Stage 5 cites results
	# by section, and needs to see where each one came from.
	body_parts = []
	for heading, raw in sections.items():
		cleaned = clean_body(raw)
		if cleaned:
			body_parts.append(f"## {heading}\n\n{cleaned}")
	body = "\n\n".join(body_parts)

	return ParsedSource(
		root=root,
		figures=extract_figures(text),
		intro=_first_match(sections, INTRO_PAT),
		conclusion=(
			_first_match(sections, CONCLUSION_PAT)
			or _first_match(sections, CONCLUSION_FALLBACK_PAT)
		),
		abstract=abstract,
		title=title,
		sections=list(sections),
		body=body,
	)
