"""Prompt loading.

Prompts live in `prompts/<stage>/v<N>.md`, never as inline strings, so a prompt
change is a reviewable diff. Each file has YAML front matter and is split into
`# System` and `# User` sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .paths import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_SECTION = re.compile(r"^#\s+(System|User)\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(slots=True)
class Prompt:
	name: str
	version: int
	system: str
	user: str
	meta: dict

	def render(self, **kwargs: object) -> tuple[str, str]:
		"""Substitute {{placeholders}} in both sections.

		Uses explicit replacement rather than str.format because prompts contain
		literal JSON braces that would otherwise need escaping.
		"""

		def sub(text: str) -> str:
			for key, value in kwargs.items():
				text = text.replace("{{" + key + "}}", str(value))
			return text

		rendered_system, rendered_user = sub(self.system), sub(self.user)
		leftover = set(re.findall(r"\{\{(\w+)\}\}", rendered_system + rendered_user))
		if leftover:
			raise ValueError(f"Prompt {self.name} has unfilled placeholders: {sorted(leftover)}")
		return rendered_system, rendered_user


def load_prompt(stage: str, version: int | None = None, prompts_dir: Path | None = None) -> Prompt:
	"""Load `prompts/<stage>/v<version>.md`, defaulting to the highest version."""
	base = (prompts_dir or PROMPTS_DIR) / stage
	if not base.exists():
		raise FileNotFoundError(f"No prompt directory for stage {stage!r} at {base}")

	if version is None:
		versions = sorted(
			(int(p.stem[1:]) for p in base.glob("v*.md") if p.stem[1:].isdigit()),
			reverse=True,
		)
		if not versions:
			raise FileNotFoundError(f"No versioned prompts in {base}")
		version = versions[0]

	path = base / f"v{version}.md"
	if not path.exists():
		raise FileNotFoundError(f"Prompt not found: {path}")

	raw = path.read_text(encoding="utf-8")
	meta: dict = {}
	if m := _FRONT_MATTER.match(raw):
		meta = yaml.safe_load(m.group(1)) or {}
		raw = raw[m.end() :]

	parts = _SECTION.split(raw)
	# split() yields [pre, label, body, label, body, ...]
	sections = {parts[i].strip().lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
	if "system" not in sections or "user" not in sections:
		raise ValueError(f"Prompt {path} must contain both '# System' and '# User' sections")

	return Prompt(
		name=f"{stage}/v{version}",
		version=version,
		system=sections["system"],
		user=sections["user"],
		meta=meta,
	)
