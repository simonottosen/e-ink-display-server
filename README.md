# Inky home display

## Technologies

- Python 3.12, FastAPI, SQLAlchemy, Pydantic, Pillow
- SQLite for application data
- Vanilla HTML, CSS, and JavaScript for the web UI
- Docker Compose for the host
- Raspberry Pi OS, Pimoroni Inky, and systemd for the display agent

## Run the host

Requirements: Git, Docker Engine, and the Docker Compose plugin.

```bash
git clone https://github.com/simonottosen/e-ink-display-server.git inky
cd inky
cp .env.example .env
docker compose -f deploy/docker/compose.yaml up -d --build host
```

Open `http://<host-ip>:8000`. The host stores SQLite data, uploaded images, and
generated display files in the named Docker volume `inky-host-data`.

To update an existing checkout:

```bash
git pull
docker compose -f deploy/docker/compose.yaml up -d --build host
```

## Fresh Debian or Ubuntu host

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo systemctl enable --now docker
git clone https://github.com/simonottosen/e-ink-display-server.git inky
cd inky
cp .env.example .env
docker compose -f deploy/docker/compose.yaml up -d --build host
```

The web host listens on port `8000` by default. Set `INKY_HOST_PORT` in `.env`
to publish a different port.

## Shared iCloud album

Paste a shared album link (`https://www.icloud.com/sharedalbum/#...`) into the
web UI and the host builds a shuffling album from it, re-checking every 15
minutes. The gallery mirrors the album: a photo removed in iCloud is removed
here too, including its file on disk, unless another album still uses it.

The link token is a bearer capability, so it is never returned by the API or
written to the logs.

## Persisting data outside Compose

`INKY_DATA_DIR` defaults to a relative `data` path, which resolves inside the
container's writable layer. Compose covers this already, but any other
deployment must both mount a volume and point the setting at it, or every
container recreate starts from an empty database:

```
volume:       /your/host/path:/var/lib/inky
environment:  INKY_DATA_DIR=/var/lib/inky
```

Mounting alone is not enough — without the environment variable the app keeps
writing to the old path and ignores the mount.

## Pi agent

See [the Pi agent notes](docs/pi-display-agent.md) and
[the Docker host notes](docs/docker-host.md).
