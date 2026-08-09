"""Stage 10 - publish the packaged episode to YouTube.

Dry-run is the default and is not a stub: it resolves the real files, applies
every YouTube-side limit, computes the real `publishAt`, and writes the exact
request body to `output/upload.json` for review. The only thing `enabled: true`
adds is the network call. That matters because the stage cannot be exercised for
real until the Google Cloud project passes its compliance audit (SPEC.md Stage
10), which is weeks of calendar time this code does not control.

Ordering under a live upload is deliberate:

    insert -> **write upload.json** -> thumbnail -> poll -> rewrite upload.json

The ID is persisted the instant it exists. Everything after the insert is
best-effort and degrades to a warning, because once a video is on YouTube the
worst outcome is a crash that loses the ID and lets the next run upload it again.

Quota: `videos.insert` costs 1600 units of the default 10,000/day, so this
project gets ~6 uploads per day before Google starts refusing. Fine for Tue/Thu;
worth knowing before scripting a backfill.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import UploadConfig
from ..paths import read_json, write_json
from ..schemas import PackageResult, UploadResult
from ..stage import Stage, StageError
from ..youtube import YouTubeClient, YouTubeError, missing_credentials

log = logging.getLogger(__name__)

# YouTube-side limits. Exceeding one is a 400 from the API, so they are checked
# here where the error can name the field.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
# The script prompt targets this; YouTube truncates around here in search results.
TITLE_DISPLAY_LIMIT = 70


def plan_publish_at(
	run_id: str, cfg: UploadConfig, now: datetime | None = None
) -> tuple[str | None, list[str]]:
	"""RFC3339 `publishAt` for this run, plus any warnings.

	Returns `None` when scheduling does not apply - YouTube only honours
	`publishAt` on a private video, and a public upload goes out immediately.
	"""
	warnings: list[str] = []
	now = now or datetime.now(UTC)

	if cfg.privacy_status != "private":
		warnings.append(
			f"privacy_status is {cfg.privacy_status!r}, so the episode publishes on upload; "
			f"publish_time_local is ignored (YouTube only schedules private videos)."
		)
		return None, warnings

	try:
		zone = ZoneInfo(cfg.timezone)
	except (ZoneInfoNotFoundError, ValueError) as e:
		raise StageError(f"upload.timezone {cfg.timezone!r} is not a known timezone: {e}") from e

	try:
		hh, mm = (int(p) for p in cfg.publish_time_local.split(":", 1))
		local_time = datetime.min.replace(hour=hh, minute=mm).time()
	except ValueError as e:
		raise StageError(
			f"upload.publish_time_local must look like '12:00', got {cfg.publish_time_local!r}"
		) from e

	try:
		run_date = date.fromisoformat(run_id)
	except ValueError:
		run_date = now.astimezone(zone).date()
		warnings.append(
			f"Run ID {run_id!r} is not a date, so the publish slot was taken from today "
			f"({run_date.isoformat()}) instead."
		)

	scheduled = datetime.combine(run_date, local_time, tzinfo=zone).astimezone(UTC)
	if scheduled <= now:
		bumped = (now + timedelta(minutes=cfg.late_publish_grace_minutes)).replace(microsecond=0)
		warnings.append(
			f"The {cfg.publish_time_local} {cfg.timezone} slot for {run_date.isoformat()} has "
			f"already passed; scheduling {cfg.late_publish_grace_minutes} min out "
			f"({bumped.strftime('%Y-%m-%dT%H:%M:%SZ')}) so the episode still goes out."
		)
		scheduled = bumped

	return scheduled.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"), warnings


def build_request_body(meta: PackageResult, cfg: UploadConfig, publish_at: str | None) -> dict:
	"""The `videos.insert` body. `containsSyntheticMedia` is not conditional."""
	status: dict = {
		"privacyStatus": cfg.privacy_status,
		"selfDeclaredMadeForKids": cfg.made_for_kids,
		# AI-content disclosure. Every episode here is synthesised narration over
		# programmatically rendered slides, so this is always true (spec Stage 10).
		"containsSyntheticMedia": True,
	}
	if publish_at:
		status["publishAt"] = publish_at

	snippet: dict = {
		"title": meta.title,
		"description": meta.description,
		"categoryId": cfg.category_id,
	}
	if cfg.tags:
		snippet["tags"] = list(cfg.tags)
	if cfg.language:
		snippet["defaultLanguage"] = cfg.language
		snippet["defaultAudioLanguage"] = cfg.language

	return {"snippet": snippet, "status": status}


def validate_body(body: dict, video: Path, thumbnail: Path | None) -> tuple[list[str], list[str]]:
	"""Check what YouTube would reject. Returns (errors, warnings)."""
	errors: list[str] = []
	warnings: list[str] = []
	snippet = body["snippet"]

	title = snippet.get("title") or ""
	if not title.strip():
		errors.append("Title is empty. Stage 6 produces it; check the episode prompt output.")
	if len(title) > TITLE_MAX:
		errors.append(f"Title is {len(title)} chars, over YouTube's {TITLE_MAX}.")
	elif len(title) > TITLE_DISPLAY_LIMIT:
		warnings.append(
			f"Title is {len(title)} chars; over ~{TITLE_DISPLAY_LIMIT} it gets truncated in "
			f"search and suggested views."
		)

	description = snippet.get("description") or ""
	if len(description) > DESCRIPTION_MAX:
		errors.append(f"Description is {len(description)} chars, over YouTube's {DESCRIPTION_MAX}.")
	if not description.strip():
		warnings.append("Description is empty; the arXiv links belong there.")

	# YouTube rejects angle brackets outright in both fields.
	for field, value in (("title", title), ("description", description)):
		bad = {c for c in "<>" if c in value}
		if bad:
			errors.append(f"{field} contains {' and '.join(sorted(bad))}, which YouTube rejects.")

	tags = snippet.get("tags") or []
	tag_chars = sum(len(t) for t in tags) + max(0, len(tags) - 1)
	if tag_chars > TAGS_TOTAL_MAX:
		errors.append(f"Tags total {tag_chars} chars, over YouTube's {TAGS_TOTAL_MAX}.")

	if not video.exists():
		errors.append(f"No episode video at {video}. Run the package stage first.")
	elif video.stat().st_size == 0:
		errors.append(f"Episode video {video} is empty.")

	if thumbnail is None or not thumbnail.exists():
		warnings.append("No thumbnail in the bundle; YouTube will pick a frame.")
	elif thumbnail.stat().st_size > THUMBNAIL_MAX_BYTES:
		warnings.append(
			f"Thumbnail is {thumbnail.stat().st_size / 1024:.0f} KB, over YouTube's 2 MB "
			f"limit; it will be skipped."
		)

	status = body["status"]
	if not status.get("containsSyntheticMedia"):
		errors.append("containsSyntheticMedia must be set - AI disclosure is non-negotiable.")
	if status.get("publishAt") and status.get("privacyStatus") != "private":
		errors.append("publishAt only applies to a private video; YouTube rejects the pairing.")

	return errors, warnings


class UploadStage(Stage):
	name = "upload"

	def is_complete(self) -> bool:
		"""Only a real upload counts. A dry-run artifact must not make the stage
		look done, or enabling uploads later would silently skip it."""
		path = self.paths.upload_json
		if not path.exists():
			return False
		try:
			result = UploadResult.model_validate(read_json(path))
		except Exception:
			return False
		return bool(result.video_id) and not result.dry_run

	def load(self) -> UploadResult:
		return UploadResult.model_validate(read_json(self.paths.upload_json))

	def _previous(self) -> UploadResult | None:
		"""A completed live upload for this run, if there is one."""
		if not self.is_complete():
			return None
		return self.load()

	def _write(self, result: UploadResult) -> None:
		write_json(self.paths.upload_json, json.loads(result.model_dump_json()))

	def run(self) -> UploadResult:
		from .package import PackageStage

		if self.ctx.paper_filter:
			raise StageError(
				"--paper does not apply to upload: the stage publishes the packaged "
				"episode, not a single segment."
			)

		# Idempotency (spec Stage 10): never double-upload.
		done = self._previous()
		if done is not None:
			log.info(
				"Already uploaded as %s; nothing to do. Delete %s to force a re-upload.",
				done.video_id,
				self.paths.upload_json,
			)
			return done

		package = PackageStage(self.ctx)
		if not package.is_complete():
			raise StageError("Nothing packaged for this run. Run the package stage first.")
		meta = package.load()

		out = self.paths.output_dir
		video = out / (meta.episode_file or "episode.mp4")
		thumbnail = out / meta.thumbnail_file if meta.thumbnail_file else None

		cfg = self.config.upload
		publish_at, warnings = plan_publish_at(self.paths.run_id, cfg)
		body = build_request_body(meta, cfg, publish_at)
		errors, more = validate_body(body, video, thumbnail)
		warnings += more

		for w in warnings:
			log.warning(w)
		if errors:
			raise StageError("Upload metadata is not publishable:\n  - " + "\n  - ".join(errors))

		usable_thumbnail = (
			thumbnail
			if thumbnail and thumbnail.exists() and thumbnail.stat().st_size <= THUMBNAIL_MAX_BYTES
			else None
		)
		size = video.stat().st_size
		result = UploadResult(
			generated_at=datetime.now(UTC),
			dry_run=not cfg.enabled,
			privacy_status=cfg.privacy_status,
			publish_at=publish_at,
			video_file=video.name,
			video_bytes=size,
			request_body=body,
			warnings=warnings,
		)

		if not cfg.enabled:
			self._write(result)
			log.info(
				"DRY RUN - would upload %s (%.1f MB) as %r, %s, publishAt=%s, "
				"containsSyntheticMedia=true. Request body written to %s.",
				video.name,
				size / 1e6,
				body["snippet"]["title"],
				cfg.privacy_status,
				publish_at or "immediate",
				self.paths.upload_json,
			)
			log.info(
				"Set upload.enabled: true in config.yaml once the YouTube API compliance "
				"audit has cleared. Until then, upload %s by hand in YouTube Studio using "
				"output/metadata.json.",
				video.name,
			)
			return result

		return self._upload(result, video, usable_thumbnail, body, cfg)

	def _upload(
		self,
		result: UploadResult,
		video: Path,
		thumbnail: Path | None,
		body: dict,
		cfg: UploadConfig,
	) -> UploadResult:
		"""The live path. Everything after `insert` is best-effort."""
		missing = missing_credentials()
		if missing:
			raise StageError(
				f"upload.enabled is true but the OAuth credentials are missing: "
				f"{', '.join(missing)}. Run `pipeline youtube-auth` once, or set "
				f"upload.enabled back to false to stay in dry-run."
			)

		client = YouTubeClient(
			max_retries=cfg.max_retries, chunk_size_bytes=cfg.chunk_size_mb * 1024 * 1024
		)
		log.info("Uploading %s (%.1f MB) to YouTube...", video.name, result.video_bytes / 1e6)
		try:
			video_id = client.insert_video(video, body)
		except YouTubeError as e:
			raise StageError(str(e)) from e

		# Persist before anything else can fail: an unrecorded video ID is how a
		# re-run ends up publishing the episode twice.
		result.video_id = video_id
		result.video_url = f"https://www.youtube.com/watch?v={video_id}"
		self._write(result)

		if thumbnail is not None:
			try:
				client.set_thumbnail(video_id, thumbnail)
				result.thumbnail_set = True
			except YouTubeError as e:
				# Custom thumbnails need a verified channel; not worth failing over.
				msg = f"Thumbnail was not set: {e}"
				log.warning(msg)
				result.warnings.append(msg)

		try:
			status = client.wait_until_processed(
				video_id,
				timeout_seconds=cfg.poll_timeout_seconds,
				interval_seconds=cfg.poll_interval_seconds,
			)
			result.processing_status = status.processing_status
			result.warnings.extend(status.notes)
			if status.privacy_status:
				result.privacy_status = status.privacy_status
			# The audit tell: an unverified project silently ignores publishAt and
			# leaves the video private forever. Say so rather than letting a run
			# look successful while nothing ever publishes. Only meaningful if the
			# poll actually read the video back.
			poll_read_the_video = status.processing_status not in {"unverified", "", "unknown"}
			if result.publish_at and not status.publish_at and poll_read_the_video:
				msg = (
					"YouTube did not record the scheduled publish time. This is what an "
					"un-audited API project looks like: uploads stay private until the "
					"YouTube API compliance audit clears. Publish by hand in the meantime."
				)
				log.warning(msg)
				result.warnings.append(msg)
		except YouTubeError as e:
			msg = f"Uploaded, but processing could not be confirmed: {e}"
			log.warning(msg)
			result.warnings.append(msg)
			result.processing_status = "unknown"

		self._write(result)
		log.info(
			"Uploaded %s as %s (%s%s)",
			video.name,
			result.video_url,
			result.privacy_status,
			f", publishing {result.publish_at}" if result.publish_at else "",
		)
		return result
