"""SQLite storage for the home display tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection

from inky_contract import RenderSettings

from .config import Settings


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class DisplayRecord(Base):
    __tablename__ = "displays"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    orientation: Mapped[str] = mapped_column(String(16))
    rotation: Mapped[int] = mapped_column(Integer)
    time_zone: Mapped[str] = mapped_column(String(64))
    default_render_settings: Mapped[dict[str, object]] = mapped_column(JSON)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    desired_revision: Mapped[int] = mapped_column(Integer, default=0)
    desired_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    desired_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssetRecord(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    file_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    render_settings: Mapped[dict[str, object]] = mapped_column(
        JSON, default=lambda: RenderSettings().model_dump(mode="json")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_path: Mapped[str] = mapped_column(String(512), unique=True)
    renderer_version: Mapped[str] = mapped_column(String(64))
    render_settings: Mapped[dict[str, object]] = mapped_column(JSON)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    palette: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DisplayJobRecord(Base):
    __tablename__ = "display_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_id: Mapped[str] = mapped_column(ForeignKey("displays.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    album_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    render_settings: Mapped[dict[str, object]] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlbumRecord(Base):
    __tablename__ = "albums"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_id: Mapped[str] = mapped_column(ForeignKey("displays.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    order_mode: Mapped[str] = mapped_column(String(16), default="sequential")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=1_200)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_temporary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    time_zone: Mapped[str] = mapped_column(String(64))
    schedule_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    default_render_settings: Mapped[dict[str, object]] = mapped_column(JSON)
    next_item_index: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AlbumItemRecord(Base):
    __tablename__ = "album_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IcloudAlbumLinkRecord(Base):
    __tablename__ = "icloud_album_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), unique=True, index=True)
    # The token is a bearer capability: anyone holding it can read the whole album. It is
    # unique so the same album cannot be linked twice and have two syncs fight over the rows.
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    share_url: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    stream_name: Mapped[str] = mapped_column(String(255), default="")
    stream_ctag: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    photo_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class IcloudPhotoRecord(Base):
    __tablename__ = "icloud_photos"
    __table_args__ = (UniqueConstraint("link_id", "photo_guid", name="uq_icloud_photo_guid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    link_id: Mapped[str] = mapped_column(ForeignKey("icloud_album_links.id"), index=True)
    photo_guid: Mapped[str] = mapped_column(String(128))
    checksum: Mapped[str] = mapped_column(String(128), default="")
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_created: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # A tombstone, not a delete: the photo leaves the playlist but its asset stays in the gallery.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)


def _apply_sqlite_migrations(engine: Engine) -> None:
    """Add columns needed when an existing SQLite file is opened by newer code."""

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS activity"))
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(assets)"))}
        if "deleted_at" not in columns:
            connection.execute(text("ALTER TABLE assets ADD COLUMN deleted_at DATETIME"))
        job_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(display_jobs)"))}
        if "album_id" not in job_columns:
            connection.execute(text("ALTER TABLE display_jobs ADD COLUMN album_id VARCHAR(36)"))
        if "render_settings" not in columns:
            connection.execute(text("ALTER TABLE assets ADD COLUMN render_settings JSON"))
        album_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(albums)"))}
        if "is_temporary" not in album_columns:
            connection.execute(text("ALTER TABLE albums ADD COLUMN is_temporary BOOLEAN NOT NULL DEFAULT 0"))


def _configure_sqlite(engine: Engine) -> None:
    """Let the render worker, the album scheduler, and the iCloud sync write concurrently.

    The default rollback journal blocks readers during a write and fails immediately on a
    locked database, which surfaces as an album that silently stops advancing.
    """

    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def set_pragmas(connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
    engine = create_engine(settings.resolved_database_url, connect_args=connect_args)
    _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    _apply_sqlite_migrations(engine)
    return sessionmaker(engine, expire_on_commit=False)
