"""FastAPI application for the single-display host vertical slice."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, desc, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint

from inky_contract import (
    AgentHeartbeat,
    ArtifactDescriptor,
    ArtifactFormat,
    DesiredState,
    DisplayNowRequest,
    DisplayOrientation,
    DisplayRotation,
    JobAcknowledgement,
    JobEvent,
    RenderSettings,
)

from .albums import AlbumScheduler, album_response, stop_album_record, stop_running_albums
from .api_models import (
    AlbumCreate,
    AlbumItemsUpdate,
    AlbumOrderMode,
    AlbumResponse,
    AlbumUpdate,
    AssetResponse,
    AssetUpdate,
    BulkDeleteRequest,
    DeleteResult,
    DisplayResponse,
    DisplaySettingsUpdate,
    IcloudLinkCreate,
    IcloudLinkResponse,
    IcloudLinkUpdate,
    JobResponse,
    QuickPlayRequest,
)
from .assets import purge_assets, upsert_asset
from .config import Settings
from .database import (
    AlbumItemRecord,
    AlbumRecord,
    ArtifactRecord,
    AssetRecord,
    DisplayJobRecord,
    DisplayRecord,
    IcloudAlbumLinkRecord,
    IcloudPhotoRecord,
    create_session_factory,
)
from .icloud import IcloudShareError, parse_share_token
from .icloud_sync import IcloudSyncService
from .observability import configure_logging, request_id_context
from .profiles import profile_from_record
from .rendering import artifact_cache_key
from .storage import Storage
from .worker import JobStatus, RenderWorker, create_display_now_job

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def _unique_asset_ids(asset_ids: list[UUID]) -> list[str]:
    """Keep the first occurrence of each image while preserving its order."""

    return list(dict.fromkeys(str(asset_id) for asset_id in asset_ids))


def _as_utc(value: datetime | None) -> datetime | None:
    """Restore SQLite's lost UTC offset before sending timestamps to browsers."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _asset_response(asset: AssetRecord, preview_url: str | None = None) -> AssetResponse:
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
        preview_url=preview_url or f"/api/v1/assets/{asset.id}/preview",
        original_url=f"/api/v1/assets/{asset.id}/original",
        render_settings=RenderSettings.model_validate(asset.render_settings or {}),
    )


def _delete_assets(asset_ids: list[str], request: Request) -> DeleteResult:
    sessions: sessionmaker[Session] = request.app.state.sessions
    storage: Storage = request.app.state.storage
    with sessions() as session:
        unique_ids = list(dict.fromkeys(asset_ids))
        assets = {asset_id: session.get(AssetRecord, asset_id) for asset_id in unique_ids}
        missing = [asset_id for asset_id, asset in assets.items() if asset is None]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more assets were not found")

        purged = purge_assets(session, unique_ids)
        try:
            purged.remove_from(storage)
        except (OSError, ValueError) as error:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not permanently remove all image files",
            ) from error
        session.commit()
    return DeleteResult(deleted_ids=unique_ids)


