import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import httpx
import pytest

from inky_host.icloud import IcloudClient, IcloudShareError

WEBSTREAM = {
    "streamName": "Family",
    "streamCtag": "ctag-1",
    "photos": [
        {
            "photoGuid": "guid-1",
            "caption": "Beach",
            "dateCreated": "2026-01-01T10:00:00Z",
            "derivatives": {
                "1": {"checksum": "small", "fileSize": "100", "width": "80", "height": "60"},
                "2": {"checksum": "big", "fileSize": 4000, "width": 1600, "height": 1200},
            },
        },
        {"photoGuid": "guid-2", "derivatives": {}},
        {"not": "a photo"},
    ],
}

T = TypeVar("T")


def run_with(
    handler: Callable[[httpx.Request], httpx.Response],
    body: Callable[[httpx.AsyncClient], Coroutine[Any, Any, T]],
) -> T:
    """Drive one coroutine against a network-free transport."""

    async def main() -> T:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await body(http)

    return asyncio.run(main())


def test_fetch_stream_parses_mixed_string_and_number_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/webstream")
        return httpx.Response(200, json=WEBSTREAM)

    stream = run_with(handler, lambda http: IcloudClient(http).fetch_stream("B1aBcDeFgH"))

    assert stream.stream_name == "Family"
    assert stream.stream_ctag == "ctag-1"
    assert [photo.photo_guid for photo in stream.photos] == ["guid-1", "guid-2"]
    best = stream.photos[0].best_derivative()
    assert best is not None and best.checksum == "big" and best.file_size == 4000


def test_fetch_stream_follows_the_330_redirect_once() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if len(seen) == 1:
            return httpx.Response(330, json={"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"})
        return httpx.Response(200, json=WEBSTREAM)

    stream = run_with(handler, lambda http: IcloudClient(http).fetch_stream("B1aBcDeFgH"))

    assert seen[1] == "p42-sharedstreams.icloud.com"
    assert stream.base_url.startswith("https://p42-sharedstreams.icloud.com/")


def test_a_second_redirect_aborts_rather_than_looping() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(330, json={"X-Apple-MMe-Host": "p42-sharedstreams.icloud.com"})

    with pytest.raises(IcloudShareError):
        run_with(handler, lambda http: IcloudClient(http).fetch_stream("B1aBcDeFgH"))


def test_a_redirect_to_a_foreign_host_is_refused() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(330, json={"X-Apple-MMe-Host": "evil.example.com"})

    with pytest.raises(IcloudShareError):
        run_with(handler, lambda http: IcloudClient(http).fetch_stream("B1aBcDeFgH"))


def test_fetch_asset_urls_chunks_large_guid_batches() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content)["photoGuids"])
        return httpx.Response(
            200,
            json={
                "items": {
                    "big": {"url_location": "cvws.icloud-content.com", "url_path": "/S/one?sig=1"},
                    "bad": {"url_location": "evil.com", "url_path": "/S/two"},
                }
            },
        )

    guids = [f"guid-{index}" for index in range(45)]
    urls = run_with(
        handler,
        lambda http: IcloudClient(http, chunk_size=20).fetch_asset_urls(
            "https://p11-sharedstreams.icloud.com/T/sharedstreams/", guids
        ),
    )

    assert [len(chunk) for chunk in requests] == [20, 20, 5]
    assert urls == {"big": "https://cvws.icloud-content.com/S/one?sig=1"}


def test_a_private_album_reports_a_message_without_the_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(IcloudShareError) as error:
        run_with(handler, lambda http: IcloudClient(http).fetch_stream("B1aBcDeFgH"))

    assert "B1aBcDeFgH" not in str(error.value)


def test_stream_download_stops_once_the_limit_is_passed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async def drain(http: httpx.AsyncClient) -> None:
        async for _chunk in IcloudClient(http).stream_download("https://cvws.icloud-content.com/S/one", 10):
            pass

    with pytest.raises(IcloudShareError):
        run_with(handler, drain)
