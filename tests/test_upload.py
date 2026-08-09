"""Stage 10 - upload.

The live path cannot be exercised against YouTube (the compliance audit is
outstanding, and a passing test suite must never publish a video), so the API is
faked at two seams: `YouTubeClient` for the stage's policy, and a fake
`googleapiclient` service for the client's own retry/resume logic.

Everything else here runs against the real code with no fakes at all, because
dry-run is the real code minus the network.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.config import UploadConfig
from pipeline.paths import STAGE_ORDER, read_json, write_json
from pipeline.schemas import PackageResult, UploadResult
from pipeline.stage import StageError
from pipeline.stages.upload import (
	THUMBNAIL_MAX_BYTES,
	UploadStage,
	build_request_body,
	plan_publish_at,
	validate_body,
)
from pipeline.youtube import RETRIABLE_STATUS, VideoStatus, YouTubeClient, YouTubeError

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)  # 05:00 ET, before the noon slot


# --- helpers -----------------------------------------------------------------


def make_metadata(**overrides) -> PackageResult:
	data = {
		"generated_at": datetime.now(UTC),
		"title": "Robots hijacked with sticky notes",
		"description": "Three papers.\n\nhttps://arxiv.org/abs/2608.05715",
		"thumbnail_text_options": ["Sticky note attacks"],
		"episode_file": "episode.mp4",
		"segment_files": ["segment_2608.05715.mp4"],
		"thumbnail_file": "thumbnail.png",
		"duration_seconds": 291.5,
		"chapters": [{"time": "0:00", "seconds": 0.0, "label": "Intro"}],
		"total_cost_usd": 1.96,
	}
	data.update(overrides)
	return PackageResult.model_validate(data)


def make_bundle(ctx, meta: PackageResult | None = None, thumb_bytes: int = 1024) -> Path:
	"""A packaged run on disk: metadata.json + episode.mp4 + thumbnail.png."""
	meta = meta or make_metadata()
	out = ctx.paths.output_dir
	write_json(out / "metadata.json", json.loads(meta.model_dump_json()))
	(out / "episode.mp4").write_bytes(b"\x00" * 2048)
	if meta.thumbnail_file:
		(out / meta.thumbnail_file).write_bytes(b"\x89PNG" + b"\x00" * (thumb_bytes - 4))
	return out


class FakeClient:
	"""Stands in for YouTubeClient in the stage's live path."""

	def __init__(self, *, video_id="vid123", status=None, thumbnail_error=None, **kwargs):
		self.video_id = video_id
		self.kwargs = kwargs
		self.inserted: list[tuple[Path, dict]] = []
		self.thumbnails: list[tuple[str, Path]] = []
		self.polled: list[str] = []
		self._status = status or VideoStatus(
			video_id=video_id,
			privacy_status="private",
			publish_at="2026-08-07T16:00:00Z",
			upload_status="uploaded",
			processing_status="succeeded",
		)
		self._thumbnail_error = thumbnail_error

	def insert_video(self, path, body):
		self.inserted.append((path, body))
		return self.video_id

	def set_thumbnail(self, video_id, path):
		if self._thumbnail_error:
			raise YouTubeError(self._thumbnail_error)
		self.thumbnails.append((video_id, path))

	def wait_until_processed(self, video_id, **kwargs):
		self.polled.append(video_id)
		return self._status


@pytest.fixture
def live(ctx, monkeypatch):
	"""Enable uploads with credentials present and the API faked out."""
	ctx.config.upload.enabled = True
	monkeypatch.setattr("pipeline.stages.upload.missing_credentials", lambda: [])
	holder: dict = {}

	def factory(**kwargs):
		client = FakeClient(**kwargs)
		holder["client"] = client
		return client

	monkeypatch.setattr("pipeline.stages.upload.YouTubeClient", factory)
	return holder


# --- publish scheduling ------------------------------------------------------


def test_publish_at_is_the_configured_local_slot_in_utc():
	cfg = UploadConfig()
	at, warnings = plan_publish_at("2026-08-07", cfg, now=NOW)
	# Noon in New York in August is 16:00 UTC.
	assert at == "2026-08-07T16:00:00Z"
	assert warnings == []


