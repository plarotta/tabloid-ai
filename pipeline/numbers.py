"""Finding the numbers in a line of script, and asking whether the digest backs them.

Stage 6 already checks every value a chart *draws* (D37, D46). What it did not
check was everything else a viewer reads or hears as a figure: a static
callout's highlight, a title, a bullet, a chart's labels, and the narration.
D46's re-run found one - "4.44 stars", spoken and on screen, in no digest.

Two forms, because the script is written twice: narration spells numbers out
for the voice ("four point four four"), the screen shows digits ("4.44").

**Backed** means the digest states the value, or the value is arithmetic on two
values it states - a difference, a ratio or a sum, to the precision the claim
is written at. A "28-point gap" between 57 and 85 is a correct claim the digest
need not spell out; an "88.5" that is neither stated nor derivable is the
fabrication D17 found, repeated in the script. See DECISIONS.md D49.
"""

from __future__ import annotations

import itertools
import re

_UNITS = {
	w: i
	for i, w in enumerate(
		[
			"zero",
			"one",
			"two",
			"three",
			"four",
			"five",
			"six",
			"seven",
			"eight",
			"nine",
			"ten",
			"eleven",
			"twelve",
			"thirteen",
			"fourteen",
			"fifteen",
			"sixteen",
			"seventeen",
			"eighteen",
			"nineteen",
		]
	)
}
_TENS = {
	w: 10 * (i + 2)
	for i, w in enumerate(
		["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
	)
}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_DIGIT_WORDS = {**_UNITS, "oh": 0}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | set(_SCALES) | {"point", "and", "oh"}

_DIGITS = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?([KMBT](?![a-z]))?")
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def digit_numbers(text: str) -> list[float]:
	"""Every number written in digits. "2,518" is one number; "GPT-4" is 4.

	A magnitude suffix counts both ways - "4B" is 4 and four billion - since a
	digest writes "a 4B verifier" where the narration says "four billion".
	"""
	out = []
	for whole, frac, suffix in _DIGITS.findall(text or ""):
		v = float(whole.replace(",", "") + (frac or ""))
		out.append(v)
		if suffix:
			out.append(v * _SUFFIX[suffix])
	return out


def spoken_numbers(text: str) -> list[float]:
	"""Every number spelled out in words, as the narration writes them.

	Handles what the prompt asks the script to produce: "sixty-nine point five",
	"eleven thousand six hundred twenty-eight", "twenty-five hundred", "point
	one two". Not ordinals ("first"), not "a dozen", not "half" - a missed number
	is a check that did not run, never a false alarm.
	"""
	words = re.findall(r"[a-z]+", (text or "").lower().replace("-", " "))
	out: list[float] = []
	run: list[str] = []
	for w in [*words, ""]:
		if w in _NUMBER_WORDS and not (w == "and" and not run):
			run.append(w)
			continue
		while run and run[-1] in ("and", "point"):
			run.pop()
		if run:
			value = _parse(run)
			if value is not None:
				out.append(value)
		run = []
	return out


def _parse(words: list[str]) -> float | None:
	if "point" in words:
		i = words.index("point")
		whole = _parse(words[:i]) if i else 0.0
		digits = [_DIGIT_WORDS.get(w) for w in words[i + 1 :]]
		if whole is None or not digits or any(d is None or d > 9 for d in digits):
			return whole
		return float(f"{int(whole)}." + "".join(str(d) for d in digits))
	total, current, seen = 0, 0, False
	for w in words:
		if w == "and":
			continue
		if w in _UNITS:
			current += _UNITS[w]
		elif w in _TENS:
			current += _TENS[w]
		elif w == "hundred":
			current = (current or 1) * 100
		elif w in _SCALES:
			total += (current or 1) * _SCALES[w]
			current = 0
		elif w == "oh":
			return None
		seen = True
	return float(total + current) if seen else None


def _places(v: float) -> int:
	s = f"{abs(v):.10g}"
	return len(s.split(".")[1]) if "." in s and "e" not in s else 0


def _sig(v: float) -> int:
	"""Significant figures `v` is written to. A whole number's trailing zeros are
	not significant: "2500" is two, which is how "twenty-five hundred" rounds."""
	if v != int(v):
		return len(f"{abs(v):.10g}".replace(".", "").lstrip("0"))
	return len(str(int(abs(v))).rstrip("0")) or 1


def _close(x: float, claim: float) -> bool:
	"""Whether `x`, written at `claim`'s precision, is `claim`.

	Precision is decimal places for a decimal claim, and significant figures -
	never fewer than two - for a round one. So 2,518 backs "twenty-five
	hundred" and 67.9 backs "sixty-eight", while 85 does not back "ninety".
	"""
	if abs(round(x, _places(claim)) - claim) < 1e-9:
		return True
	if claim and claim == int(claim):
		sig = max(_sig(claim), 2)
		return abs(float(f"{x:.{sig}g}") - claim) < 1e-9
	return False


class Digest:
	"""The numbers a digest states, ready to test claims against."""

	def __init__(self, text: str) -> None:
		self.text = text or ""
		self.values = sorted(set(digit_numbers(self.text)) | set(spoken_numbers(self.text)))

	def states(self, v: float) -> bool:
		return any(_close(x, abs(v)) for x in self.values)

	def derives(self, v: float) -> bool:
		"""A difference, sum or ratio of two stated values, at the claim's precision."""
		v = abs(v)
		for a, b in itertools.combinations(self.values, 2):
			cands = [abs(a - b), a + b]
			if a and b:
				cands += [a / b, b / a]
			if any(_close(c, v) for c in cands):
				return True
		return False

	def backs(self, v: float) -> bool:
		return self.states(v) or self.derives(v)


def worth_checking(v: float) -> bool:
	"""Small whole numbers are everywhere - "three models", "GPT-4", "two
	stages" - and a check that fires on all of them is a check nobody reads.
	Decimals and anything from ten up are what gets invented."""
	return v != int(v) or abs(v) >= 10


def unbacked(text: str, digest: Digest, spoken: bool = False) -> list[str]:
	"""The numbers in `text` the digest neither states nor derives, as written."""
	found = spoken_numbers(text) if spoken else digit_numbers(text)
	return [f"{v:g}" for v in found if worth_checking(v) and not digest.backs(v)]
