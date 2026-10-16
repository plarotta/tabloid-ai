"""Give an existing preview breathing room. Local ffmpeg only; no provider calls.

Slows picture and speech together (pitch preserved), then holds each completed
scene in silence. Always start from the original preview to avoid compounded
slowdowns. The input video and cue report remain untouched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def duration(path: Path) -> float:
	return float(
		subprocess.check_output(
			[
				"ffprobe",
				"-v",
				"error",
				"-show_entries",
				"format=duration",
				"-of",
				"default=noprint_wrappers=1:nokey=1",
				str(path),
			],
			text=True,
		)
	)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source", type=Path, default=Path("demos/out/transformer-final"))
	parser.add_argument("--output", type=Path, default=Path("demos/out/transformer-calm"))
	parser.add_argument("--rate", type=float, default=0.8)
	parser.add_argument("--hold", type=float, default=1.5)
	args = parser.parse_args()
	if not 0.5 <= args.rate <= 1 or not 0 <= args.hold <= 5:
		parser.error("Use a rate from 0.5 to 1 and a hold from 0 to 5 seconds.")
	source, output = args.source.resolve(), args.output.resolve()
	if source == output:
		parser.error("Output must differ from source; retain the original narration/video.")
	report = json.loads((source / "pacing.json").read_text())
	if "retiming" in report:
		parser.error("Source is already retimed. Start from the original preview.")
	video = source / "editorial-preview.mp4"
	original_duration = duration(video)
	if not report["scenes"] or abs(report["duration_seconds"] - original_duration) > 0.2:
		parser.error("The pacing report does not match the source video duration.")
	output.mkdir(parents=True, exist_ok=True)
	elapsed = 0.0
	with tempfile.TemporaryDirectory(prefix="tabloid-calm-") as temp:
		paths = []
		for i, row in enumerate(report["scenes"]):
			path = Path(temp) / f"{i:03d}.mp4"
			seconds = row["duration_seconds"]
			subprocess.run(
				[
					"ffmpeg",
					"-v",
					"error",
					"-y",
					"-ss",
					str(row["start_seconds"]),
					"-t",
					str(seconds),
					"-i",
					str(video),
					"-vf",
					f"setpts=(PTS-STARTPTS)/{args.rate},tpad=stop_mode=clone:stop_duration={args.hold}",
					"-af",
					f"atempo={args.rate},apad=pad_dur={args.hold}",
					"-t",
					str(seconds / args.rate + args.hold),
					"-r",
					"30",
					"-c:v",
					"libx264",
					"-threads",
					"2",
					"-preset",
					"fast",
					"-crf",
					"18",
					"-c:a",
					"aac",
					"-b:a",
					"192k",
					"-movflags",
					"+faststart",
					str(path),
				],
				check=True,
				timeout=180,
			)
			paths.append(path)
			row["start_seconds"] = round(elapsed, 3)
			row["duration_seconds"] = duration(path)
			row["reading_hold_seconds"] = args.hold
			row["reveal_cues_seconds"] = {
				k: round(v / args.rate, 3) for k, v in row["reveal_cues_seconds"].items()
			}
			elapsed += row["duration_seconds"]
		listing = Path(temp) / "clips.txt"
		listing.write_text("".join(f"file '{p.name}'\n" for p in paths))
		final = output / "editorial-preview.mp4"
		subprocess.run(
			[
				"ffmpeg",
				"-v",
				"error",
				"-y",
				"-f",
				"concat",
				"-safe",
				"0",
				"-i",
				str(listing),
				"-c",
				"copy",
				"-movflags",
				"+faststart",
				str(final),
			],
			check=True,
			timeout=180,
		)
	report["duration_seconds"] = duration(final)
	report["retiming"] = {
		"source": str(video),
		"rate": args.rate,
		"hold_seconds": args.hold,
		"api_spend_usd": 0,
	}
	report["average_scene_seconds"] = round(elapsed / len(report["scenes"]), 2)
	# Old pacing flags were based on the faster edit. Retain them as history,
	# not as a claimed review of the slower cut.
	report["source_issues"] = report.pop("issues", [])
	report["note"] = (
		"Offline slower cut; cue times scaled with picture and audio. Visual review required."
	)
	(output / "pacing.json").write_text(json.dumps(report, indent=2) + "\n")
	print(f"{final}\n{original_duration:.1f}s → {report['duration_seconds']:.1f}s; $0 API spend")


if __name__ == "__main__":
	main()