def test_publish_at_slides_forward_when_the_slot_has_passed():
	"""A late run should still publish today, loudly, rather than silently
	scheduling in the past."""
	cfg = UploadConfig(late_publish_grace_minutes=15)
	late = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)  # 16:00 ET, past the noon slot
	at, warnings = plan_publish_at("2026-08-07", cfg, now=late)
	assert at == "2026-08-07T20:15:00Z"
	assert any("already passed" in w for w in warnings)


def test_public_upload_has_no_publish_at():
	"""YouTube only schedules private videos; sending both is a 400."""
	at, warnings = plan_publish_at("2026-08-07", UploadConfig(privacy_status="public"), now=NOW)
	assert at is None
	assert any("publishes on upload" in w for w in warnings)


def test_non_date_run_id_falls_back_to_today():
	at, warnings = plan_publish_at("enrich-smoke", UploadConfig(), now=NOW)
	assert at == "2026-08-07T16:00:00Z"
	assert any("not a date" in w for w in warnings)


def test_bad_timezone_and_bad_time_fail_with_the_field_named():
	with pytest.raises(StageError, match=r"upload\.timezone"):
		plan_publish_at("2026-08-07", UploadConfig(timezone="Mars/Olympus"), now=NOW)
	with pytest.raises(StageError, match="publish_time_local"):
		plan_publish_at("2026-08-07", UploadConfig(publish_time_local="noon"), now=NOW)


# --- request body and validation ---------------------------------------------


def test_synthetic_media_disclosure_is_always_set():
	"""Non-negotiable for this channel (spec Stage 10). No config can turn it off."""
	body = build_request_body(make_metadata(), UploadConfig(), "2026-08-07T16:00:00Z")
	assert body["status"]["containsSyntheticMedia"] is True
	assert "containsSyntheticMedia" not in UploadConfig.model_fields


def test_request_body_shape():
	cfg = UploadConfig(tags=["AI", "arXiv"], category_id="28")
	body = build_request_body(make_metadata(), cfg, "2026-08-07T16:00:00Z")
	assert body["snippet"]["title"] == "Robots hijacked with sticky notes"
	assert body["snippet"]["tags"] == ["AI", "arXiv"]
	assert body["snippet"]["categoryId"] == "28"
	assert body["status"]["privacyStatus"] == "private"
	assert body["status"]["publishAt"] == "2026-08-07T16:00:00Z"
	assert body["status"]["selfDeclaredMadeForKids"] is False


@pytest.mark.parametrize(
	("meta_kwargs", "expected"),
	[
		({"title": "x" * 101}, "over YouTube's 100"),
		({"title": ""}, "Title is empty"),
		({"title": "Papers <b>ranked</b>"}, "which YouTube rejects"),
		({"description": "y" * 5001}, "over YouTube's 5000"),
	],
)
def test_metadata_that_youtube_would_reject_fails_here(ctx, meta_kwargs, expected):
	make_bundle(ctx, make_metadata(**meta_kwargs))
	with pytest.raises(StageError, match=expected):
		UploadStage(ctx).run()


def test_oversized_tag_list_is_an_error():
	body = build_request_body(make_metadata(), UploadConfig(tags=["tag" * 40] * 5), None)
	errors, _ = validate_body(body, Path("/nonexistent"), None)
	assert any("over YouTube's 500" in e for e in errors)


def test_missing_episode_names_the_stage_to_run(ctx):
	make_bundle(ctx)
	(ctx.paths.output_dir / "episode.mp4").unlink()
	with pytest.raises(StageError, match="Run the package stage first"):
		UploadStage(ctx).run()


def test_unpackaged_run_is_refused(ctx):
	with pytest.raises(StageError, match="Nothing packaged"):
		UploadStage(ctx).run()


def test_paper_filter_is_refused(ctx):
	make_bundle(ctx)
	ctx.paper_filter = "2608.05715"
	with pytest.raises(StageError, match="--paper does not apply"):
		UploadStage(ctx).run()


