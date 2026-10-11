import time
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from inky_contract import JobAcknowledgement, JobEvent
from inky_host.config import Settings
from inky_host.main import create_app


def png_upload(color: tuple[int, int, int] = (30, 110, 200)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (1000, 700), color).save(output, format="PNG")
    return output.getvalue()


def test_health_and_upload_to_desired_state(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        environment="test",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/health", headers={"X-Request-ID": "test-correlation"})
        assert health.json()["status"] == "ok"
        assert health.headers["X-Request-ID"] == "test-correlation"

        upload = client.post(
            "/api/v1/assets",
            files={"file": ("sky.png", png_upload(), "image/png")},
        )
        assert upload.status_code == 201
        asset_id = upload.json()["id"]

        queued = client.post(
            "/api/v1/displays/inky-main/display-now",
            json={"asset_id": asset_id, "render_settings": {"fit_mode": "contain"}},
        )
        assert queued.status_code == 200
        job_id = queued.json()["id"]

        for _ in range(30):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"ready", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "ready"
        assert client.get(job["preview_url"]).headers["content-type"].startswith("image/png")

        desired = client.get("/api/v1/displays/inky-main/desired")
        assert desired.status_code == 200
        payload = desired.json()
        assert payload["artifact"]["width"] == 800
        assert payload["artifact"]["format"] == "rgb-png"
        artifact = Image.open(BytesIO(client.get(payload["artifact"]["url"]).content))
        assert artifact.mode == "RGB"

        started = JobAcknowledgement(event=JobEvent.STARTED, occurred_at="2026-01-01T00:00:00Z")
        assert (
            client.post(
                f"/api/v1/displays/inky-main/jobs/{job_id}/started",
                json=started.model_dump(mode="json"),
            ).status_code
            == 200
        )
        completed = JobAcknowledgement(
            event=JobEvent.COMPLETED,
            completed_revision=payload["revision"],
            occurred_at="2026-01-01T00:00:01Z",
        )
        assert (
            client.post(
                f"/api/v1/displays/inky-main/jobs/{job_id}/completed",
                json=completed.model_dump(mode="json"),
            ).status_code
            == 200
        )
        assert client.get("/api/v1/displays/inky-main").json()["current_revision"] == 1


def test_agent_from_a_previous_database_still_receives_the_desired_state(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", environment="test")
    with TestClient(create_app(settings)) as client:
        upload = client.post("/api/v1/assets", files={"file": ("sky.png", png_upload(), "image/png")})
        queued = client.post(
            "/api/v1/displays/inky-main/display-now",
            json={"asset_id": upload.json()["id"]},
        )
        job_id = queued.json()["id"]
        for _ in range(30):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"ready", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "ready"

        stranded = client.get("/api/v1/displays/inky-main/desired?current_revision=194")
        assert stranded.status_code == 200
        revision = stranded.json()["revision"]
        assert revision == 1

        reported = client.get("/api/v1/displays/inky-main").json()["last_error"]
        assert "194" in reported
        assert "state.json" in reported

        completed = JobAcknowledgement(
            event=JobEvent.COMPLETED,
            completed_revision=revision,
            occurred_at="2026-01-01T00:00:01Z",
        )
        client.post(
            f"/api/v1/displays/inky-main/jobs/{job_id}/completed",
            json=completed.model_dump(mode="json"),
        )
        caught_up = client.get(f"/api/v1/displays/inky-main/desired?current_revision={revision}")
        assert caught_up.status_code == 204
        assert client.get("/api/v1/displays/inky-main").json()["last_error"] is None


def test_upload_rejects_unsupported_content_type(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", environment="test")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/assets", files={"file": ("not-an-image.txt", b"hello", "text/plain")})
    assert response.status_code == 415


def test_portrait_setting_changes_the_browser_preview_orientation(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", environment="test")
    with TestClient(create_app(settings)) as client:
        settings_response = client.patch(
            "/api/v1/displays/inky-main/settings",
            json={"orientation": "portrait", "rotation": 90},
        )
        assert settings_response.status_code == 200
        assert settings_response.json()["orientation"] == "portrait"

        upload = client.post(
            "/api/v1/assets",
            files={"file": ("portrait.png", png_upload(), "image/png")},
        )
        queued = client.post(
            "/api/v1/displays/inky-main/display-now",
            json={"asset_id": upload.json()["id"]},
        )
        job_id = queued.json()["id"]
        for _ in range(30):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"ready", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "ready"

        preview = Image.open(BytesIO(client.get(job["preview_url"]).content))
        assert preview.size == (480, 800)


def test_gallery_album_lifecycle_allows_soft_delete_of_active_content(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", environment="test")
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/assets",
            files={"file": ("first.png", png_upload((200, 40, 30)), "image/png")},
        ).json()
        second = client.post(
            "/api/v1/assets",
            files={"file": ("second.png", png_upload((30, 180, 60)), "image/png")},
        ).json()
        album = client.post(
            "/api/v1/albums",
            json={
                "name": "Weekend colours",
                "asset_ids": [first["id"], second["id"]],
                "interval_seconds": 60,
            },
        )
        assert album.status_code == 201
        album_id = album.json()["id"]
        assert [item["asset_id"] for item in album.json()["items"]] == [first["id"], second["id"]]

        running = client.post(f"/api/v1/albums/{album_id}/run")
        assert running.status_code == 200
        assert running.json()["is_running"] is True

        deleted_first = client.delete(f"/api/v1/assets/{first['id']}")
        assert deleted_first.status_code == 200
        assert deleted_first.json()["deleted_ids"] == [first["id"]]

        assert client.post(f"/api/v1/albums/{album_id}/stop").status_code == 200
        deleted = client.post("/api/v1/assets/bulk-delete", json={"asset_ids": [second["id"]]})
        assert deleted.status_code == 200
        assert deleted.json()["deleted_ids"] == [second["id"]]
        assert client.get("/api/v1/assets").json() == []

        restored = client.post(f"/api/v1/assets/{second['id']}/restore")
        assert restored.status_code == 200
        assert [asset["id"] for asset in client.get("/api/v1/assets").json()] == [second["id"]]
        assert any(event["event_type"] == "album.started" for event in client.get("/api/v1/activity").json())
