# Deployment

## Prerequisites

Tested on **Debian 12**. Other Linux distributions with Docker should work but are untested.

Required:
- Docker Engine 20.10+
- Docker Compose V2 (`docker compose`)
- git

## Quick Start

### Automated install (fresh server)

```bash
curl -sSL https://raw.githubusercontent.com/isolson/firmware-updater/main/scripts/install.sh | sudo bash
```

This installs Docker if needed, clones the repo to `/opt/tachyon`, builds and starts all services in standalone mode, and creates a systemd service for auto-start on boot.

### Manual install

```bash
git clone https://github.com/isolson/firmware-updater.git
cd firmware-updater
./deploy.sh
```

`deploy.sh` creates the required directories, builds the Docker images, and starts all services in standalone mode (app + nginx + certbot).

## Initial Setup

After starting the services, open `https://your-server-ip` in a browser. Accept the self-signed certificate warning — this is replaced with a real certificate in step 3.

**1. Create admin password**

Set your admin password on the first-run setup screen.

<!-- screenshot: password setup screen -->

**2. Dashboard**

After login you'll land on the main dashboard. The setup wizard runs automatically on first login.

<!-- screenshot: main dashboard -->

**3. Configure HTTPS (recommended)**

The setup wizard prompts for your domain and email to request a Let's Encrypt certificate. The bundled certbot service handles automatic renewal.

<!-- screenshot: SSL/Let's Encrypt wizard step -->

**4. Configure Git backups (optional)**

Point to a Git repository for automatic configuration backups.

<!-- screenshot: Git backup wizard step -->

**5. Add tower sites and devices**

Add your tower sites, then add APs by IP address with their credentials.

<!-- screenshot: add tower site / AP screen -->

**6. Upload firmware**

Upload firmware files on the Firmware tab. The system auto-detects which device models each file applies to.

<!-- screenshot: firmware upload screen -->

## Docker Compose Files

The project ships two compose files:

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Base: just the app on port 8000. Use behind your own reverse proxy. |
| `docker-compose.standalone.yml` | Overlay: adds nginx (80/443) and certbot. Use for standalone deployments. |

### Behind your own proxy (base only)

```bash
docker compose up -d --build
```

Starts only the application container on port 8000. You provide your own reverse proxy and TLS termination.

### Standalone mode (bundled nginx + Let's Encrypt)

```bash
docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build
```

Starts three services:

| Service | Image | Purpose |
|---------|-------|---------|
| `tachyon-mgmt` | Built from `Dockerfile` | The application (FastAPI on port 8000) |
| `nginx` | `nginx:alpine` | Reverse proxy, HTTPS termination, WebSocket upgrade |
| `certbot` | `certbot/certbot` | Let's Encrypt certificate auto-renewal (every 12h) |

`install.sh` and `deploy.sh` use standalone mode by default.

## Volumes & Data

All host paths (left side of `:`) can be changed to suit your setup.

### All modes

| Host path | Container path | Contents |
|-----------|---------------|----------|
| `./firmware` | `/app/firmware` | Uploaded firmware files |
| `./data` | `/app/data` | SQLite database (`tachyon.db`) |
| `./backups` | `/app/backups` | Git backup repository |

### Standalone mode (additional volumes)

| Host path | Container path | Contents |
|-----------|---------------|----------|
| `./nginx/conf.d` | `/etc/nginx/conf.d` (nginx) and `/app/nginx-conf` (app) | Nginx site config |
| `./nginx/ssl` | `/etc/nginx/ssl` | Self-signed certificate (initial boot) |
| `./certbot/www` | `/var/www/certbot` | ACME challenge files |
| `./certbot/conf` | `/etc/letsencrypt` | Let's Encrypt certificates |

The application container runs as `appuser` (UID/GID 1500). Docker bind mounts use host-side permissions, so these directories must be owned by UID 1500 for the container to write to them. The install and deploy scripts handle this automatically. If you create directories manually:

```bash
sudo chown 1500:1500 data firmware backups nginx/conf.d
sudo chmod 700 data
```

Back up `./data` and `./firmware` to preserve your database, device inventory, and firmware files.

## Environment Variables

