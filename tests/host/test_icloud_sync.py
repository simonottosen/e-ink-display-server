import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from inky_host.config import Settings
from inky_host.database import AssetRecord, create_session_factory
from inky_host.main import create_app

SHARE_URL = "https://share.icloud.com/photos/B1aBcDeFgH"


def photo_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (400, 300), color).save(output, format="JPEG")
    return output.getvalue()


class FakeIcloud:
    """Serves a shared album from memory, counting the calls the sync actually makes."""

    def __init__(self) -> None:
        self.ctag = "ctag-1"
        self.photos: dict[str, bytes] = {
            "guid-1": photo_bytes((200, 30, 30)),
            "guid-2": photo_bytes((30, 200, 30)),
        }
        self.asset_url_calls = 0

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/webstream"):
            return httpx.Response(
                200,
                json={
                    "streamName": "Family",
                    "streamCtag": self.ctag,
                    "photos": [
                        {
                            "photoGuid": guid,
                            "caption": guid,
                            "dateCreated": f"2026-01-0{index + 1}T10:00:00Z",
                            "derivatives": {
                                "1": {"checksum": guid, "fileSize": len(data), "width": 400, "height": 300}
                            },
                        }
                        for index, (guid, data) in enumerate(self.photos.items())
                    ],
                },
            )
        if path.endswith("/webasseturls"):
            self.asset_url_calls += 1
            guids = json.loads(request.content)["photoGuids"]
            return httpx.Response(
                200,
                json={
                    "items": {
                        guid: {"url_location": "cvws.icloud-content.com", "url_path": f"/S/{guid}"}
                        for guid in guids
                        if guid in self.photos
                    }
                },
            )
        guid = path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=self.photos[guid])


def link_album(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/icloud/links", json={"share_url": SHARE_URL})
    assert response.status_code == 201, response.text
    return response.json()


def test_linking_an_album_downloads_its_photos_into_a_shuffled_album(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        link = link_album(client)
        assert link["photo_count"] == 2
        assert link["last_error"] is None
        assert link["stream_name"] == "Family"
        # The token is a bearer capability and this API is unauthenticated.
        assert "token" not in link and "share_url" not in link

        album = client.get(f"/api/v1/albums/{link['album_id']}").json()
        assert album["order_mode"] == "shuffle"
        assert album["name"] == "Family"
        assert len(album["items"]) == 2


def test_an_unchanged_ctag_costs_one_request_and_no_downloads(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        link = link_album(client)
        assert remote.asset_url_calls == 1

        synced = client.post(f"/api/v1/icloud/links/{link['id']}/sync").json()
        # A forced sync still re-reads the stream, but nothing new needs downloading.
        assert synced["photo_count"] == 2
        assert remote.asset_url_calls == 1


def test_a_photo_removed_upstream_leaves_the_album_but_stays_in_the_gallery(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        link = link_album(client)

        del remote.photos["guid-2"]
        remote.ctag = "ctag-2"
        client.post(f"/api/v1/icloud/links/{link['id']}/sync")

        album = client.get(f"/api/v1/albums/{link['album_id']}").json()
        assert len(album["items"]) == 1
        gallery = client.get("/api/v1/assets").json()
        assert len(gallery) == 2
        assert all(asset["deleted_at"] is None for asset in gallery)


def test_identical_photos_collapse_onto_one_asset(tmp_path: Path) -> None:
    remote = FakeIcloud()
    remote.photos["guid-2"] = remote.photos["guid-1"]
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        link_album(client)
        assert len(client.get("/api/v1/assets").json()) == 1


def test_an_empty_album_response_never_wipes_the_playlist(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        link = link_album(client)

        remote.photos.clear()
        remote.ctag = "ctag-empty"
        synced = client.post(f"/api/v1/icloud/links/{link['id']}/sync").json()

        assert synced["last_error"]
        album = client.get(f"/api/v1/albums/{link['album_id']}").json()
        assert len(album["items"]) == 2


def test_a_private_album_is_reported_without_leaking_the_token(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with TestClient(create_app(settings, factory)) as client:
        link = link_album(client)
        assert link["photo_count"] == 0
        assert "private" in str(link["last_error"])
        assert "B1aBcDeFgH" not in str(link["last_error"])


def test_a_malformed_share_link_is_rejected_at_paste_time(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    with TestClient(create_app(settings, remote.factory())) as client:
        assert client.post("/api/v1/icloud/links", json={"share_url": "not a link"}).status_code == 400
        link_album(client)
        assert client.post("/api/v1/icloud/links", json={"share_url": SHARE_URL}).status_code == 409


def test_unlinking_keeps_the_photos(tmp_path: Path) -> None:
    remote = FakeIcloud()
    settings = Settings(data_dir=tmp_path / "data", environment="test", icloud_sync_enabled=False)
    app = create_app(settings, remote.factory())
    with TestClient(app) as client:
        link = link_album(client)
        assert client.delete(f"/api/v1/icloud/links/{link['id']}").status_code == 204
        assert client.get("/api/v1/icloud/links").json() == []
        assert len(client.get("/api/v1/assets").json()) == 2

    sessions = create_session_factory(settings)
    with sessions() as session:
        assert session.query(AssetRecord).count() == 2
