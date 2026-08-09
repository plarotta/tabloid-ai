"""YouTube Data API v3 adapter for Stage 10.

Everything that talks to Google lives here; `stages/upload.py` holds the policy
(what to send, when to refuse) and never imports a Google symbol. That split is
what lets the whole dry-run path - and most of the test suite - run with none of
the `google-*` packages installed.

Scope is `youtube.upload` and nothing wider. The spec asks for the narrowest
scope that does the job, both on principle and because a narrow scope is an
easier compliance audit to pass. It covers `videos.insert` and `thumbnails.set`.
It does *not* reliably cover `videos.list`, so the processing poll degrades to a
warning rather than widening the scope - see `wait_until_processed`.

Auth is non-interactive: a refresh token from the environment is exchanged for an
access token on every run. Obtaining that refresh token once is a separate
interactive step (`pipeline youtube-auth`).
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The one scope this pipeline asks for.
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Transient by Google's own retry guidance: 5xx plus rate limiting. Anything else
# (400 bad metadata, 401 bad token, 403 quota/permission) is a real failure that
# retrying only makes slower.
RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})

ENV_CLIENT_ID = "YOUTUBE_CLIENT_ID"
ENV_CLIENT_SECRET = "YOUTUBE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "YOUTUBE_REFRESH_TOKEN"


class YouTubeError(RuntimeError):
	"""Anything that stops the upload. The stage turns this into a StageError."""


@dataclass
class VideoStatus:
	"""What the API reports back about a video after upload."""

	video_id: str
	privacy_status: str = ""
	publish_at: str | None = None
	upload_status: str = ""
	processing_status: str = ""
	# Set when the poll could not run (scope) rather than reporting a real state.
	notes: list[str] = field(default_factory=list)

	@property
	def url(self) -> str:
		return f"https://www.youtube.com/watch?v={self.video_id}"


def missing_credentials() -> list[str]:
	"""Which of the three OAuth env vars are unset or empty."""
	names = (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN)
	return [k for k in names if not os.environ.get(k)]


def _http_status(exc: Exception) -> int | None:
	"""Status code out of a googleapiclient HttpError, without importing it."""
	resp = getattr(exc, "resp", None)
	status = getattr(resp, "status", None)
	if status is None:
		status = getattr(exc, "status_code", None)
	try:
		return int(status) if status is not None else None
	except (TypeError, ValueError):
		return None


def build_credentials():
	"""Exchange the stored refresh token for an access token. Non-interactive.

	Raises with the exact missing variable names rather than a Google traceback:
	this failure is nearly always a deployment problem, not a bug.
	"""
	missing = missing_credentials()
	if missing:
		raise YouTubeError(
			f"Missing YouTube OAuth credentials: {', '.join(missing)}. "
			f"Run `pipeline youtube-auth` once to obtain a refresh token, then put all "
			f"three in .env (see .env.example)."
		)
	try:
		from google.auth.transport.requests import Request
		from google.oauth2.credentials import Credentials
	except ImportError as e:  # pragma: no cover - exercised only without the extra
		raise YouTubeError(
			"YouTube upload needs the google client libraries: `uv pip install -e '.[youtube]'`"
		) from e

	creds = Credentials(
		token=None,
		refresh_token=os.environ[ENV_REFRESH_TOKEN],
		client_id=os.environ[ENV_CLIENT_ID],
		client_secret=os.environ[ENV_CLIENT_SECRET],
		token_uri=TOKEN_URI,
		scopes=[SCOPE],
	)
	try:
		creds.refresh(Request())
	except Exception as e:
		raise YouTubeError(
			f"Could not refresh the YouTube access token: {e}. The refresh token may "
			f"have been revoked or the OAuth client deleted; re-run `pipeline youtube-auth`."
		) from e
	return creds


class YouTubeClient:
	"""Thin wrapper over `googleapiclient`. Resumable upload, backoff, thumbnail."""

	def __init__(
		self,
		credentials: Any = None,
		*,
		max_retries: int = 5,
		chunk_size_bytes: int = 8 * 1024 * 1024,
		sleep=time.sleep,
	) -> None:
		self._credentials = credentials
		self._service = None
		self.max_retries = max_retries
		self.chunk_size_bytes = chunk_size_bytes
		self._sleep = sleep

	# --- plumbing ------------------------------------------------------------

	def service(self):
		"""Built lazily so constructing a client costs nothing in dry-run."""
		if self._service is None:
			try:
				from googleapiclient.discovery import build
			except ImportError as e:  # pragma: no cover - exercised only without the extra
				raise YouTubeError(
					"YouTube upload needs the google client libraries: "
					"`uv pip install -e '.[youtube]'`"
				) from e
			creds = self._credentials or build_credentials()
			# cache_discovery=False: the file cache warns under any non-oauth2client
			# credential and we do not want a cache in a scheduled runner anyway.
			self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
		return self._service

	def _backoff(self, attempt: int) -> float:
		"""Exponential with jitter, capped. Jitter matters because a Tue/Thu cron
		means every retry in a fleet would otherwise line up on the same second."""
		return min(2**attempt, 64) + random.uniform(0, 1)

	def _retry(self, describe: str, call):
		"""Run `call()`, retrying only on transient status codes."""
		last: Exception | None = None
		for attempt in range(self.max_retries + 1):
			try:
				return call()
			except YouTubeError:
				raise
			except Exception as e:
				status = _http_status(e)
				retriable = status in RETRIABLE_STATUS or (status is None and _is_transport(e))
				if not retriable or attempt == self.max_retries:
					raise YouTubeError(f"{describe} failed: {e}") from e
				delay = self._backoff(attempt)
				log.warning(
					"%s failed (%s), retry %s/%s in %.1fs",
					describe,
					status or type(e).__name__,
					attempt + 1,
					self.max_retries,
					delay,
				)
				self._sleep(delay)
				last = e
		raise YouTubeError(f"{describe} failed: {last}")  # pragma: no cover - loop always returns

	# --- API calls -----------------------------------------------------------

	def insert_video(self, video_path: Path, body: dict) -> str:
		"""Resumable `videos.insert`. Returns the new video ID.

		A resumable upload survives a mid-transfer 5xx: `next_chunk()` picks up
		where it stopped instead of re-sending an 11 MB file from byte zero.
		"""
		try:
			from googleapiclient.http import MediaFileUpload
		except ImportError as e:  # pragma: no cover - exercised only without the extra
			raise YouTubeError(
				"YouTube upload needs the google client libraries: `uv pip install -e '.[youtube]'`"
			) from e

		media = MediaFileUpload(
			str(video_path), chunksize=self.chunk_size_bytes, resumable=True, mimetype="video/mp4"
		)
		request = self.service().videos().insert(part=",".join(body), body=body, media_body=media)

		response = None
		attempt = 0
		while response is None:
			try:
				progress, response = request.next_chunk()
			except Exception as e:
				status = _http_status(e)
				if (status in RETRIABLE_STATUS or _is_transport(e)) and attempt < self.max_retries:
					delay = self._backoff(attempt)
					attempt += 1
					log.warning(
						"Upload chunk failed (%s), resuming in %.1fs (%s/%s)",
						status or type(e).__name__,
						delay,
						attempt,
						self.max_retries,
					)
					self._sleep(delay)
					continue
				raise YouTubeError(f"videos.insert failed: {e}") from e
			# A successful chunk resets the budget: this is a fresh network state,
			# not a continuation of whatever failed earlier in the transfer.
			attempt = 0
			if progress is not None:
				log.info("Uploading... %d%%", int(progress.progress() * 100))

		video_id = (response or {}).get("id")
		if not video_id:
			raise YouTubeError(f"videos.insert returned no video ID: {response!r}")
		log.info("Uploaded: https://www.youtube.com/watch?v=%s", video_id)
		return video_id

	def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> None:
		"""`thumbnails.set`. Covered by the upload scope."""
		try:
			from googleapiclient.http import MediaFileUpload
		except ImportError as e:  # pragma: no cover - exercised only without the extra
			raise YouTubeError(
				"YouTube upload needs the google client libraries: `uv pip install -e '.[youtube]'`"
			) from e

		media = MediaFileUpload(str(thumbnail_path), mimetype="image/png")
		self._retry(
			"thumbnails.set",
			lambda: self.service().thumbnails().set(videoId=video_id, media_body=media).execute(),
		)
		log.info("Thumbnail set on %s", video_id)

	def fetch_status(self, video_id: str) -> dict:
		"""One `videos.list` for status + processing details."""
		resp = self._retry(
			"videos.list",
			lambda: (
				self.service().videos().list(part="status,processingDetails", id=video_id).execute()
			),
		)
		items = (resp or {}).get("items") or []
		if not items:
			raise YouTubeError(f"videos.list returned no item for {video_id}")
		return items[0]

	def wait_until_processed(
		self, video_id: str, *, timeout_seconds: float = 900, interval_seconds: float = 20
	) -> VideoStatus:
		"""Poll until YouTube finishes processing, or give up loudly-but-safely.

		Three outcomes, none of which un-uploads the video:

		"processed" is the happy path, and "failed"/"rejected" raises - the episode
		is not going to publish.

		"unverified" means the poll itself could not run. `youtube.upload` is not
		documented to authorise `videos.list`, so a 403 here says the scope is too
		narrow for *verification only*. The upload already succeeded and its ID is
		already recorded, so that is a warning, not a failure. Widening to
		`youtube.readonly` would fix it at the cost of a broader audit.
		"""
		status = VideoStatus(video_id=video_id)
		deadline = time.monotonic() + timeout_seconds
		while True:
			try:
				item = self.fetch_status(video_id)
			except YouTubeError as e:
				if _http_status(e.__cause__ or e) == 403:
					note = (
						"Could not verify processing: videos.list refused the "
						"youtube.upload scope. The upload itself succeeded."
					)
					log.warning(note)
					status.processing_status = "unverified"
					status.notes.append(note)
					return status
				raise

			st = item.get("status") or {}
			details = item.get("processingDetails") or {}
			status.privacy_status = st.get("privacyStatus", "")
			status.publish_at = st.get("publishAt")
			status.upload_status = st.get("uploadStatus", "")
			status.processing_status = details.get("processingStatus", "")

			if status.upload_status in {"rejected", "failed"}:
				reason = st.get("rejectionReason") or st.get("failureReason") or "unknown"
				raise YouTubeError(f"YouTube {status.upload_status} the video ({reason}).")
			if status.processing_status == "succeeded" or status.upload_status == "processed":
				log.info("Processing complete for %s", video_id)
				return status
			if time.monotonic() >= deadline:
				note = (
					f"Still processing after {timeout_seconds:.0f}s "
					f"(uploadStatus={status.upload_status or '?'}). Not an error - large "
					f"videos routinely take longer; check YouTube Studio."
				)
				log.warning(note)
				status.notes.append(note)
				return status
			self._sleep(interval_seconds)


def _is_transport(exc: Exception) -> bool:
	"""Socket/SSL/timeout-class errors, which are worth resuming on."""
	import http.client
	import socket
	import ssl

	return isinstance(exc, socket.timeout | ssl.SSLError | http.client.HTTPException | OSError)
