"""Shared asset ingest and removal used by both the gallery routes and the iCloud sync."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from inky_contract import RenderSettings

from .database import (
    AlbumItemRecord,
    AlbumRecord,
    ArtifactRecord,
    AssetRecord,
    DisplayJobRecord,
    DisplayRecord,
    IcloudPhotoRecord,
)
from .rendering import validate_source_image
from .storage import Storage, StoredUpload

# A shared album also holds videos, and a failed request body is served as HTML. The
# remote Content-Type is a claim, so the first bytes decide what we actually stored.
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class UnsupportedImageError(Exception):
    """Downloaded bytes are not an image we can render. The message is safe to show a user."""


def sniff_image_type(path: Path) -> str:
    header = path.open("rb").read(16)
    for prefix, mime_type in _MAGIC_PREFIXES:
        if header.startswith(prefix):
            return mime_type
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    raise UnsupportedImageError("That file is not a JPEG, PNG, or WebP image")


def validate_stored_image(path: Path, max_pixels: int) -> tuple[str, int, int]:
    """Confirm the stored bytes decode as a supported image before a row refers to them."""

    mime_type = sniff_image_type(path)
    try:
        width, height = validate_source_image(path, max_pixels)
    except UnsupportedImageError:
        raise
    except Exception as error:
        raise UnsupportedImageError(f"That image could not be read ({error})") from error
    return mime_type, width, height


def upsert_asset(
    session: Session,
    stored: StoredUpload,
    width: int | None = None,
    height: int | None = None,
) -> AssetRecord:
    """Return the asset for these bytes, reviving a soft-deleted one rather than duplicating it."""

    asset = session.scalar(select(AssetRecord).where(AssetRecord.sha256 == stored.sha256))
    if asset is None:
        asset = AssetRecord(
            id=str(uuid4()),
            original_filename=stored.filename,
            sha256=stored.sha256,
            storage_path=stored.relative_path,
            mime_type=stored.mime_type,
            file_size=stored.file_size,
            width=width,
            height=height,
            render_settings=RenderSettings().model_dump(mode="json"),
        )
        session.add(asset)
    elif asset.deleted_at is not None:
        asset.deleted_at = None
    return asset


@dataclass(frozen=True)
class PurgedFiles:
    """Files whose rows are gone, to be unlinked once the transaction is committed."""

    storage_paths: tuple[str, ...]
    artifact_previews: tuple[str, ...]

    def remove_from(self, storage: Storage) -> None:
        for relative_path in self.storage_paths:
            storage.remove(relative_path)
        for sha256 in self.artifact_previews:
            storage.remove_preview(sha256)


def purge_assets(session: Session, asset_ids: Sequence[str]) -> PurgedFiles:
    """Delete assets and everything pointing at them, reporting the files left behind.

    Rendered artifacts, queued jobs, and playlist entries all refer to an asset, and a
    display may be parked on one of those artifacts, so each has to be detached here.
    """

    unique_ids = list(dict.fromkeys(asset_ids))
    if not unique_ids:
        return PurgedFiles((), ())

    assets = [asset for asset in (session.get(AssetRecord, id_) for id_ in unique_ids) if asset is not None]
    jobs = session.scalars(select(DisplayJobRecord).where(DisplayJobRecord.asset_id.in_(unique_ids))).all()
    artifacts = session.scalars(select(ArtifactRecord).where(ArtifactRecord.asset_id.in_(unique_ids))).all()
    job_ids = {job.id for job in jobs}
    artifact_ids = {artifact.id for artifact in artifacts}

    for display in session.scalars(select(DisplayRecord)).all():
        if display.desired_job_id in job_ids or display.desired_artifact_id in artifact_ids:
            display.desired_job_id = None
            display.desired_artifact_id = None

    affected_album_ids = set(
        session.scalars(select(AlbumItemRecord.album_id).where(AlbumItemRecord.asset_id.in_(unique_ids))).all()
    )
    session.execute(delete(AlbumItemRecord).where(AlbumItemRecord.asset_id.in_(unique_ids)))
    for album_id in affected_album_ids:
        album = session.get(AlbumRecord, album_id)
        has_items = session.scalar(select(AlbumItemRecord.id).where(AlbumItemRecord.album_id == album_id))
        if album is not None and not has_items:
            album.is_running = False
            album.next_run_at = None

    # A linked photo keeps pointing at bytes that no longer exist. Clearing the pointer lets
    # the next sync fetch the photo again, which is what the shared album still asks for.
    session.execute(
        update(IcloudPhotoRecord)
        .where(IcloudPhotoRecord.asset_id.in_(unique_ids))
        .values(asset_id=None, download_error=None)
    )

    session.execute(delete(DisplayJobRecord).where(DisplayJobRecord.asset_id.in_(unique_ids)))
    session.execute(delete(ArtifactRecord).where(ArtifactRecord.asset_id.in_(unique_ids)))
    for asset in assets:
        session.delete(asset)

    return PurgedFiles(
        storage_paths=tuple([asset.storage_path for asset in assets] + [a.storage_path for a in artifacts]),
        artifact_previews=tuple(artifact.sha256 for artifact in artifacts),
    )
