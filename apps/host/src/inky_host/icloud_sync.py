"""Keeps an album's playlist in step with a shared iCloud album."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .assets import PurgedFiles, UnsupportedImageError, purge_assets, upsert_asset, validate_stored_image
from .config import Settings
from .database import (
    AlbumItemRecord,
    AlbumRecord,
    AssetRecord,
    IcloudAlbumLinkRecord,
    IcloudPhotoRecord,
)
from .icloud import IcloudClient, IcloudPhoto, IcloudShareError, IcloudStream
from .storage import Storage, StorageError, StoredUpload

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
MAX_BACKOFF_SECONDS = 6 * 60 * 60
CONCURRENT_DOWNLOADS = 3
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LinkSnapshot:
    id: str
    token: str
    base_url: str
    stream_ctag: str
    photo_count: int


class IcloudSyncService:
    """Polls linked albums and rebuilds their playlists, one link at a time."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        storage: Storage,
        settings: Settings,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._settings = settings
        self._client_factory = client_factory or self._default_client
        self._task: asyncio.Task[None] | None = None

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._settings.icloud_request_timeout_seconds,
            headers={"Origin": "https://www.icloud.com"},
        )

    async def start(self) -> None:
        if not self._settings.icloud_sync_enabled:
            return
        self._task = asyncio.create_task(self._run(), name="inky-icloud-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                for link_id in await asyncio.to_thread(self._due_link_ids):
                    await self.sync_link(link_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("icloud sync iteration failed")
            await asyncio.sleep(TICK_SECONDS)

    async def sync_link(self, link_id: str, force: bool = False) -> None:
        snapshot = await asyncio.to_thread(self._snapshot, link_id)
        if snapshot is None:
            return
        try:
            async with self._client_factory() as client:
                icloud = IcloudClient(client, self._settings.icloud_guid_chunk_size)
                stream = await icloud.fetch_stream(snapshot.token, snapshot.base_url or None)
                # An album turned private, or a transient error served as an empty body, must
                # never be allowed to wipe a playlist that is currently driving the panel.
                if not stream.photos and snapshot.photo_count:
                    raise IcloudShareError("The album came back empty, so the playlist was left alone")
                if stream.stream_ctag and stream.stream_ctag == snapshot.stream_ctag and not force:
                    await asyncio.to_thread(self._record_success, link_id, stream, rebuild=False)
                    return
                pending = await asyncio.to_thread(self._record_stream, link_id, stream)
                await self._download_pending(icloud, link_id, stream, pending)
                await asyncio.to_thread(self._record_success, link_id, stream, rebuild=True)
        except (IcloudShareError, httpx.HTTPError) as error:
            message = str(error) if isinstance(error, IcloudShareError) else "The album could not be reached"
            logger.warning("icloud sync failed: link=%s reason=%s", link_id, message)
            await asyncio.to_thread(self._record_failure, link_id, message)

    async def _download_pending(
        self,
        icloud: IcloudClient,
        link_id: str,
        stream: IcloudStream,
        pending: list[IcloudPhoto],
    ) -> None:
        if not pending:
            return
        urls = await icloud.fetch_asset_urls(stream.base_url, [photo.photo_guid for photo in pending])
        limit = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

        async def download(photo: IcloudPhoto) -> None:
            async with limit:
                derivative = photo.best_derivative()
                url = urls.get(derivative.checksum) if derivative else None
                if url is None:
                    await asyncio.to_thread(
                        self._record_photo_error, link_id, photo.photo_guid, "No download link was offered"
                    )
                    return
                try:
                    await self._download_photo(icloud, link_id, photo, url)
                except (IcloudShareError, StorageError, UnsupportedImageError) as error:
                    await asyncio.to_thread(self._record_photo_error, link_id, photo.photo_guid, str(error))
                except httpx.HTTPError:
                    # One photo losing its connection must not discard the whole pass; the
                    # album may hold hundreds, and this guid is simply retried next cycle.
                    await asyncio.to_thread(
                        self._record_photo_error, link_id, photo.photo_guid, "The photo could not be downloaded"
                    )

        await asyncio.gather(*(download(photo) for photo in pending))

    async def _download_photo(self, icloud: IcloudClient, link_id: str, photo: IcloudPhoto, url: str) -> None:
        max_bytes = self._settings.max_upload_bytes
        stored = await self._storage.save_stream(
            icloud.stream_download(url, max_bytes), photo.photo_guid, "application/octet-stream", max_bytes
        )
        path = self._storage.path(stored.relative_path)
        mime_type, width, height = await asyncio.to_thread(
            validate_stored_image, path, self._settings.max_source_pixels
        )
        name = (photo.caption or photo.photo_guid).strip() or photo.photo_guid
        stored = replace(stored, mime_type=mime_type, filename=f"{name}{EXTENSIONS[mime_type]}")
        await asyncio.to_thread(self._attach_asset, link_id, photo.photo_guid, stored, width, height)

    def _due_link_ids(self) -> list[str]:
        now = utc_now()
        with self._sessions() as session:
            links = session.scalars(
                select(IcloudAlbumLinkRecord).where(IcloudAlbumLinkRecord.enabled.is_(True))
            ).all()
            due = [(link.id, _as_utc(link.next_sync_at)) for link in links]
            return [link_id for link_id, next_sync_at in due if next_sync_at is None or next_sync_at <= now]

    def _snapshot(self, link_id: str) -> LinkSnapshot | None:
        with self._sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                return None
            link.last_sync_at = utc_now()
            session.commit()
            return LinkSnapshot(link.id, link.token, link.base_url, link.stream_ctag, link.photo_count)

    def _record_stream(self, link_id: str, stream: IcloudStream) -> list[IcloudPhoto]:
        """Reconcile the photo rows against the album, and report which still need bytes."""

        now = utc_now()
        photos = stream.photos[: self._settings.icloud_max_photos]
        seen = {photo.photo_guid: photo for photo in photos}
        pending: list[IcloudPhoto] = []
        with self._sessions() as session:
            existing = {
                record.photo_guid: record
                for record in session.scalars(
                    select(IcloudPhotoRecord).where(IcloudPhotoRecord.link_id == link_id)
                ).all()
            }
            for guid, photo in seen.items():
                derivative = photo.best_derivative()
                record = existing.get(guid)
                if record is None:
                    record = IcloudPhotoRecord(id=str(uuid4()), link_id=link_id, photo_guid=guid)
                    session.add(record)
                record.checksum = derivative.checksum if derivative else ""
                record.caption = photo.caption
                record.date_created = photo.date_created
                record.last_seen_at = now
                record.removed_at = None
                if record.asset_id is None:
                    pending.append(photo)
            for guid, record in existing.items():
                if guid not in seen and record.removed_at is None:
                    record.removed_at = now
            session.commit()
        return pending

    def _attach_asset(self, link_id: str, guid: str, stored: StoredUpload, width: int, height: int) -> None:
        with self._sessions() as session:
            record = session.scalar(
                select(IcloudPhotoRecord).where(
                    IcloudPhotoRecord.link_id == link_id, IcloudPhotoRecord.photo_guid == guid
                )
            )
            if record is None:
                return
            try:
                asset = upsert_asset(session, stored, width, height)
                session.flush()
            except IntegrityError:
                # Two photos in the album held identical bytes and raced to insert the
                # one content-addressed asset they share. The winner's row is the answer.
                session.rollback()
                asset = session.scalar(select(AssetRecord).where(AssetRecord.sha256 == stored.sha256))
                record = session.scalar(
                    select(IcloudPhotoRecord).where(
                        IcloudPhotoRecord.link_id == link_id, IcloudPhotoRecord.photo_guid == guid
                    )
                )
                if asset is None or record is None:
                    return
            record.asset_id = asset.id
            record.download_error = None
            session.commit()

    def _record_photo_error(self, link_id: str, guid: str, message: str) -> None:
        with self._sessions() as session:
            record = session.scalar(
                select(IcloudPhotoRecord).where(
                    IcloudPhotoRecord.link_id == link_id, IcloudPhotoRecord.photo_guid == guid
                )
            )
            if record is None:
                return
            record.download_error = message
            session.commit()

    def _record_success(self, link_id: str, stream: IcloudStream, rebuild: bool) -> None:
        now = utc_now()
        purged = PurgedFiles((), ())
        with self._sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                return
            if rebuild:
                link.photo_count = self._rebuild_playlist(session, link)
                purged = self._purge_removed_photos(session, link)
            link.base_url = stream.base_url
            link.stream_name = stream.stream_name
            link.stream_ctag = stream.stream_ctag
            link.last_error = None
            link.consecutive_failures = 0
            link.last_success_at = now
            link.next_sync_at = now + timedelta(seconds=link.sync_interval_seconds)
            session.commit()
        # Only once the rows are committed, so a crash leaves an unreferenced file rather
        # than a row pointing at bytes that are gone.
        try:
            purged.remove_from(self._storage)
        except (OSError, ValueError):
            logger.warning("could not remove files for photos dropped from the album: link=%s", link_id)

    def _purge_removed_photos(self, session: Session, link: IcloudAlbumLinkRecord) -> PurgedFiles:
        """Delete the photos the shared album no longer holds, so the gallery mirrors it.

        Assets are content addressed, so the bytes may be shared with a photo that is still
        in a linked album or with a playlist built by hand. Those keep the asset alive; only
        the ones nothing else refers to are deleted.
        """

        removed = session.scalars(
            select(IcloudPhotoRecord).where(
                IcloudPhotoRecord.link_id == link.id, IcloudPhotoRecord.removed_at.is_not(None)
            )
        ).all()
        if not removed:
            return PurgedFiles((), ())

        candidates = {record.asset_id for record in removed if record.asset_id is not None}
        for record in removed:
            session.delete(record)
        session.flush()

        still_wanted = set(
            session.scalars(
                select(IcloudPhotoRecord.asset_id).where(IcloudPhotoRecord.asset_id.in_(candidates))
            ).all()
        ) | set(
            session.scalars(
                select(AlbumItemRecord.asset_id).where(AlbumItemRecord.asset_id.in_(candidates))
            ).all()
        )
        return purge_assets(session, sorted(candidates - still_wanted))

    def _rebuild_playlist(self, session: Session, link: IcloudAlbumLinkRecord) -> int:
        """Replace the album's items in one transaction.

        The scheduler stops an album the moment it sees an empty playlist, so the delete and
        the insert must never be committed apart.
        """

        records = session.scalars(
            select(IcloudPhotoRecord)
            .join(AssetRecord, AssetRecord.id == IcloudPhotoRecord.asset_id)
            .where(
                IcloudPhotoRecord.link_id == link.id,
                IcloudPhotoRecord.removed_at.is_(None),
                AssetRecord.deleted_at.is_(None),
            )
        ).all()
        ordered = sorted(records, key=lambda record: (record.date_created or "", record.first_seen_at))
        session.execute(delete(AlbumItemRecord).where(AlbumItemRecord.album_id == link.album_id))
        for position, record in enumerate(ordered):
            session.add(
                AlbumItemRecord(
                    id=str(uuid4()),
                    album_id=link.album_id,
                    asset_id=record.asset_id,
                    position=position,
                )
            )
        album = session.get(AlbumRecord, link.album_id)
        if album is not None:
            album.updated_at = utc_now()
        return len(ordered)

    def _record_failure(self, link_id: str, message: str) -> None:
        now = utc_now()
        with self._sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                return
            link.consecutive_failures += 1
            link.last_error = message
            backoff = min(link.sync_interval_seconds * 2**link.consecutive_failures, MAX_BACKOFF_SECONDS)
            link.next_sync_at = now + timedelta(seconds=backoff)
            session.commit()


def _as_utc(value: datetime | None) -> datetime | None:
    """Restore UTC on timestamps loaded from SQLite, which stores no offset."""

    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
