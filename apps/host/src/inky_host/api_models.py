"""Host-only HTTP response and settings models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from inky_contract import DisplayOrientation, DisplayRotation, RenderSettings


class AssetResponse(BaseModel):
    id: str
    original_filename: str
    sha256: str
    mime_type: str
    file_size: int
    width: int | None
    height: int | None
    created_at: datetime
    deleted_at: datetime | None
    preview_url: str | None = None
    original_url: str | None = None
    render_settings: RenderSettings


class AssetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_settings: RenderSettings


class JobResponse(BaseModel):
    id: str
    status: str
    revision: int | None
    asset_id: str
    artifact_sha256: str | None = None
    preview_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DisplaySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    orientation: DisplayOrientation | None = None
    rotation: DisplayRotation | None = None
    default_render_settings: RenderSettings | None = None


class DisplayResponse(BaseModel):
    id: str
    name: str
    width: int = 800
    height: int = 480
    orientation: DisplayOrientation
    rotation: DisplayRotation
    time_zone: str
    current_revision: int
    desired_revision: int
    desired_job_id: str | None
    last_seen_at: datetime | None
    last_error: str | None
    current_asset_id: str | None = None
    current_asset_filename: str | None = None
    current_preview_url: str | None = None
    current_album_id: str | None = None
    current_album_name: str | None = None
    requested_asset_id: str | None = None
    requested_asset_filename: str | None = None
    requested_preview_url: str | None = None
    requested_album_id: str | None = None
    requested_album_name: str | None = None
    active_album_name: str | None = None
    default_render_settings: RenderSettings


class AlbumOrderMode(StrEnum):
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"


class AlbumItemInput(BaseModel):
    asset_id: UUID


class AlbumCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    display_id: str = "inky-main"
    asset_ids: list[UUID] = Field(min_length=1)
    order_mode: AlbumOrderMode = AlbumOrderMode.SEQUENTIAL
    interval_seconds: int = Field(default=1_200, ge=60, le=86_400)
    enabled: bool = True
    time_zone: str = Field(default="Europe/Copenhagen", min_length=1, max_length=64)
    schedule_start_at: datetime | None = None
    schedule_end_at: datetime | None = None

    @model_validator(mode="after")
    def validate_schedule_window(self) -> AlbumCreate:
        if self.schedule_start_at and self.schedule_end_at and self.schedule_end_at <= self.schedule_start_at:
            raise ValueError("schedule_end_at must be after schedule_start_at")
        return self


class AlbumUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    order_mode: AlbumOrderMode | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    enabled: bool | None = None
    time_zone: str | None = Field(default=None, min_length=1, max_length=64)
    schedule_start_at: datetime | None = None
    schedule_end_at: datetime | None = None

    @model_validator(mode="after")
    def validate_complete_schedule_window(self) -> AlbumUpdate:
        if self.schedule_start_at and self.schedule_end_at and self.schedule_end_at <= self.schedule_start_at:
            raise ValueError("schedule_end_at must be after schedule_start_at")
        return self


class AlbumItemsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[UUID] = Field(min_length=1)


class AlbumItemResponse(BaseModel):
    id: str
    asset_id: str
    position: int
    asset: AssetResponse


class AlbumResponse(BaseModel):
    id: str
    display_id: str
    name: str
    order_mode: AlbumOrderMode
    interval_seconds: int
    enabled: bool
    is_running: bool
    time_zone: str
    schedule_start_at: datetime | None
    schedule_end_at: datetime | None
    next_item_index: int
    next_run_at: datetime | None
    items: list[AlbumItemResponse]
    created_at: datetime
    updated_at: datetime


class BulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[UUID] = Field(min_length=1, max_length=100)


class QuickPlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[UUID] = Field(min_length=1, max_length=100)
    order_mode: AlbumOrderMode = AlbumOrderMode.SEQUENTIAL
    interval_seconds: int = Field(default=1_200, ge=60, le=86_400)


class DeleteResult(BaseModel):
    deleted_ids: list[str]


class IcloudLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share_url: str = Field(min_length=1, max_length=2048)
    name: str | None = Field(default=None, max_length=120)
    display_id: str = "inky-main"
    interval_seconds: int = Field(default=1_200, ge=60, le=86_400)
    sync_interval_seconds: int = Field(default=900, ge=60, le=86_400)


class IcloudLinkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    sync_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)


class IcloudLinkResponse(BaseModel):
    """Deliberately carries neither the token nor the share URL.

    The token is a bearer capability for the whole album, and this API is unauthenticated.
    """

    id: str
    album_id: str
    stream_name: str
    enabled: bool
    sync_interval_seconds: int
    photo_count: int
    last_sync_at: datetime | None
    last_success_at: datetime | None
    next_sync_at: datetime | None
    last_error: str | None
    created_at: datetime

