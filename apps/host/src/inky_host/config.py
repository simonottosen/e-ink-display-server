"""Configuration for the Docker host."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from inky_contract import (
    DisplayOrientation,
    DisplayProfile,
    DisplayRotation,
    PaletteColor,
    RenderSettings,
)


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="INKY_", extra="ignore")

    log_level: str = "INFO"
    data_dir: Path = Path("data")
    database_url: str | None = None
    max_upload_bytes: int = 20 * 1024 * 1024
    max_source_pixels: int = 40_000_000

    display_id: str = "inky-main"
    display_name: str = "Main Inky Display"
    display_orientation: DisplayOrientation = DisplayOrientation.LANDSCAPE
    display_rotation_degrees: DisplayRotation = DisplayRotation.DEGREES_0
    display_time_zone: str = "Europe/Copenhagen"

    icloud_sync_enabled: bool = True
    icloud_sync_interval_seconds: int = 900
    # A shared album holds up to 5000 photos; uncapped, the first sync is tens of gigabytes.
    icloud_max_photos: int = 500
    icloud_guid_chunk_size: int = 20
    icloud_request_timeout_seconds: int = 30

    @cached_property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir.resolve() / 'inky.sqlite3'}"

    def initial_display_profile(self) -> DisplayProfile:
        return DisplayProfile(
            id=self.display_id,
            name=self.display_name,
            width=800,
            height=480,
            palette=tuple(PaletteColor),
            orientation=self.display_orientation,
            rotation=self.display_rotation_degrees,
            default_render_settings=RenderSettings(),
            time_zone=self.display_time_zone,
        )
