"""Client for Apple's undocumented shared photo stream API.

The protocol is reverse engineered and has no published specification, so every field is
treated as untrusted: numbers arrive as strings or numbers depending on the field, and the
host names used for the redirect and for downloads come from the response body rather than
from us.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

SHARE_HOST_SUFFIXES = (".icloud.com", ".icloud-content.com", ".cdn-apple.com")
PARTITION_COUNT = 40
REDIRECT_STATUS = 330
DEFAULT_GUID_CHUNK_SIZE = 20
MAX_ASSET_REDIRECTS = 2
# Short legacy tokens are base62, but a current link carries a long base64url token that
# also uses "-" and "_" (observed: a 78 character token from /sharedalbum/#...).
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


class IcloudShareError(Exception):
    """A shared album could not be read. The message is safe to show a user."""


def parse_share_token(url: str) -> str:
    """Extract the album token from a share link, or from a bare token."""

    candidate = url.strip()
    if not candidate:
        raise IcloudShareError("Enter an iCloud shared album link")
    # Apple hands out links as https://share.icloud.com/photos/#TOKEN as well as /photos/TOKEN.
    candidate = candidate.split("#")[-1] if "#" in candidate else candidate
    if "://" in candidate:
        candidate = urlsplit(candidate).path
    token = candidate.strip("/").split("/")[-1]
    if not TOKEN_PATTERN.match(token):
        raise IcloudShareError("That does not look like an iCloud shared album link")
    return token


def _base62_value(character: str) -> int:
    if character.isdigit():
        return ord(character) - ord("0")
    if "A" <= character <= "Z":
        return ord(character) - ord("A") + 10
    if "a" <= character <= "z":
        return ord(character) - ord("a") + 36
    # "-" and "_" are outside Apple's base62 alphabet, so the partition is a guess. That is
    # harmless: a wrong guess answers 330 with the host to use, and we follow it.
    return 0


def base_url_for(token: str) -> str:
    """Derive the sharded host Apple expects for this token."""

    if not token:
        raise IcloudShareError("That does not look like an iCloud shared album link")
    partition = 1 + (_base62_value(token[0]) % PARTITION_COUNT)
    return f"https://p{partition:02d}-sharedstreams.icloud.com/{token}/sharedstreams/"


def require_allowed_url(url: str) -> str:
    """Reject a URL that remote JSON could use to make us fetch something private.

    Both the redirect host and every download URL are chosen by the response body, and this
    host sits unauthenticated on a home network where localhost and the router are reachable.
    """

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise IcloudShareError("The album server asked for an address that is not HTTPS")
    host = parts.hostname
    if not host or not host.isascii():
        raise IcloudShareError("The album server asked for an address we do not recognise")
    try:
        if parts.port not in (None, 443):
            raise IcloudShareError("The album server asked for an address we do not recognise")
    except ValueError as error:
        raise IcloudShareError("The album server asked for an address we do not recognise") from error
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise IcloudShareError("The album server asked for an address we do not recognise")
    if not host.endswith(SHARE_HOST_SUFFIXES):
        logger.warning("refused an iCloud address outside Apple's domains: host=%s", host)
        raise IcloudShareError("The album server asked for an address we do not recognise")
    return url


def _coerce_int(value: object) -> int | None:
    """Read a field Apple returns as either a number or a decimal string."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class IcloudDerivative:
    checksum: str
    file_size: int | None
    width: int | None
    height: int | None

    @property
    def pixels(self) -> int:
        if self.width is None or self.height is None:
            return 0
        return self.width * self.height


@dataclass(frozen=True)
class IcloudPhoto:
    photo_guid: str
    caption: str | None
    date_created: str | None
    derivatives: tuple[IcloudDerivative, ...]

    def best_derivative(self) -> IcloudDerivative | None:
        if not self.derivatives:
            return None
        return max(self.derivatives, key=lambda item: (item.pixels, item.file_size or 0))


@dataclass(frozen=True)
class IcloudStream:
    stream_name: str
    stream_ctag: str
    base_url: str
    photos: tuple[IcloudPhoto, ...]


def _parse_photo(payload: object) -> IcloudPhoto | None:
    if not isinstance(payload, dict):
        return None
    guid = payload.get("photoGuid")
    if not isinstance(guid, str) or not guid:
        return None
    derivatives: list[IcloudDerivative] = []
    raw_derivatives = payload.get("derivatives")
    if isinstance(raw_derivatives, dict):
        for entry in raw_derivatives.values():
            if not isinstance(entry, dict):
                continue
            checksum = entry.get("checksum")
            if not isinstance(checksum, str) or not checksum:
                continue
            derivatives.append(
                IcloudDerivative(
                    checksum=checksum,
                    file_size=_coerce_int(entry.get("fileSize")),
                    width=_coerce_int(entry.get("width")),
                    height=_coerce_int(entry.get("height")),
                )
            )
    caption = payload.get("caption")
    date_created = payload.get("dateCreated")
    return IcloudPhoto(
        photo_guid=guid,
        caption=caption if isinstance(caption, str) and caption else None,
        date_created=date_created if isinstance(date_created, str) and date_created else None,
        derivatives=tuple(derivatives),
    )