# --- dry run -----------------------------------------------------------------


def test_dry_run_writes_the_exact_request_body_and_uploads_nothing(ctx):
	make_bundle(ctx)
	result = UploadStage(ctx).run()

	assert result.dry_run is True
	assert result.video_id is None
	assert result.request_body["status"]["containsSyntheticMedia"] is True
	assert result.video_bytes == 2048

	on_disk = UploadResult.model_validate(read_json(ctx.paths.upload_json))
	assert on_disk.request_body == result.request_body
	assert on_disk.publish_at == result.publish_at


def test_dry_run_does_not_mark_the_stage_complete(ctx):
	"""Otherwise flipping `enabled` later would skip the stage entirely."""
	make_bundle(ctx)
	stage = UploadStage(ctx)
	stage.run()
	assert stage.is_complete() is False


def test_dry_run_needs_no_credentials(ctx, monkeypatch):
	for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
		monkeypatch.delenv(var, raising=False)
	make_bundle(ctx)
	assert UploadStage(ctx).run().dry_run is True


def test_upload_json_lands_beside_the_bundle(ctx):
	make_bundle(ctx)
	UploadStage(ctx).run()
	# The spec names this path: runs/<date>/output/upload.json
	assert ctx.paths.upload_json == ctx.paths.output_dir / "upload.json"
	assert ctx.paths.upload_json.exists()


# --- live path ---------------------------------------------------------------


def test_live_upload_records_id_thumbnail_and_processing(ctx, live):
	make_bundle(ctx)
	result = UploadStage(ctx).run()
	client = live["client"]

	assert result.dry_run is False
	assert result.video_id == "vid123"
	assert result.video_url == "https://www.youtube.com/watch?v=vid123"
	assert result.thumbnail_set is True
	assert result.processing_status == "succeeded"
	assert [p.name for p, _ in client.inserted] == ["episode.mp4"]
	assert client.thumbnails[0][1].name == "thumbnail.png"
	assert client.polled == ["vid123"]

	assert UploadResult.model_validate(read_json(ctx.paths.upload_json)).video_id == "vid123"


def test_enabled_without_credentials_fails_before_uploading(ctx, monkeypatch):
	ctx.config.upload.enabled = True
	monkeypatch.setattr(
		"pipeline.stages.upload.missing_credentials", lambda: ["YOUTUBE_REFRESH_TOKEN"]
	)
	make_bundle(ctx)
	with pytest.raises(StageError, match="YOUTUBE_REFRESH_TOKEN"):
		UploadStage(ctx).run()
	assert not ctx.paths.upload_json.exists()


def test_rerun_does_not_double_upload(ctx, live):
	"""The spec's idempotency requirement, and the reason the ID is written the
	moment it exists."""
	make_bundle(ctx)
	first = UploadStage(ctx).run()
	live.pop("client")

	second = UploadStage(ctx).run()
	assert second.video_id == first.video_id
	assert "client" not in live  # no client was ever constructed


def test_video_id_is_persisted_before_the_follow_up_calls(ctx, monkeypatch):
	"""A crash between insert and the follow-up calls must still leave the video
	ID on disk - otherwise the next run publishes the episode a second time."""
	ctx.config.upload.enabled = True
	monkeypatch.setattr("pipeline.stages.upload.missing_credentials", lambda: [])

	class Exploding(FakeClient):
		def wait_until_processed(self, video_id, **kwargs):
			raise RuntimeError("network died")

	monkeypatch.setattr("pipeline.stages.upload.YouTubeClient", lambda **kw: Exploding(**kw))
	make_bundle(ctx)

	with pytest.raises(RuntimeError, match="network died"):
		UploadStage(ctx).run()

	recorded = UploadResult.model_validate(read_json(ctx.paths.upload_json))
	assert recorded.video_id == "vid123"
	assert recorded.dry_run is False
	# And the next run treats it as done rather than uploading again.
	assert UploadStage(ctx).is_complete() is True


