# spikes

Throwaway code kept for its measurements, not for reuse. Nothing here is
imported by `pipeline/`, nothing here is tested, and nothing here runs in CI.

## `animated_callout.py`

The hand-written manim scene that decided D36. It animates one real
`result_callout` from `runs/2026-08-19` — segment `2608.17286`, scene `s3`,
measured 9.55s — as a two-bar comparison, and carries five palette variants
selected with the `SPIKE_PALETTE` environment variable.

It is kept because it is the artefact the palette decision was made against, and
because its calibration constants (`FONT_RATIO`, `PX`) were measured rather than
derived. The shipped implementation lives in `pipeline/render/animate.py` and
`pipeline/render/manim_scenes.py`.

```bash
uv venv --python 3.12 /tmp/manim-spike
VIRTUAL_ENV=/tmp/manim-spike uv pip install manim
SPIKE_PALETTE=chosen /tmp/manim-spike/bin/manim render spikes/animated_callout.py \
  AnimatedCallout -r 1920,1080 --fps 30 --format=mp4 -o chosen
```

Variants: `current` (the baseline bar in the rules colour — the bug D36 names),
`neutral`, `mono`, `duotone`, `lifted`, `chosen`.