class IcloudClient:
    """Speaks the sharedstreams protocol over an injected HTTP client."""

    def __init__(self, client: httpx.AsyncClient, chunk_size: int = DEFAULT_GUID_CHUNK_SIZE) -> None:
        self._client = client
        self._chunk_size = max(1, chunk_size)

    async def fetch_stream(self, token: str, base_url: str | None = None) -> IcloudStream:
        base = require_allowed_url(base_url) if base_url else base_url_for(token)
        response = await self._post(base + "webstream", {"streamCtag": None})
        if response.status_code == REDIRECT_STATUS:
            base = self._redirect_base(response, token)
            response = await self._post(base + "webstream", {"streamCtag": None})
            if response.status_code == REDIRECT_STATUS:
                raise IcloudShareError("The album server redirected us in a loop")
        self._raise_for_share_status(response)

        payload = self._json(response)
        photos = tuple(photo for photo in map(_parse_photo, payload.get("photos") or []) if photo is not None)
        stream_ctag = payload.get("streamCtag")
        stream_name = payload.get("streamName")
        return IcloudStream(
            stream_name=stream_name if isinstance(stream_name, str) and stream_name else "Shared album",
            stream_ctag=stream_ctag if isinstance(stream_ctag, str) else "",
            base_url=base,
            photos=photos,
        )

    async def fetch_asset_urls(self, base_url: str, guids: Sequence[str]) -> dict[str, str]:
        """Map derivative checksum to a signed, short-lived download URL.

        Apple rejects large batches, so guids are requested in chunks.
        """

        base = require_allowed_url(base_url)
        urls: dict[str, str] = {}
        for index in range(0, len(guids), self._chunk_size):
            chunk = list(guids[index : index + self._chunk_size])
            if not chunk:
                continue
            response = await self._post(base + "webasseturls", {"photoGuids": chunk})
            self._raise_for_share_status(response)
            items = self._json(response).get("items")
            if not isinstance(items, dict):
                continue
            for checksum, entry in items.items():
                if not isinstance(entry, dict):
                    continue
                location = entry.get("url_location")
                path = entry.get("url_path")
                if not isinstance(location, str) or not isinstance(path, str):
                    continue
                # url_path starts with "/", and location is a bare host. Build the URL through
                # urlunsplit so a crafted location cannot smuggle in a different authority.
                candidate = urlunsplit(("https", location, "", "", ""))
                host = urlsplit(candidate).hostname
                if host is None or host != location.lower():
                    continue
                try:
                    # One unusable entry must not cost us every other photo in the batch.
                    urls[str(checksum)] = require_allowed_url(f"https://{host}{path}")
                except IcloudShareError:
                    continue
        return urls

    async def stream_download(self, url: str, max_bytes: int) -> AsyncIterator[bytes]:
        """Yield the asset body, following only redirects that stay inside Apple's domains."""

        target = require_allowed_url(url)
        for _ in range(MAX_ASSET_REDIRECTS + 1):
            request = self._client.build_request("GET", target)
            response = await self._client.send(request, stream=True, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location", "")
                await response.aclose()
                if not location:
                    raise IcloudShareError("The album server redirected without an address")
                target = require_allowed_url(str(httpx.URL(target).join(location)))
                continue
            try:
                self._raise_for_share_status(response)
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise IcloudShareError("A photo in the album is larger than the upload limit")
                    yield chunk
            finally:
                await response.aclose()
            return
        raise IcloudShareError("The album server redirected too many times")

    async def _post(self, url: str, payload: dict[str, object]) -> httpx.Response:
        try:
            return await self._client.post(require_allowed_url(url), json=payload, follow_redirects=False)
        except httpx.HTTPError as error:
            raise IcloudShareError("The album could not be reached") from error

    def _redirect_base(self, response: httpx.Response, token: str) -> str:
        host = self._json(response).get("X-Apple-MMe-Host")
        if not isinstance(host, str) or not host:
            raise IcloudShareError("The album server redirected without an address")
        return require_allowed_url(f"https://{host}/{token}/sharedstreams/")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as error:
            raise IcloudShareError("The album server sent a response we could not read") from error
        if not isinstance(payload, dict):
            raise IcloudShareError("The album server sent a response we could not read")
        return payload

    @staticmethod
    def _raise_for_share_status(response: httpx.Response) -> None:
        # The URL carries the token, which is a bearer capability, so it must not reach a
        # message that ends up in a log line or an API response.
        if response.status_code in (401, 403):
            raise IcloudShareError("The album is private, or its link was turned off")
        if response.status_code == 404:
            raise IcloudShareError("That shared album no longer exists")
        if response.status_code >= 400:
            raise IcloudShareError(f"The album server returned an error (HTTP {response.status_code})")
