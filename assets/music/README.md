# Music bed

Drop one royalty-free audio file here (`.mp3`, `.m4a`, `.wav`, `.aac`, `.ogg`,
`.flac`) and set `render.background_music: true` in `config.yaml`.

The renderer loops and trims it to each segment's length, fades it in and out,
and mixes it at `render.music_db` (default -18 dB) under the narration.

If no file is present the renderer synthesises a soft pad instead. That fallback
is licence-safe but sounds like a hum once it is loud enough to notice, which is
why the flag is off by default. See `DECISIONS.md` D21.
