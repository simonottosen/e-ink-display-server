"""Local content-addressed storage for originals, previews, and display files."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class StorageError(Exception):
    """A stored file could not be written. The message is safe to show a user."""


class StorageTooLargeError(StorageError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    filename: str
    mime_type: str
    sha256: str
    file_size: int
    relative_path: str


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.originals = root / "originals"
        self.artifacts = root / "artifacts"
        self.tmp = root / "tmp"
        for directory in (self.originals, self.artifacts, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)

    def path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if self.root.resolve() not in candidate.parents:
            raise ValueError("storage path escapes the data directory")
        return candidate

    async def save_upload(self, upload: UploadFile, max_bytes: int) -> StoredUpload:
        mime_type = upload.content_type or ""
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Only JPEG, PNG, and WebP image uploads are supported",
            )

        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await upload.read(1024 * 1024):
                yield chunk

        try:
            return await self.save_stream(chunks(), upload.filename or "upload", mime_type, max_bytes)
        except StorageTooLargeError as error:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=str(error),
            ) from error
        finally:
            await upload.close()

    async def save_stream(
        self,
        chunks: AsyncIterator[bytes],
        filename: str,
        mime_type: str,
        max_bytes: int,
    ) -> StoredUpload:
        """Hash and store a streamed image, stopping as soon as it grows past the limit."""

        safe_filename = Path(filename).name[:255] or "upload"
        temporary_path = self.tmp / f"{uuid4()}.upload"
        digest = hashlib.sha256()
        file_size = 0

        try:
            with temporary_path.open("xb") as output:
                async for chunk in chunks:
                    file_size += len(chunk)
                    if file_size > max_bytes:
                        raise StorageTooLargeError(f"Image exceeds the {max_bytes // (1024 * 1024)} MiB limit")
                    digest.update(chunk)
                    output.write(chunk)

            sha256 = digest.hexdigest()
            relative_path = f"originals/{sha256[:2]}/{sha256}"
            destination = self.path(relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary_path.unlink()
            else:
                os.replace(temporary_path, destination)
            return StoredUpload(safe_filename, mime_type, sha256, file_size, relative_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def write_artifact(self, content: bytes, sha256: str) -> str:
        relative_path = f"artifacts/{sha256[:2]}/{sha256}.png"
        return self._write_derived(content, relative_path)

    def preview_path(self, artifact_sha256: str) -> Path:
        return self.path(f"artifacts/{artifact_sha256[:2]}/{artifact_sha256}.preview.png")

    def write_preview(self, content: bytes, artifact_sha256: str) -> str:
        relative_path = f"artifacts/{artifact_sha256[:2]}/{artifact_sha256}.preview.png"
        return self._write_derived(content, relative_path)

    def remove(self, relative_path: str) -> None:
        self._delete_path(relative_path).unlink(missing_ok=True)

    def remove_preview(self, artifact_sha256: str) -> None:
        self.remove(f"artifacts/{artifact_sha256[:2]}/{artifact_sha256}.preview.png")

    def _delete_path(self, relative_path: str) -> Path:
        """Return an unlink target confined to this storage volume.

        The final filename is deliberately not resolved: if it is unexpectedly
        a symlink, unlink removes the symlink rather than following it.
        """

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("storage path escapes the data directory")
        target = self.root / relative
        root = self.root.resolve()
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise ValueError("storage path escapes the data directory")
        return target

    def _write_derived(self, content: bytes, relative_path: str) -> str:
        destination = self.path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return relative_path

        temporary_path = self.tmp / f"{uuid4()}.artifact"
        try:
            with temporary_path.open("xb") as output:
                output.write(content)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return relative_path