def test_thumbnail_failure_does_not_fail_the_upload(ctx, monkeypatch):
	ctx.config.upload.enabled = True
	monkeypatch.setattr("pipeline.stages.upload.missing_credentials", lambda: [])
	monkeypatch.setattr(
		"pipeline.stages.upload.YouTubeClient",
		lambda **kw: FakeClient(thumbnail_error="channel not verified", **kw),
	)
	make_bundle(ctx)

	result = UploadStage(ctx).run()
	assert result.video_id == "vid123"
	assert result.thumbnail_set is False
	assert any("Thumbnail was not set" in w for w in result.warnings)


def test_oversized_thumbnail_is_skipped_not_fatal(ctx, live):
	make_bundle(ctx, thumb_bytes=THUMBNAIL_MAX_BYTES + 1)
	result = UploadStage(ctx).run()
	assert result.video_id == "vid123"
	assert live["client"].thumbnails == []
	assert any("over YouTube's 2 MB" in w for w in result.warnings)


def test_ignored_publish_time_is_reported_as_the_audit_symptom(ctx, monkeypatch):
	"""An un-audited project accepts the upload and drops publishAt. The run must
	not look like a success that will publish."""
	ctx.config.upload.enabled = True
	monkeypatch.setattr("pipeline.stages.upload.missing_credentials", lambda: [])
	status = VideoStatus(
		video_id="vid123",
		privacy_status="private",
		publish_at=None,
		upload_status="uploaded",
		processing_status="succeeded",
	)
	monkeypatch.setattr(
		"pipeline.stages.upload.YouTubeClient", lambda **kw: FakeClient(status=status, **kw)
	)
	make_bundle(ctx)

	result = UploadStage(ctx).run()
	assert any("compliance audit" in w for w in result.warnings)


# --- client: retries, resume, polling ----------------------------------------


class FakeHttpError(Exception):
	def __init__(self, status: int):
		self.resp = types.SimpleNamespace(status=status)
		super().__init__(f"HTTP {status}")


class FakeInsertRequest:
	def __init__(self, steps):
		self.steps = list(steps)
		self.calls = 0

	def next_chunk(self):
		self.calls += 1
		step = self.steps.pop(0)
		if isinstance(step, Exception):
			raise step
		return step


class FakeService:
	def __init__(self, insert_steps=None, list_responses=None):
		self.insert_request = FakeInsertRequest(insert_steps or [])
		self.list_responses = list(list_responses or [])
		self.insert_kwargs: dict = {}
		self.thumbnail_calls = 0

	def videos(self):
		return self

	def thumbnails(self):
		return self

	def insert(self, **kwargs):
		self.insert_kwargs = kwargs
		return self.insert_request

	def list(self, **kwargs):
		return self

	def set(self, **kwargs):
		self.thumbnail_calls += 1
		return self

	def execute(self):
		if self.list_responses:
			step = self.list_responses.pop(0)
			if isinstance(step, Exception):
				raise step
			return step
		return {}


@pytest.fixture
def fake_google(monkeypatch):
	"""Satisfy the lazy `googleapiclient` imports without installing it."""
	http_mod = types.ModuleType("googleapiclient.http")

	class MediaFileUpload:
		def __init__(self, path, **kwargs):
			self.path = path
			self.kwargs = kwargs

	http_mod.MediaFileUpload = MediaFileUpload
	pkg = types.ModuleType("googleapiclient")
	pkg.http = http_mod
	monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
	monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)


def build_client(service, **kwargs) -> YouTubeClient:
	client = YouTubeClient(credentials=object(), sleep=lambda _: None, **kwargs)
	client._service = service
	return client


def test_upload_resumes_after_a_transient_error(fake_google, tmp_path):
	"""A 503 mid-transfer resumes the upload rather than restarting it."""
	video = tmp_path / "episode.mp4"
	video.write_bytes(b"\x00" * 16)
	service = FakeService(
		insert_steps=[
			(types.SimpleNamespace(progress=lambda: 0.5), None),
			FakeHttpError(503),
			(None, {"id": "abc"}),
		]
	)
	client = build_client(service)
	assert client.insert_video(video, {"snippet": {}, "status": {}}) == "abc"
	assert service.insert_request.calls == 3
	# `part` is derived from the body's top-level keys.
	assert service.insert_kwargs["part"] == "snippet,status"


