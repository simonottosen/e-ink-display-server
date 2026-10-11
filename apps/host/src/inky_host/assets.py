"""Shared asset ingest used by both the upload route and the iCloud sync."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from inky_contract import RenderSettings

from .database import AssetRecord
from .rendering import validate_source_image
from .storage import StoredUpload

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
