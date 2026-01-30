# Deployment

## Docker (recommended)

### Quick start

```bash
docker compose up -d
```

This builds the image, starts the server on port 8000, and creates persistent volumes for firmware files and the SQLite database.

### docker-compose.yml configuration

```yaml
services:
  tachyon-mgmt:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./firmware:/app/firmware   # Firmware files
      - ./data:/app/data           # SQLite database
    environment:
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=changeme
      # - RADIUS_SERVER=radius.example.com
      # - RADIUS_SECRET=secret
      # - RADIUS_PORT=1812
    restart: unless-stopped
```

### Persistent data

| Volume | Container path | Contents |
|--------|---------------|----------|
| `./firmware` | `/app/firmware` | Uploaded firmware files |
| `./data` | `/app/data` | SQLite database (`updater.db`) |

Back up these directories to preserve your configuration, device inventory, and job history.

## Bare Metal

### Requirements

- Python 3.9+
- `curl` (used by TachyonClient for device HTTPS communication)
- `ping` (optional, for connectivity checks)

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### Run

```bash
# Using the entry point
firmware-updater

# Or directly
python -m updater.app

# Custom port
PORT=8080 firmware-updater
```

The server listens on `0.0.0.0:8000` by default.

### Development mode

```bash
uvicorn updater.app:app --reload --port 8000
```

## Environment Variables

### Authentication

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ADMIN_USERNAME` | Yes | — | Local admin username |
| `ADMIN_PASSWORD` | Yes | — | Local admin password (plaintext or bcrypt hash) |
| `RADIUS_SERVER` | No | — | RADIUS server hostname or IP |
| `RADIUS_SECRET` | No | — | RADIUS shared secret |
| `RADIUS_PORT` | No | `1812` | RADIUS server port |

When RADIUS is configured, it is tried first. If RADIUS fails or is unavailable, local auth is used as fallback.

To use a bcrypt-hashed password for `ADMIN_PASSWORD`, generate one with:

```bash
python -c "from passlib.hash import bcrypt; print(bcrypt.hash('yourpassword'))"
```

### Server

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `8000` | HTTP server port |

## Database

The application uses SQLite. The database file (`updater.db`) is created automatically on first run in the working directory (or `/app/data/` in Docker).

Schema migrations run automatically on startup. No manual database setup is required.

## Network Requirements

The server needs HTTPS access (port 443) to all managed devices. Tachyon devices use self-signed TLS certificates, which the client handles with curl's `-k` flag.

Optional outbound connections:
- `ip-api.com` and `zippopotam.us` for geolocation
- `api.weather.gov` for weather forecasts
- NTP servers for time validation