def test_upload_gives_up_on_a_client_error(fake_google, tmp_path):
	"""400 means the metadata is wrong; retrying just wastes quota."""
	video = tmp_path / "episode.mp4"
	video.write_bytes(b"\x00" * 16)
	service = FakeService(insert_steps=[FakeHttpError(400)])
	with pytest.raises(YouTubeError, match=r"videos\.insert failed"):
		build_client(service).insert_video(video, {"snippet": {}, "status": {}})
	assert service.insert_request.calls == 1


def test_upload_stops_after_max_retries(fake_google, tmp_path):
	video = tmp_path / "episode.mp4"
	video.write_bytes(b"\x00" * 16)
	service = FakeService(insert_steps=[FakeHttpError(503)] * 5)
	with pytest.raises(YouTubeError):
		build_client(service, max_retries=2).insert_video(video, {"snippet": {}, "status": {}})
	assert service.insert_request.calls == 3  # initial + 2 retries


def test_missing_video_id_in_the_response_is_an_error(fake_google, tmp_path):
	video = tmp_path / "episode.mp4"
	video.write_bytes(b"\x00" * 16)
	service = FakeService(insert_steps=[(None, {})])
	with pytest.raises(YouTubeError, match="no video ID"):
		build_client(service).insert_video(video, {"snippet": {}, "status": {}})


def test_backoff_grows_and_is_capped():
	client = YouTubeClient(credentials=object(), sleep=lambda _: None)
	assert 1 <= client._backoff(0) <= 2
	assert 8 <= client._backoff(3) <= 9
	assert client._backoff(20) <= 65  # capped, not 2**20 seconds


def test_poll_returns_when_processing_succeeds():
	service = FakeService(
		list_responses=[
			{"items": [{"status": {"uploadStatus": "uploaded"}, "processingDetails": {}}]},
			{
				"items": [
					{
						"status": {
							"uploadStatus": "processed",
							"privacyStatus": "private",
							"publishAt": "2026-08-07T16:00:00Z",
						},
						"processingDetails": {"processingStatus": "succeeded"},
					}
				]
			},
		]
	)
	status = build_client(service).wait_until_processed("abc", interval_seconds=0)
	assert status.processing_status == "succeeded"
	assert status.publish_at == "2026-08-07T16:00:00Z"


def test_rejected_video_raises():
	service = FakeService(
		list_responses=[
			{
				"items": [
					{
						"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"},
						"processingDetails": {},
					}
				]
			}
		]
	)
	with pytest.raises(YouTubeError, match="copyright"):
		build_client(service).wait_until_processed("abc", interval_seconds=0)


def test_poll_forbidden_by_scope_is_a_warning_not_a_failure():
	"""`youtube.upload` is not documented to authorise videos.list. The upload has
	already succeeded by this point, so a 403 must not fail the run."""
	service = FakeService(list_responses=[FakeHttpError(403)])
	status = build_client(service).wait_until_processed("abc", interval_seconds=0)
	assert status.processing_status == "unverified"
	assert any("youtube.upload scope" in n for n in status.notes)


def test_retriable_statuses_are_the_transient_ones():
	assert set(RETRIABLE_STATUS) == {429, 500, 502, 503, 504}
	assert 403 not in RETRIABLE_STATUS and 400 not in RETRIABLE_STATUS


# --- wiring ------------------------------------------------------------------


def test_upload_is_the_last_stage_and_is_registered():
	from pipeline.cli import STAGES

	assert STAGE_ORDER[-1] == "upload"
	assert set(STAGE_ORDER) == set(STAGES)


def test_repo_config_ships_with_uploads_disabled():
	"""The audit is outstanding; the checked-in default must stay dry-run."""
	from pipeline.config import load_config

	assert load_config().upload.enabled is False
