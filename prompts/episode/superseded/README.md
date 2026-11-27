# Superseded prompt versions

`pr1-v8.md` (script) and `../../episode/superseded/pr1-v6.md` (episode) are the
prompts from PR #1, "Improve storytelling, pacing, and editorial motion". They
were written in parallel with `craft/ep8-prep`, which used the same version
numbers for different prompts. On merging (2026-11-26) the owner chose the
craft branch's editorial direction: its script v8-v10 and episode v6-v9 are live, and
these are kept for reference only. `load_prompt` globs `v*.md` in the stage
directory and never reads this folder. See DECISIONS.md D47.