### Authentication

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ADMIN_USERNAME` | Yes | — | Local admin username |
| `ADMIN_PASSWORD` | No | — | Admin password (plaintext or bcrypt hash). If unset, you'll be prompted to create one on first run via the web UI. |

To use a bcrypt-hashed password:

```bash
python -c "from passlib.hash import bcrypt; print(bcrypt.hash('yourpassword'))"
```

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP server port (inside the container) |
| `GITHUB_REPO` | `isolson/firmware-updater` | GitHub repo for auto-update checks |
| `AUTOUPDATE_CHECK_INTERVAL` | `604800` (7 days) | Seconds between release checks |

## Reverse Proxy

### Standalone nginx (bundled)

In standalone mode, the included nginx container handles everything out of the box:
- HTTP → HTTPS redirect
- TLS 1.2/1.3 with strong ciphers
- WebSocket upgrade at `/ws`
- `client_max_body_size 500M` for firmware uploads
- Security headers (X-Frame-Options, X-Content-Type-Options, etc.)
- Let's Encrypt ACME challenge passthrough

No additional configuration is needed.

### Using your own reverse proxy

Use the base `docker-compose.yml` without the standalone overlay:

```bash
docker compose up -d --build
```

The app listens on `localhost:8000`. Configure your proxy to forward to it with these requirements:

| Requirement | Value | Why |
|-------------|-------|-----|
| Upstream | `http://localhost:8000` | App listens on port 8000 |
| WebSocket upgrade | `/ws` path, long timeout | Real-time status updates |
| Max body size | 500 MB | Firmware file uploads |
| Forwarded headers | `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto` | IP-based rate limiting, HTTPS detection |

**Caddy example** (`Caddyfile`):

```
tachyon.example.com {
    reverse_proxy localhost:8000
}
```

Caddy handles WebSocket upgrade, large uploads, and HTTPS certificates automatically.

**nginx example**:

```nginx
server {
    listen 443 ssl http2;
    server_name tachyon.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    client_max_body_size 500M;

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### No reverse proxy (direct port access)

For testing or internal networks where TLS is handled elsewhere (e.g., a VPN), use the base compose file and access the app directly over HTTP:

```bash
docker compose up -d --build
# Access on http://your-server:8000
```

This works but has no TLS — traffic including login credentials is sent in plaintext. A reverse proxy with HTTPS is recommended for any production or internet-facing deployment.

## SSL/TLS

In standalone mode, the nginx entrypoint generates a self-signed certificate on first boot so HTTPS works immediately. When you configure Let's Encrypt through the setup wizard (or the SSL setup page), certbot requests a real certificate and the nginx config is updated automatically.

The certbot container checks for renewal every 12 hours. Certificates renew automatically before expiry.

## Bare Metal

If you prefer to run without Docker:

### Requirements

- Python 3.9+
- `curl` (device HTTPS communication)
- `ping` (optional, connectivity checks)

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

The server listens on `0.0.0.0:8000` by default. You will need to provide your own reverse proxy and TLS termination.

### Development mode

```bash
uvicorn updater.app:app --reload --port 8000
```

## Database

SQLite. The database file (`tachyon.db`) is created automatically on first run in the working directory (or `/app/data/` in Docker).

Schema migrations run automatically on startup. No manual database setup is required.

## Network Requirements

The server needs HTTPS access (port 443) to all managed devices. Tachyon devices use self-signed TLS certificates, which the client handles with curl's `-k` flag.

Optional outbound connections:
- `ip-api.com` and `zippopotam.us` for geolocation
- `api.weather.gov` for weather forecasts
- `api.github.com` for auto-update release checks
- NTP servers for time validation

## Auto-Updates

The application can check GitHub for new releases and update itself automatically when running in Docker.

### How it works

1. A background service checks `https://api.github.com/repos/{GITHUB_REPO}/releases/latest` on a configurable interval (default: every 7 days)
2. If a newer version is found, the UI displays a notification via WebSocket
3. An admin can apply the update from the UI or via `POST /api/updates/apply`
4. The update pulls the latest Docker image and recreates the container

### Safety checks

The system will **not** apply updates when:
- A firmware rollout is in progress or paused
- The current time falls within a configured firmware maintenance window

### Docker requirements

For automatic updates, the container needs access to the Docker socket and compose file(s):

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - ./docker-compose.yml:/app/docker-compose.yml:ro
```

In standalone mode, the overlay also mounts `docker-compose.standalone.yml`. The auto-updater detects this file and includes it automatically when running `docker compose up`.

If the Docker socket is not mounted, the API returns manual commands to run on the host instead.

Enable or disable auto-update checking in the Settings UI (`autoupdate_enabled`).

## Publishing a Release

When you're ready to publish a new version that the auto-update system can detect:

### 1. Bump the version

Edit `updater/__init__.py`:

```python
__version__ = "0.2.0"
```

### 2. Commit and tag

```bash
git add updater/__init__.py
git commit -m "Bump version to 0.2.0"
git tag v0.2.0
git push origin main --tags
```

### 3. Create the GitHub release

```bash
gh release create v0.2.0 --title "v0.2.0" --notes "Release notes here..."
```

Or create the release through the GitHub web UI at **Releases > Draft a new release**.

### 4. Build and push the Docker image (if using a registry)

```bash
docker build -t ghcr.io/isolson/firmware-updater:0.2.0 -t ghcr.io/isolson/firmware-updater:latest .
docker push ghcr.io/isolson/firmware-updater:0.2.0
docker push ghcr.io/isolson/firmware-updater:latest
```

If building locally on the deployment host, `docker compose build` is sufficient.

### Release tag format

The auto-update system expects tags in the format `v*.*.*` (e.g., `v0.2.0`). The leading `v` is stripped for version comparison using semantic versioning.
