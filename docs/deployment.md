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

This installs Docker if needed, clones the repo to `/opt/tachyon`, builds and starts all services, and creates a systemd service for auto-start on boot.

### Manual install

```bash
git clone https://github.com/isolson/firmware-updater.git
cd firmware-updater
./deploy.sh
```

`deploy.sh` creates the required directories, builds the Docker images, and starts all three services (nginx, certbot, app).

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

## Docker Compose Services

The default `docker-compose.yml` runs three services:

| Service | Image | Purpose |
|---------|-------|---------|
| `nginx` | `nginx:alpine` | Reverse proxy, HTTPS termination, WebSocket upgrade |
| `certbot` | `certbot/certbot` | Let's Encrypt certificate auto-renewal (every 12h) |
| `tachyon-mgmt` | Built from `Dockerfile` | The application (FastAPI on port 8000) |

Nginx listens on ports 80 and 443. The app container only exposes port 8000 internally to nginx — it is not directly accessible from outside.

## Volumes & Data

All host paths (left side of `:`) can be changed to suit your setup. For example, use `/srv/tachyon/data` instead of `./data` if you prefer a different location.

| Host path | Container path | Contents |
|-----------|---------------|----------|
| `./firmware` | `/app/firmware` | Uploaded firmware files |
| `./data` | `/app/data` | SQLite database (`updater.db`) |
| `./backups` | `/app/backups` | Git backup repository |
| `./nginx/conf.d` | `/etc/nginx/conf.d` (nginx) and `/app/nginx-conf` (app) | Nginx site config |
| `./nginx/ssl` | `/etc/nginx/ssl` | Self-signed certificate (initial boot) |
| `./certbot/www` | `/var/www/certbot` | ACME challenge files |
| `./certbot/conf` | `/etc/letsencrypt` | Let's Encrypt certificates |

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

### Bundled nginx (default)

The included nginx container handles everything out of the box:
- HTTP → HTTPS redirect
- TLS 1.2/1.3 with strong ciphers
- WebSocket upgrade at `/ws`
- `client_max_body_size 500M` for firmware uploads
- Security headers (X-Frame-Options, X-Content-Type-Options, etc.)
- Let's Encrypt ACME challenge passthrough

No additional configuration is needed for the default setup.

### Using your own reverse proxy

If you already run a reverse proxy (Caddy, Traefik, another nginx, etc.), you can remove the bundled nginx and certbot services and expose the app directly.

**1. Modify `docker-compose.yml`:**

Remove or comment out the `nginx` and `certbot` services. Expose port 8000 on `tachyon-mgmt`:

```yaml
services:
  tachyon-mgmt:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./firmware:/app/firmware
      - ./data:/app/data
      - ./backups:/app/backups
      - /var/run/docker.sock:/var/run/docker.sock
      - ./docker-compose.yml:/app/docker-compose.yml:ro
    environment:
      - ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
    restart: unless-stopped
```

**2. Configure your proxy** with these requirements:

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

**nginx example** (standalone):

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

For testing, internal networks, or environments where TLS is handled elsewhere (e.g., a VPN), you can run the app container alone and access it directly over HTTP.

Use the same simplified `docker-compose.yml` from the section above, and map to any host port you want:

```yaml
services:
  tachyon-mgmt:
    build: .
    ports:
      - "9090:8000"    # access on http://your-server:9090
    volumes:
      - ./firmware:/app/firmware
      - ./data:/app/data
      - ./backups:/app/backups
      - /var/run/docker.sock:/var/run/docker.sock
      - ./docker-compose.yml:/app/docker-compose.yml:ro
    environment:
      - ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
    restart: unless-stopped
```

This works but has no TLS — traffic including login credentials is sent in plaintext. A reverse proxy with HTTPS is recommended for any production or internet-facing deployment.

## SSL/TLS

On first boot, the nginx entrypoint generates a self-signed certificate so HTTPS works immediately. When you configure Let's Encrypt through the setup wizard (or the SSL setup page), certbot requests a real certificate and the nginx config is updated automatically.

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

SQLite. The database file (`updater.db`) is created automatically on first run in the working directory (or `/app/data/` in Docker).

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

For automatic updates, the container needs access to the Docker socket and compose file:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - ./docker-compose.yml:/app/docker-compose.yml:ro
```

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