def _job_response(session: Session, job: DisplayJobRecord) -> JobResponse:
    artifact = session.get(ArtifactRecord, job.artifact_id) if job.artifact_id else None
    return JobResponse(
        id=job.id,
        status=job.status,
        revision=job.revision,
        asset_id=job.asset_id,
        artifact_sha256=artifact.sha256 if artifact else None,
        preview_url=f"/api/v1/jobs/{job.id}/preview" if artifact else None,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _display_response(session: Session, display: DisplayRecord) -> DisplayResponse:
    current_job = session.scalar(
        select(DisplayJobRecord)
        .where(
            DisplayJobRecord.display_id == display.id,
            DisplayJobRecord.revision == display.current_revision,
            DisplayJobRecord.status == JobStatus.COMPLETED,
        )
        .order_by(desc(DisplayJobRecord.completed_at))
    )
    requested_job = session.get(DisplayJobRecord, display.desired_job_id) if display.desired_job_id else None
    current_asset = session.get(AssetRecord, current_job.asset_id) if current_job else None
    requested_asset = session.get(AssetRecord, requested_job.asset_id) if requested_job else None
    current_album = _job_album(session, current_job)
    requested_album = _job_album(session, requested_job)
    active_album = session.scalar(
        select(AlbumRecord).where(AlbumRecord.display_id == display.id, AlbumRecord.is_running.is_(True))
    )
    return DisplayResponse(
        id=display.id,
        name=display.name,
        orientation=DisplayOrientation(display.orientation),
        rotation=DisplayRotation(display.rotation),
        time_zone=display.time_zone,
        current_revision=display.current_revision,
        desired_revision=display.desired_revision,
        desired_job_id=display.desired_job_id,
        last_seen_at=_as_utc(display.last_seen_at),
        last_error=display.last_error,
        current_asset_id=current_asset.id if current_asset else None,
        current_asset_filename=current_asset.original_filename if current_asset else None,
        current_preview_url=f"/api/v1/jobs/{current_job.id}/preview" if current_job else None,
        current_album_id=current_album.id if current_album else None,
        current_album_name=_album_label(current_album),
        requested_asset_id=requested_asset.id if requested_asset else None,
        requested_asset_filename=requested_asset.original_filename if requested_asset else None,
        requested_preview_url=(
            f"/api/v1/jobs/{requested_job.id}/preview" if requested_job and requested_job.artifact_id else None
        ),
        requested_album_id=requested_album.id if requested_album else None,
        requested_album_name=_album_label(requested_album),
        active_album_name=_album_label(active_album),
        default_render_settings=RenderSettings.model_validate(display.default_render_settings),
    )


def _unique_album_name(session: Session, preferred: str, keep_id: str | None = None) -> str:
    """Album names are unique, and two shared albums are both plausibly called "Family"."""

    base = preferred.strip()[:110] or "Shared album"
    for suffix in range(100):
        candidate = base if suffix == 0 else f"{base} ({suffix + 1})"
        clash = session.scalar(select(AlbumRecord).where(AlbumRecord.name == candidate))
        if clash is None or clash.id == keep_id:
            return candidate
    return f"{base} {uuid4().hex[:8]}"


def _icloud_link_response(link: IcloudAlbumLinkRecord) -> IcloudLinkResponse:
    return IcloudLinkResponse(
        id=link.id,
        album_id=link.album_id,
        stream_name=link.stream_name,
        enabled=link.enabled,
        sync_interval_seconds=link.sync_interval_seconds,
        photo_count=link.photo_count,
        last_sync_at=_as_utc(link.last_sync_at),
        last_success_at=_as_utc(link.last_success_at),
        next_sync_at=_as_utc(link.next_sync_at),
        last_error=link.last_error,
        created_at=link.created_at,
    )


def _icloud_link_response_by_id(sessions: sessionmaker[Session], link_id: str) -> IcloudLinkResponse:
    with sessions() as session:
        link = session.get(IcloudAlbumLinkRecord, link_id)
        if link is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked album not found")
        return _icloud_link_response(link)


def _report_stale_agent_revision(session: Session, display: DisplayRecord, agent_revision: int) -> None:
    """Surface a revision counter left over from an earlier database, which is otherwise silent."""

    message = (
        f"The display agent reported revision {agent_revision}, which this host has never issued "
        f"(the latest is {display.desired_revision}). Its saved state is left over from an earlier "
        "host database, so the agent will ignore new images. Delete state.json in the agent data "
        "directory and restart the agent."
    )
    # Agents poll every few seconds; only record a change so the log and the dashboard
    # do not fill with repeats of a condition that is already reported.
    if display.last_error == message:
        return
    logger.warning(message)
    display.last_error = message
    session.commit()


def _job_album(session: Session, job: DisplayJobRecord | None) -> AlbumRecord | None:
    if job is None:
        return None
    return session.get(AlbumRecord, job.album_id) if job.album_id else None


def _album_label(album: AlbumRecord | None) -> str | None:
    if album is None:
        return None
    return "Selected images" if album.is_temporary else album.name


def _seed_display(sessions: sessionmaker[Session], settings: Settings) -> None:
    profile = settings.initial_display_profile()
    with sessions() as session:
        if session.get(DisplayRecord, profile.id) is not None:
            return
        session.add(
            DisplayRecord(
                id=profile.id,
                name=profile.name,
                orientation=profile.orientation.value,
                rotation=int(profile.rotation),
                time_zone=profile.time_zone,
                default_render_settings=profile.default_render_settings.model_dump(mode="json"),
            )
        )
        session.commit()


def create_app(
    settings: Settings | None = None,
    icloud_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    configure_logging(runtime_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sessions = create_session_factory(runtime_settings)
        storage = Storage(runtime_settings.data_dir)
        _seed_display(sessions, runtime_settings)
        worker = RenderWorker(sessions, storage, runtime_settings.max_source_pixels)
        album_scheduler = AlbumScheduler(sessions, worker)
        icloud_sync = IcloudSyncService(sessions, storage, runtime_settings, icloud_client_factory)
        app.state.settings = runtime_settings
        app.state.sessions = sessions
        app.state.storage = storage
        app.state.worker = worker
        app.state.album_scheduler = album_scheduler
        app.state.icloud_sync = icloud_sync
        await worker.start()
        await album_scheduler.start()
        await icloud_sync.start()
        logger.info("host started", extra={"display_id": runtime_settings.display_id})
        try:
            yield
        finally:
            await icloud_sync.stop()
            await album_scheduler.stop()
            await worker.stop()
            logger.info("host stopped")

    app = FastAPI(title="Inky Display Host", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get("X-Request-ID", str(uuid4()))
        token = request_id_context.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            request_id_context.reset(token)
        response.headers["X-Request-ID"] = correlation_id
        return response

    def get_sessions(request: Request) -> sessionmaker[Session]:
        return request.app.state.sessions  # type: ignore[no-any-return]

    def require_agent(request: Request, sessions: sessionmaker[Session] = Depends(get_sessions)) -> DisplayRecord:
        display_id = request.path_params.get("display_id", request.app.state.settings.display_id)
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            session.expunge(display)
            return display

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "worker": "running"}

    @app.post("/api/v1/assets", response_model=AssetResponse, status_code=status.HTTP_201_CREATED)
    async def upload_asset(
        request: Request,
        file: UploadFile = File(...),
    ) -> AssetResponse:
        storage: Storage = request.app.state.storage
        runtime: Settings = request.app.state.settings
        stored = await storage.save_upload(file, runtime.max_upload_bytes)
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            asset = upsert_asset(session, stored)
            session.commit()
            session.refresh(asset)
            response = _asset_response(asset)
        logger.info("asset uploaded", extra={"asset_id": asset.id, "sha256": stored.sha256})
        return response

    @app.get("/api/v1/assets", response_model=list[AssetResponse])
    def list_assets(
        request: Request,
        include_deleted: bool = False,
        query: str = "",
        offset: int = 0,
        limit: int = 250,
    ) -> list[AssetResponse]:
        safe_offset = max(offset, 0)
        safe_limit = min(max(limit, 1), 100)
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            asset_query = select(AssetRecord).order_by(desc(AssetRecord.created_at))
            if not include_deleted:
                asset_query = asset_query.where(AssetRecord.deleted_at.is_(None))
            if query.strip():
                asset_query = asset_query.where(AssetRecord.original_filename.ilike(f"%{query.strip()}%"))
            assets = session.scalars(asset_query.offset(safe_offset).limit(safe_limit)).all()
            return [_asset_response(asset) for asset in assets]

    @app.delete("/api/v1/assets/{asset_id}", response_model=DeleteResult)
    def delete_asset(asset_id: str, request: Request) -> DeleteResult:
        return _delete_assets([asset_id], request)

    @app.post("/api/v1/assets/bulk-delete", response_model=DeleteResult)
    def bulk_delete_assets(body: BulkDeleteRequest, request: Request) -> DeleteResult:
        return _delete_assets([str(asset_id) for asset_id in body.asset_ids], request)

    @app.patch("/api/v1/assets/{asset_id}", response_model=AssetResponse)
    def update_asset(asset_id: str, body: AssetUpdate, request: Request) -> AssetResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            asset = session.get(AssetRecord, asset_id)
            if asset is None or asset.deleted_at is not None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
            display = session.get(DisplayRecord, request.app.state.settings.display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Display is unavailable")
            try:
                request.app.state.worker.prepare_asset_preview(session, asset, display, body.render_settings)
            except Exception as error:
                session.rollback()
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
            asset.render_settings = body.render_settings.model_dump(mode="json")
            session.commit()
            return _asset_response(asset)

    @app.get("/api/v1/assets/{asset_id}/preview")
    def asset_preview(asset_id: str, request: Request) -> FileResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        storage: Storage = request.app.state.storage
        with sessions() as session:
            asset = session.get(AssetRecord, asset_id)
            if asset is None or asset.deleted_at is not None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
            display = session.get(DisplayRecord, request.app.state.settings.display_id)
            artifact = None
            if display is not None:
                settings = RenderSettings.model_validate(asset.render_settings or display.default_render_settings)
                cache_key = artifact_cache_key(asset.sha256, profile_from_record(display), settings)
                artifact = session.scalar(select(ArtifactRecord).where(ArtifactRecord.cache_key == cache_key))
            if artifact is not None:
                path = storage.preview_path(artifact.sha256)
                media_type = "image/png"
            else:
                path = storage.path(asset.storage_path)
                media_type = asset.mime_type
        return FileResponse(path, media_type=media_type)

    @app.get("/api/v1/assets/{asset_id}/original")
    def asset_original(asset_id: str, request: Request) -> FileResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        storage: Storage = request.app.state.storage
        with sessions() as session:
            asset = session.get(AssetRecord, asset_id)
            if asset is None or asset.deleted_at is not None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
            path = storage.path(asset.storage_path)
            media_type = asset.mime_type
        return FileResponse(path, media_type=media_type)

    @app.get("/api/v1/displays/{display_id}", response_model=DisplayResponse)
    def get_display(display_id: str, request: Request) -> DisplayResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            return _display_response(session, display)

    @app.patch("/api/v1/displays/{display_id}/settings", response_model=DisplayResponse)
    def update_display_settings(display_id: str, update: DisplaySettingsUpdate, request: Request) -> DisplayResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            if update.name is not None:
                display.name = update.name
            if update.orientation is not None:
                display.orientation = update.orientation.value
            if update.rotation is not None:
                display.rotation = int(update.rotation)
            if update.default_render_settings is not None:
                display.default_render_settings = update.default_render_settings.model_dump(mode="json")
            session.commit()
            session.refresh(display)
            return _display_response(session, display)

    @app.get("/api/v1/albums", response_model=list[AlbumResponse])
    def list_albums(request: Request) -> list[AlbumResponse]:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            albums = session.scalars(
                select(AlbumRecord)
                .where(AlbumRecord.is_temporary.is_(False))
                .order_by(AlbumRecord.created_at.desc())
            ).all()
            return [album_response(session, album) for album in albums]

    @app.post("/api/v1/albums", response_model=AlbumResponse, status_code=status.HTTP_201_CREATED)
    def create_album(body: AlbumCreate, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            if session.get(DisplayRecord, body.display_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            if session.scalar(select(AlbumRecord).where(AlbumRecord.name == body.name)) is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Album name already exists")
            assets = [session.get(AssetRecord, asset_id) for asset_id in _unique_asset_ids(body.asset_ids)]
            available_assets = [asset for asset in assets if asset is not None and asset.deleted_at is None]
            if len(available_assets) != len(assets):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Albums require available gallery images"
                )
            album = AlbumRecord(
                id=str(uuid4()),
                display_id=body.display_id,
                name=body.name,
                order_mode=body.order_mode.value,
                interval_seconds=body.interval_seconds,
                enabled=body.enabled,
                time_zone=body.time_zone,
                schedule_start_at=body.schedule_start_at,
                schedule_end_at=body.schedule_end_at,
                default_render_settings=RenderSettings().model_dump(mode="json"),
            )
            session.add(album)
            session.flush()
            for position, asset in enumerate(available_assets):
                session.add(AlbumItemRecord(id=str(uuid4()), album_id=album.id, asset_id=asset.id, position=position))
            session.commit()
            return album_response(session, album)

    @app.get("/api/v1/albums/{album_id}", response_model=AlbumResponse)
    def get_album(album_id: str, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            return album_response(session, album)

    @app.patch("/api/v1/albums/{album_id}", response_model=AlbumResponse)
    def update_album(album_id: str, body: AlbumUpdate, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            values = body.model_dump(exclude_unset=True)
            for field, value in values.items():
                if field == "order_mode":
                    value = value.value
                setattr(album, field, value)
            if not album.enabled and album.is_running:
                stop_album_record(session, album)
            if album.schedule_start_at and album.schedule_end_at and album.schedule_end_at <= album.schedule_start_at:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="schedule_end_at must be after schedule_start_at",
                )
            session.commit()
            return album_response(session, album)

    @app.put("/api/v1/albums/{album_id}/items", response_model=AlbumResponse)
    def replace_album_items(album_id: str, body: AlbumItemsUpdate, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            assets = [session.get(AssetRecord, asset_id) for asset_id in _unique_asset_ids(body.asset_ids)]
            available_assets = [asset for asset in assets if asset is not None and asset.deleted_at is None]
            if len(available_assets) != len(assets):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Albums require available gallery images"
                )
            existing_items = session.scalars(select(AlbumItemRecord).where(AlbumItemRecord.album_id == album.id)).all()
            for item in existing_items:
                session.delete(item)
            for position, asset in enumerate(available_assets):
                session.add(AlbumItemRecord(id=str(uuid4()), album_id=album.id, asset_id=asset.id, position=position))
            album.next_item_index = 0
            session.commit()
            return album_response(session, album)

    @app.post("/api/v1/albums/{album_id}/run", response_model=AlbumResponse)
    async def run_album(album_id: str, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            if not session.scalar(select(AlbumItemRecord.id).where(AlbumItemRecord.album_id == album_id)):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Album has no images")
        await request.app.state.album_scheduler.run_album(album_id)
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            if not album.is_running:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Album has no available images")
            return album_response(session, album)

    @app.post("/api/v1/albums/{album_id}/stop", response_model=AlbumResponse)
    async def stop_album(album_id: str, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            if session.get(AlbumRecord, album_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
        await request.app.state.album_scheduler.stop_album(album_id)
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            return album_response(session, album)

    @app.delete("/api/v1/albums/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_album(album_id: str, request: Request) -> Response:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
            stop_album_record(session, album)
            for item in session.scalars(select(AlbumItemRecord).where(AlbumItemRecord.album_id == album.id)):
                session.delete(item)
            session.execute(
                text("UPDATE display_jobs SET album_id = NULL WHERE album_id = :album_id"),
                {"album_id": album.id},
            )
            session.delete(album)
            session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/icloud/links", response_model=IcloudLinkResponse, status_code=status.HTTP_201_CREATED)
    async def create_icloud_link(body: IcloudLinkCreate, request: Request) -> IcloudLinkResponse:
        try:
            token = parse_share_token(body.share_url)
        except IcloudShareError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, body.display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            existing = session.scalar(
                select(IcloudAlbumLinkRecord).where(IcloudAlbumLinkRecord.token == token)
            )
            if existing is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That album is already linked")
            album = AlbumRecord(
                id=str(uuid4()),
                display_id=body.display_id,
                name=_unique_album_name(session, body.name or "Shared album"),
                order_mode=AlbumOrderMode.SHUFFLE.value,
                interval_seconds=body.interval_seconds,
                enabled=True,
                time_zone=display.time_zone,
                default_render_settings=RenderSettings().model_dump(mode="json"),
            )
            session.add(album)
            session.flush()
            link = IcloudAlbumLinkRecord(
                id=str(uuid4()),
                album_id=album.id,
                token=token,
                share_url=body.share_url,
                base_url="",
                stream_name=album.name,
                sync_interval_seconds=body.sync_interval_seconds,
                next_sync_at=datetime.now(UTC),
            )
            session.add(link)
            session.commit()
            link_id = link.id

        # Run the first pass inline so the UI shows photos instead of an empty album.
        await request.app.state.icloud_sync.sync_link(link_id)
        with sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked album not found")
            album = session.get(AlbumRecord, link.album_id)
            if body.name is None and album is not None and link.stream_name:
                album.name = _unique_album_name(session, link.stream_name, album.id)
                session.commit()
            return _icloud_link_response(link)

    @app.get("/api/v1/icloud/links", response_model=list[IcloudLinkResponse])
    def list_icloud_links(request: Request) -> list[IcloudLinkResponse]:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            links = session.scalars(
                select(IcloudAlbumLinkRecord).order_by(desc(IcloudAlbumLinkRecord.created_at))
            ).all()
            return [_icloud_link_response(link) for link in links]

    @app.patch("/api/v1/icloud/links/{link_id}", response_model=IcloudLinkResponse)
    def update_icloud_link(link_id: str, body: IcloudLinkUpdate, request: Request) -> IcloudLinkResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked album not found")
            if body.enabled is not None:
                link.enabled = body.enabled
            if body.sync_interval_seconds is not None:
                link.sync_interval_seconds = body.sync_interval_seconds
            session.commit()
            return _icloud_link_response(link)

    @app.post("/api/v1/icloud/links/{link_id}/sync", response_model=IcloudLinkResponse)
    async def sync_icloud_link(link_id: str, request: Request) -> IcloudLinkResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            if session.get(IcloudAlbumLinkRecord, link_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked album not found")
        await request.app.state.icloud_sync.sync_link(link_id, force=True)
        return _icloud_link_response_by_id(sessions, link_id)

    @app.delete("/api/v1/icloud/links/{link_id}")
    def delete_icloud_link(link_id: str, request: Request, delete_album: bool = False) -> Response:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            link = session.get(IcloudAlbumLinkRecord, link_id)
            if link is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked album not found")
            album = session.get(AlbumRecord, link.album_id)
            session.execute(delete(IcloudPhotoRecord).where(IcloudPhotoRecord.link_id == link.id))
            session.delete(link)
            if delete_album and album is not None:
                stop_album_record(session, album)
                session.execute(delete(AlbumItemRecord).where(AlbumItemRecord.album_id == album.id))
                session.execute(
                    text("UPDATE display_jobs SET album_id = NULL WHERE album_id = :album_id"),
                    {"album_id": album.id},
                )
                session.delete(album)
            session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/displays/{display_id}/play-selection", response_model=AlbumResponse)
    async def play_selection(display_id: str, body: QuickPlayRequest, request: Request) -> AlbumResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            assets = [session.get(AssetRecord, asset_id) for asset_id in _unique_asset_ids(body.asset_ids)]
            if any(asset is None or asset.deleted_at is not None for asset in assets):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Choose available gallery images")
            album = AlbumRecord(
                id=str(uuid4()),
                display_id=display_id,
                name=f"Selected images {uuid4().hex[:8]}",
                order_mode=body.order_mode.value,
                interval_seconds=body.interval_seconds,
                enabled=True,
                is_temporary=True,
                time_zone=display.time_zone,
                default_render_settings=RenderSettings().model_dump(mode="json"),
            )
            session.add(album)
            session.flush()
            for position, asset in enumerate(assets):
                assert asset is not None
                session.add(AlbumItemRecord(id=str(uuid4()), album_id=album.id, asset_id=asset.id, position=position))
            session.commit()
            album_id = album.id
        await request.app.state.album_scheduler.run_album(album_id)
        with sessions() as session:
            album = session.get(AlbumRecord, album_id)
            if album is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Selection not found")
            return album_response(session, album)

    @app.post("/api/v1/displays/{display_id}/stop-playback")
    def stop_playback(display_id: str, request: Request) -> dict[str, bool]:
        """Stop the currently running saved album or temporary selection."""

        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            if session.get(DisplayRecord, display_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            stopped_albums = stop_running_albums(session, display_id)
            session.commit()
        return {"stopped": bool(stopped_albums)}

    @app.post("/api/v1/displays/{display_id}/display-now", response_model=JobResponse)
    async def display_now(display_id: str, body: DisplayNowRequest, request: Request) -> JobResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            asset = session.get(AssetRecord, str(body.asset_id))
            if display is None or asset is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display or asset not found")
            if asset.deleted_at is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Asset is unavailable")
            render_settings = body.render_settings or RenderSettings.model_validate(
                asset.render_settings or display.default_render_settings
            )
            stop_running_albums(session, display.id)
            job = create_display_now_job(session, display, asset, render_settings)
            session.commit()
            response = _job_response(session, job)
        await request.app.state.worker.enqueue(job.id)
        logger.info("display-now queued", extra={"job_id": job.id, "asset_id": asset.id})
        return response

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, request: Request) -> JobResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            job = session.get(DisplayJobRecord, job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
            return _job_response(session, job)

    @app.get("/api/v1/jobs/{job_id}/preview")
    def job_preview(job_id: str, request: Request) -> FileResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        storage: Storage = request.app.state.storage
        with sessions() as session:
            job = session.get(DisplayJobRecord, job_id)
            artifact = session.get(ArtifactRecord, job.artifact_id) if job and job.artifact_id else None
            if artifact is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview is not ready")
            path = storage.preview_path(artifact.sha256)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/displays/{display_id}/desired", response_model=DesiredState)
    def desired_state(
        display_id: str,
        request: Request,
        current_revision: int = 0,
        _: DisplayRecord = Depends(require_agent),
    ) -> DesiredState | Response:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            # Revisions count up from zero in this database. An agent reporting a revision we
            # never issued kept its state across a host database reset, so its counter says
            # nothing about what is on the panel and comparing against it would strand the
            # display until we caught back up.
            agent_predates_this_database = current_revision > display.desired_revision
            if agent_predates_this_database:
                _report_stale_agent_revision(session, display, current_revision)
            job = session.get(DisplayJobRecord, display.desired_job_id) if display and display.desired_job_id else None
            artifact = session.get(ArtifactRecord, job.artifact_id) if job and job.artifact_id else None
            if job is None or artifact is None or job.revision is None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            if job.revision <= current_revision and not agent_predates_this_database:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            return DesiredState(
                display_id=display_id,
                revision=job.revision,
                job_id=UUID(job.id),
                artifact=ArtifactDescriptor(
                    sha256=artifact.sha256,
                    url=f"/api/v1/artifacts/{artifact.sha256}",
                    format=ArtifactFormat.RGB_PNG,
                    width=artifact.width,
                    height=artifact.height,
                    palette=tuple(profile_from_record(display).palette),
                    renderer_version=artifact.renderer_version,
                ),
            )

    @app.get("/api/v1/artifacts/{sha256}")
    def download_artifact(sha256: str, request: Request, _: DisplayRecord = Depends(require_agent)) -> FileResponse:
        sessions: sessionmaker[Session] = request.app.state.sessions
        storage: Storage = request.app.state.storage
        with sessions() as session:
            artifact = session.scalar(select(ArtifactRecord).where(ArtifactRecord.sha256 == sha256))
            if artifact is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
            path = storage.path(artifact.storage_path)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, immutable"})

    @app.post("/api/v1/displays/{display_id}/heartbeat")
    def heartbeat(
        display_id: str,
        body: AgentHeartbeat,
        request: Request,
        _: DisplayRecord = Depends(require_agent),
    ) -> dict[str, bool]:
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            if display is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display not found")
            display.last_seen_at = body.sent_at
            session.commit()
        return {"ok": True}

    @app.post("/api/v1/displays/{display_id}/jobs/{job_id}/{event}")
    def acknowledge_job(
        display_id: str,
        job_id: str,
        event: JobEvent,
        body: JobAcknowledgement,
        request: Request,
        _: DisplayRecord = Depends(require_agent),
    ) -> dict[str, bool]:
        if body.event != event:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Event does not match route")
        sessions: sessionmaker[Session] = request.app.state.sessions
        with sessions() as session:
            display = session.get(DisplayRecord, display_id)
            job = session.get(DisplayJobRecord, job_id)
            if display is None or job is None or job.display_id != display_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Display job not found")
            display.last_seen_at = body.occurred_at
            if event is JobEvent.STARTED:
                if job.status == JobStatus.READY:
                    job.status = JobStatus.STARTED
                    job.started_at = body.occurred_at
            elif event is JobEvent.COMPLETED:
                if job.status in (JobStatus.READY, JobStatus.STARTED):
                    job.status = JobStatus.COMPLETED
                    job.completed_at = body.occurred_at
                    if job.revision is not None:
                        display.current_revision = max(display.current_revision, job.revision)
                    display.last_error = None
            else:
                if job.status in (JobStatus.QUEUED, JobStatus.RENDERING, JobStatus.READY, JobStatus.STARTED):
                    job.status = JobStatus.FAILED
                    job.failed_at = body.occurred_at
                    job.error_code = body.error_code or "agent-failed"
                    job.error_message = body.error_message or "The agent reported a display failure"
                    display.last_error = job.error_message
            session.commit()
        return {"ok": True}

    return app


app = create_app()
