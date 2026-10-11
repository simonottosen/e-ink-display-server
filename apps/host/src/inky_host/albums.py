"""Albums and the single-process album runner."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inky_contract import RenderSettings

from .api_models import AlbumItemResponse, AlbumOrderMode, AlbumResponse, AssetResponse
from .database import (
    AlbumItemRecord,
    AlbumRecord,
    AssetRecord,
    DisplayJobRecord,
    DisplayRecord,
)
from .worker import JobStatus, RenderWorker, create_display_now_job

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Restore UTC on timestamps loaded from SQLite, which stores no offset."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def asset_response(asset: AssetRecord) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        original_filename=asset.original_filename,
        sha256=asset.sha256,
        mime_type=asset.mime_type,
        file_size=asset.file_size,
        width=asset.width,
        height=asset.height,
        created_at=asset.created_at,
        deleted_at=asset.deleted_at,
        preview_url=f"/api/v1/assets/{asset.id}/preview",
        original_url=f"/api/v1/assets/{asset.id}/original",
        render_settings=RenderSettings.model_validate(asset.render_settings or {}),
    )


def album_response(session: Session, album: AlbumRecord) -> AlbumResponse:
    items = session.scalars(
        select(AlbumItemRecord).where(AlbumItemRecord.album_id == album.id).order_by(AlbumItemRecord.position)
    ).all()
    responses: list[AlbumItemResponse] = []
    for item in items:
        asset = session.get(AssetRecord, item.asset_id)
        if asset is not None:
            responses.append(
                AlbumItemResponse(
                    id=item.id,
                    asset_id=item.asset_id,
                    position=item.position,
                    asset=asset_response(asset),
                )
            )
    return AlbumResponse(
        id=album.id,
        display_id=album.display_id,
        name=album.name,
        order_mode=AlbumOrderMode(album.order_mode),
        interval_seconds=album.interval_seconds,
        enabled=album.enabled,
        is_running=album.is_running,
        time_zone=album.time_zone,
        schedule_start_at=as_utc(album.schedule_start_at),
        schedule_end_at=as_utc(album.schedule_end_at),
        next_item_index=album.next_item_index,
        next_run_at=as_utc(album.next_run_at),
        items=responses,
        created_at=album.created_at,
        updated_at=album.updated_at,
    )


class AlbumScheduler:
    """Queues one album item at a time; physical refreshes remain worker-serialized."""

    def __init__(self, sessions: sessionmaker[Session], worker: RenderWorker) -> None:
        self._sessions = sessions
        self._worker = worker
        self._task: asyncio.Task[None] | None = None
        self._random = random.SystemRandom()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="inky-album-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_album(self, album_id: str) -> str | None:
        job_id = await asyncio.to_thread(self._start_and_queue, album_id)
        if job_id is not None:
            await self._worker.enqueue(job_id)
        return job_id

    async def stop_album(self, album_id: str) -> None:
        await asyncio.to_thread(self._stop_album, album_id)

    async def _run(self) -> None:
        while True:
            try:
                job_ids = await asyncio.to_thread(self._queue_due_items)
                for job_id in job_ids:
                    await self._worker.enqueue(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("album scheduler iteration failed")
            await asyncio.sleep(2)

    def _start_and_queue(self, album_id: str) -> str | None:
        with self._sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                return None
            stop_running_albums(session, album.display_id)
            album.is_running = True
            album.enabled = True
            now = utc_now()
            schedule_start_at = as_utc(album.schedule_start_at)
            schedule_end_at = as_utc(album.schedule_end_at)
            if schedule_end_at and now >= schedule_end_at:
                album.is_running = False
                session.commit()
                return None
            if schedule_start_at and now < schedule_start_at:
                album.next_run_at = schedule_start_at
                job_id = None
            else:
                job_id = self._queue_next_item(session, album, now)
            session.commit()
            return job_id

    def _stop_album(self, album_id: str) -> None:
        with self._sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                return
            stop_album_record(session, album)
            session.commit()

    def _queue_due_items(self) -> list[str]:
        now = utc_now()
        job_ids: list[str] = []
        with self._sessions() as session:
            albums = session.scalars(
                select(AlbumRecord).where(AlbumRecord.is_running.is_(True), AlbumRecord.enabled.is_(True))
            ).all()
            for album in albums:
                schedule_start_at = as_utc(album.schedule_start_at)
                schedule_end_at = as_utc(album.schedule_end_at)
                next_run_at = as_utc(album.next_run_at)
                if schedule_start_at and now < schedule_start_at:
                    continue
                if schedule_end_at and now >= schedule_end_at:
                    album.is_running = False
                    album.next_run_at = None
                    continue
                if next_run_at is None or next_run_at <= now:
                    job_id = self._queue_next_item(session, album, now)
                    if job_id is not None:
                        job_ids.append(job_id)
            session.commit()
        return job_ids

    def _queue_next_item(self, session: Session, album: AlbumRecord, now: datetime) -> str | None:
        valid_items = session.scalars(
            select(AlbumItemRecord)
            .join(AssetRecord, AssetRecord.id == AlbumItemRecord.asset_id)
            .where(AlbumItemRecord.album_id == album.id, AssetRecord.deleted_at.is_(None))
            .order_by(AlbumItemRecord.position)
        ).all()
        if not valid_items:
            album.is_running = False
            return None

        if album.order_mode == AlbumOrderMode.SHUFFLE:
            item = self._random.choice(valid_items)
        else:
            item = valid_items[album.next_item_index % len(valid_items)]
            album.next_item_index = (album.next_item_index + 1) % len(valid_items)

        display = session.get(DisplayRecord, album.display_id)
        asset = session.get(AssetRecord, item.asset_id)
        if display is None or asset is None:
            return None
        settings = RenderSettings.model_validate(asset.render_settings or display.default_render_settings)
        job = create_display_now_job(session, display, asset, settings, album_id=album.id)
        album.next_run_at = now + timedelta(seconds=album.interval_seconds)
        return job.id


def stop_running_albums(session: Session, display_id: str) -> list[AlbumRecord]:
    """Stop every active album for a display and cancel its unfinished jobs."""

    albums = session.scalars(
        select(AlbumRecord).where(
            AlbumRecord.display_id == display_id,
            AlbumRecord.is_running.is_(True),
        )
    ).all()
    for album in albums:
        stop_album_record(session, album)
    return albums


def stop_album_record(session: Session, album: AlbumRecord) -> None:
    """Stop one album and make its not-yet-started display work obsolete."""

    album.is_running = False
    album.next_run_at = None

    unfinished = session.scalars(
        select(DisplayJobRecord).where(
            DisplayJobRecord.album_id == album.id,
            DisplayJobRecord.status.in_([JobStatus.QUEUED, JobStatus.RENDERING, JobStatus.READY]),
        )
    ).all()
    unfinished_ids = {job.id for job in unfinished}
    for job in unfinished:
        job.status = JobStatus.SUPERSEDED

    display = session.get(DisplayRecord, album.display_id)
    if display is not None and display.desired_job_id in unfinished_ids:
        display.desired_job_id = None
        display.desired_artifact_id = None
