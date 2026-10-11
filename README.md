# Inky home display

[![Publish](https://github.com/simonottosen/e-ink-display-server/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/simonottosen/e-ink-display-server/actions/workflows/docker-publish.yml)

A self-hosted photo frame for the Pimoroni Inky Impression. Point it at a shared
iCloud album and it keeps a 7.3-inch e-ink panel showing your photos, forever,
without anyone touching it again.

This is the **host** half. It runs in Docker on whatever machine is always on,
owns the photo library, and decides what should be on the panel. The
[Pi agent](https://github.com/simonottosen/e-ink-display-client) is the other
half: it runs on the Raspberry Pi wired to the display and does what the host
tells it.

## Why

An e-ink panel draws no power between refreshes and reads like paper, which
makes it a genuinely good photo frame. The awkward part is everything around it:
images have to be cropped to 800×480 and mapped to a six-colour palette, the Pi
needs to survive reboots and flaky Wi-Fi without showing a blank screen, and
somebody has to keep feeding it new pictures.

So the Pi does as little as possible. It holds no library, makes no decisions,
and runs one loop: *ask the host what should be showing, fetch it, draw it.*
Everything else — storage, cropping, playlists, scheduling, syncing — happens on
the host, where a crash is a restart rather than a trip to the living room with
a keyboard.

## How it works

The host keeps a **revision** number per display. Every time it decides on a new
image it renders a ready-to-draw PNG, stores it, and bumps the revision. The
agent polls, compares, and refreshes the panel when the numbers differ:

```
agent                                 host
  |  GET /displays/inky-main/desired?current_revision=6
  |------------------------------------->|   revision 7 is ready
  |<-------------------------------------|   { revision: 7, artifact: {...} }
  |  GET /artifacts/{sha256}             |
  |------------------------------------->|
  |<-------------------------------------|   800x480 PNG
  |  POST .../jobs/{id}/completed        |   panel now shows revision 7
  |------------------------------------->|
```

If the revisions match, the host answers `204 No Content` and the agent goes
back to sleep. That is the entire protocol — four endpoints, no push, no
websockets, nothing to reconnect.

Rendering is content-addressed. An artifact's filename is a hash of the source
image, the panel profile, the render settings, and the renderer version, so the
same photo framed the same way is only ever rendered once and the agent can
cache it immutably.

## Quick start

Requirements: Git, Docker Engine, and the Docker Compose plugin.

```bash
git clone https://github.com/simonottosen/e-ink-display-server.git inky
cd inky
cp .env.example .env
docker compose -f deploy/docker/compose.yaml up -d --build host
```

Open `http://<host-ip>:8000` and upload a photo. Data lives in the named volume
`inky-host-data`.

To update:

```bash
git pull
docker compose -f deploy/docker/compose.yaml up -d --build host
```

On a fresh Debian or Ubuntu box, install the prerequisites first:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

### Without Compose

A prebuilt image is published to
`ghcr.io/simonottosen/e-ink-display-server:latest`. If you run it yourself, you
must mount a volume **and** point `INKY_DATA_DIR` at it:

```
volume:       /your/host/path:/var/lib/inky
environment:  INKY_DATA_DIR=/var/lib/inky
```

`INKY_DATA_DIR` defaults to a relative `data` path, which resolves inside the
container's writable layer. Mounting alone is not enough — without the
environment variable the app keeps writing to the old path, ignores your mount,
and silently starts from an empty database every time the container is
recreated.

## Shared iCloud albums

Paste a shared album link into the web UI and the host builds a shuffling album
from it:

```bash
curl -X POST http://localhost:8000/api/v1/icloud/links \
  -H 'content-type: application/json' \
  -d '{"share_url": "https://www.icloud.com/sharedalbum/#B1aBcDeFgH"}'
```

```json
{
  "id": "…",
  "album_id": "…",
  "stream_name": "Family",
  "photo_count": 63,
  "last_error": null
}
```

From then on it re-checks every 15 minutes. Apple's shared-album API exposes a
`streamCtag` that changes only when the album does, so an unchanged album costs
exactly one request and no downloads.

**The gallery mirrors the album.** Add a photo in iCloud and it appears here;
delete one there and it is deleted here too, including its bytes on disk —
unless another album still uses it, in which case it stays. Photos are
content-addressed by SHA-256, so two copies of the same image collapse onto one
stored file.

Note the response above: neither the token nor the share URL comes back. The
token is a bearer capability — anyone holding it can read the whole album — and
this API is unauthenticated, so it is kept out of responses, logs, and error
messages.

Every URL the sync fetches is checked before the request goes out: HTTPS only,
port 443 only, hostname suffix on an allowlist (`.icloud.com`,
`.icloud-content.com`, `.cdn-apple.com`), IP literals rejected, and the
allowlist re-applied on every redirect hop. Both the redirect host and the
download URLs arrive in remote JSON, which on an unauthenticated home server is
otherwise a server-side request forgery primitive pointed at your LAN.

## Albums

An album is an ordered list of photos plus a schedule. The scheduler ticks every
two seconds and queues the next image whenever an album is due.

```bash
curl -X POST http://localhost:8000/api/v1/albums \
  -H 'content-type: application/json' \
  -d '{
        "name": "Holiday",
        "asset_ids": ["…", "…"],
        "display_id": "inky-main",
        "order_mode": "shuffle",
        "interval_seconds": 1800
      }'
```

- `order_mode` is `sequential` or `shuffle`.
- `interval_seconds` defaults to `1200` and is clamped to 60–86,400.
- `schedule_start_at` / `schedule_end_at` bound when the album is allowed to run,
  so the panel can go quiet overnight.

## Rendering

Photos are framed to 800×480 before they ever reach the Pi.

| Fit mode  | Behaviour                                 |
|-----------|-------------------------------------------|
| `crop`    | Scale to fill, centre-crop the excess      |
| `contain` | Scale to fit, letterbox the remainder      |
| `stretch` | Scale to exact dimensions, distorting      |

`content_rotation` (0/90/180/270) turns the photo inside the frame; the panel's
own `rotation` turns the finished artifact to match how the display is mounted.
JPEG, PNG, and WebP are accepted, capped at 20 MiB and 40 megapixels.

The host emits plain RGB. Mapping to the panel's six colours happens on the Pi
at refresh time, where the hardware library already knows the exact palette.

## API

All routes are unauthenticated and assume a trusted network. Base path
`/api/v1`.

**Displays**

| Route | Purpose |
|---|---|
| `GET /displays/{id}` | Current state, last check-in, last error |
| `PATCH /displays/{id}/settings` | Name, orientation, rotation, render defaults |
| `POST /displays/{id}/display-now` | Queue one image immediately |
| `POST /displays/{id}/play-selection` | Play an ad-hoc selection |
| `POST /displays/{id}/stop-playback` | Stop whatever is running |

**Assets**

| Route | Purpose |
|---|---|
| `POST /assets` → 201 | Upload an image |
| `GET /assets` | List the gallery |
| `PATCH /assets/{id}` | Change its render settings |
| `DELETE /assets/{id}` | Delete it, permanently |
| `POST /assets/bulk-delete` | Delete several |
| `GET /assets/{id}/preview` | Rendered preview PNG |
| `GET /assets/{id}/original` | The file as uploaded |

**Albums**

| Route | Purpose |
|---|---|
| `POST /albums` → 201 | Create an album |
| `GET /albums` · `GET /albums/{id}` | List or fetch |
| `PATCH /albums/{id}` | Rename, reorder, reschedule |
| `PUT /albums/{id}/items` | Replace the playlist |
| `POST /albums/{id}/run` · `/stop` | Start or stop playback |
| `DELETE /albums/{id}` → 204 | Delete the album |

**iCloud**

| Route | Purpose |
|---|---|
| `POST /icloud/links` → 201 | Link a shared album |
| `GET /icloud/links` | List linked albums |
| `PATCH /icloud/links/{id}` | Enable, disable, re-interval |
| `POST /icloud/links/{id}/sync` | Force a sync now |
| `DELETE /icloud/links/{id}` → 204 | Unlink; `?delete_album=true` drops the album too |

**Jobs**

| Route | Purpose |
|---|---|
| `GET /jobs/{id}` | Status and error detail for one render |
| `GET /jobs/{id}/preview` | Preview of what that job produced |

**Agent protocol**

| Route | Purpose |
|---|---|
| `GET /displays/{id}/desired?current_revision=N` | Next desired state, or 204 |
| `GET /artifacts/{sha256}` | The ready-to-draw PNG |
| `POST /displays/{id}/heartbeat` | Agent check-in |
| `POST /displays/{id}/jobs/{job_id}/{event}` | `started`, `completed`, `failed` |

`GET /health` reports database and worker status.

## Configuration

Settings are read from the environment with an `INKY_` prefix, or from a `.env`
file. All are optional.

| Setting | Default |
|---|---|
| `INKY_DATA_DIR` | `data` — **set this explicitly outside Compose** |
| `INKY_DATABASE_URL` | `sqlite:///<data_dir>/inky.sqlite3` |
| `INKY_LOG_LEVEL` | `INFO` |
| `INKY_MAX_UPLOAD_BYTES` | `20971520` (20 MiB) |
| `INKY_MAX_SOURCE_PIXELS` | `40000000` |
| `INKY_DISPLAY_ID` | `inky-main` |
| `INKY_DISPLAY_NAME` | `Main Inky Display` |
| `INKY_DISPLAY_ORIENTATION` | `landscape` |
| `INKY_DISPLAY_ROTATION_DEGREES` | `0` |
| `INKY_DISPLAY_TIME_ZONE` | `Europe/Copenhagen` |
| `INKY_ICLOUD_SYNC_ENABLED` | `true` |
| `INKY_ICLOUD_SYNC_INTERVAL_SECONDS` | `900` |
| `INKY_ICLOUD_MAX_PHOTOS` | `500` |
| `INKY_ICLOUD_GUID_CHUNK_SIZE` | `20` |
| `INKY_ICLOUD_REQUEST_TIMEOUT_SECONDS` | `30` |

`INKY_HOST_PORT` (used by Compose, not the app) changes the published port from
`8000`.

## Development

The toolchain is a Docker image, so nothing needs installing locally beyond
Docker itself.

```bash
docker build -f deploy/docker/Dockerfile --target tools -t inky-tools:dev .
docker run --rm -v "$PWD":/w -w /w inky-tools:dev run --all-packages pytest tests/host -q
docker run --rm -v "$PWD":/w -w /w inky-tools:dev run ruff check
```

Layout:

```
apps/host/           FastAPI app, rendering, scheduler, iCloud sync, web UI
packages/contract/   Pydantic models shared with the Pi agent
tests/               pytest suite
deploy/docker/       Dockerfile and Compose file
docs/                Longer-form notes
```

`packages/contract` is the seam between the two repos: both sides import the
same `DesiredState`, `ArtifactDescriptor`, and `JobEvent` definitions, so a
protocol change that breaks one of them fails to type-check rather than failing
on the wall.

The iCloud tests use `httpx.MockTransport` and never touch the network.

## Known limits

- **No authentication.** Anyone who can reach the port can read and change
  everything. Put it behind a VPN or a reverse proxy with auth, not on the open
  internet.
- **One display.** The display profile is fixed at 800×480 and the display ID is
  effectively a singleton.
- **Schema changes are manual.** There is no Alembic; new tables are created on
  startup, new columns need hand-written `ALTER TABLE` statements.
- **Disk only grows.** Artifacts whose render settings changed are never
  collected.

## See also

- [Pi agent notes](docs/pi-display-agent.md)
- [Docker host notes](docs/docker-host.md)
- [The Pi agent itself](https://github.com/simonottosen/e-ink-display-client)
