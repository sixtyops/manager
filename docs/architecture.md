# Architecture

## Overview

SixtyOps Manager is a FastAPI application with an async-first architecture. The backend is Python, the frontend is server-rendered HTML with vanilla JavaScript, and real-time updates flow over WebSocket. Device-admin RADIUS authentication is provided by a built-in pyrad-based RADIUS server.

```
┌─────────────────────────────────────┐
│     Browser (HTML/JS Templates)     │
│  login.html │ monitor.html │ setup  │
└──────────────┬──────────────────────┘
               │ HTTP + WebSocket
┌──────────────▼──────────────────────┐
│       FastAPI App (app.py)          │
│  REST API │ WebSocket │ Templates   │
└──┬────┬───────┬───────┬─────────────┘
   │    │       │       │
┌──▼──┐ │ ┌────▼───┐ ┌─▼───┐
│Tele-│ │ │Schedul-│ │Poll-│
│metry│ │ │er      │ │er   │
└──┬──┘ │ └───┬────┘ └──┬──┘
   │    │     │          │
   ▼    ▼     │          │
 AWS  Slack   │          │
Lambda Webhook│          │
       ┌──────▼──────────▼──┐
       │  SQLite (database) │
       └──────┬─────────────┘
              │               HTTPS/curl
              ▼               ▼
      RADIUS Server (pyrad)  Network Devices
          (UDP 1812)
```

## Modules

### `app.py` - HTTP and WebSocket Server

The main FastAPI application. Handles all routing, serves HTML templates, manages WebSocket connections, and coordinates update jobs.

Key responsibilities:
- 30+ REST endpoints for sites, APs, CPEs, firmware, updates, settings, rollouts
- WebSocket broadcast to all connected clients
- Update job orchestration with phase-based device ordering
- App lifespan management (starts/stops poller, scheduler, release checks, and Radius log sync)
- Manages the in-process pyrad RADIUS server for built-in device-admin authentication

### `tachyon.py` - Device Communication

Low-level client for Tachyon hardware. Communicates over HTTPS using `curl` subprocesses (required for device SSL compatibility).

Update sequence per device:
1. Login and get session token
2. Fetch device info (model, firmware version, MAC, bank status)
3. Upload firmware file via multipart POST
4. Trigger installation with optional force/reset flags
5. Wait for reboot (poll until device responds)
6. Verify new firmware version

### `scheduler.py` - Automatic Update Scheduler

Background task that checks every 60 seconds whether to start an update. Enforces safety conditions before running:

- Schedule enabled and within configured day/time window
- System clock validated against NTP sources
- Weather conditions acceptable (optional temperature check)
- Firmware files selected
- Not already ran today

Manages gradual rollout progression across consecutive schedule windows.

### `poller.py` - Network Discovery

Background task that polls all APs every 60 seconds. Discovers connected CPEs, collects signal metrics, and broadcasts topology updates over WebSocket. Polls up to 5 APs concurrently.

### `database.py` - Data Layer

SQLite database with schema creation, migrations, and CRUD helpers for inventory, jobs, auth, config backups, and Radius state. Radius-related tables include `radius_users`, `radius_auth_log`, and `radius_client_overrides`.

### `auth.py` - Authentication

Web login authentication for the management UI. Supports local username/password and optional OIDC SSO. Sessions are stored in SQLite with a 24-hour TTL and validated for both HTTP and WebSocket requests.

### `builtin_radius.py` - Device Admin RADIUS Control Plane

Application-side management for the built-in pyrad RADIUS server used by APs, switches, and other managed devices.

Key responsibilities:
- Stores built-in RADIUS users, auth history, and manual client overrides in SQLite
- Reads configuration directly from the database — no config file generation
- Manages the in-process pyrad server lifecycle (start/stop/restart on config changes)
- Logs authentication results directly to SQLite for stats and audit history
- Enforces reserved usernames (`admin` and `root`) for device-admin RADIUS accounts

### `models.py` - Data Models

Pydantic models for API validation: `Device`, `CPEInfo`, `APWithCPEs`, `NetworkTopology`. Enums for `DeviceType` and `SignalHealth`.

### `services.py` - External Integrations

Helpers for IP geolocation, weather forecasts (weather.gov), timezone detection, and NTP time validation.

### `telemetry.py` - Anonymous Usage Telemetry

Sends anonymized job statistics to an AWS Lambda endpoint after each update job completes. Runs as a fire-and-forget background task that never blocks the main flow.

**What is sent:** event type, timestamp, anonymous install ID (hashed), device counts (success/failed/skipped/cancelled), success rate, duration, bank mode, scheduled vs manual, device model distribution, categorized error counts (timeout, connection, auth, upload, install, reboot, verification), and per-role (AP/CPE/switch) breakdowns.

**What is never sent:** IP addresses, MAC addresses, hostnames, credentials, location, or raw error messages.

**Disabling telemetry:** Set the `DISABLE_TELEMETRY=1` environment variable (e.g., in `docker-compose.yml`), then restart the app.

### `slack.py` - Slack Notifications

Sends rich webhook notifications on job completion with success/failure counts, failed device details, rollout phase progress, and next scheduled job info. Configured via `slack_webhook_url` in settings.

### `sftp_backup.py` - System Backup & Restore

SFTP-based backup system for the management database, settings, and device configurations.

Key responsibilities:
- **Scheduled Backups**: Creates a compressed `tar.gz` archive containing the SQLite database, device inventory, and configuration snapshots.
- **Remote Storage**: Uploads backups to a configured SFTP server with support for password or SSH key authentication.
- **Retention**: Automatically prunes older backups based on a configurable retention count.
- **Restore Flow**: Provides an API to list remote backups and restore the local database from a selected archive (requires system restart).

### `features.py` - Feature Classification

Defines the `Feature` enum and classifies features as stable or dangerous. All features are always enabled — there is no license gating or remote validation.

Key concepts:
- **Feature enum**: All 15 features (firmware updates, config management, notifications, auth, etc.)
- **Dangerous classification**: 6 features that make sweeping network changes are labeled "dangerous" in the UI: `CONFIG_BACKUP`, `CONFIG_TEMPLATES`, `CONFIG_COMPLIANCE`, `CONFIG_PUSH`, `RADIUS_AUTH`, `SSO_OIDC`
- **No-op dependencies**: `require_feature()` and `require_pro()` remain in 60+ endpoint signatures but do nothing — kept for minimal diff and future extensibility
- **Backward compatibility**: `license.py` re-exports everything from `features.py` so existing imports continue to work

### `release_checker.py` - Self-Update

Background service that checks GitHub Releases API for new versions. Compares the current `__version__` against the latest release tag. When a newer version is found, broadcasts a notification over WebSocket. Admins can apply the update from the Settings UI, which pulls the latest Docker image and recreates the container.

Respects appliance version compatibility: if a release's notes contain `<!-- min_appliance_version: X.Y -->`, the update is blocked on appliances running an older platform version.

## Feature Classification

All 15 features are always enabled. Six features that make sweeping changes to network devices or authentication infrastructure are classified as "dangerous" and shown with an amber badge in the UI:

| Dangerous Feature | Why |
|-------------------|-----|
| `CONFIG_BACKUP` | Backup/restore can overwrite device state |
| `CONFIG_TEMPLATES` | Templates push config patterns fleet-wide |
| `CONFIG_COMPLIANCE` | Auto-enforces config drift corrections across all devices |
| `CONFIG_PUSH` | Pushes configuration changes to live production devices |
| `RADIUS_AUTH` | Changes authentication infrastructure for all managed devices |
| `SSO_OIDC` | Changes login/authentication for the management UI |

The remaining 9 features (firmware updates, Slack, SNMP, webhooks, device portal, history, sites, beta firmware, firmware holds) are stable and have no special label.

## Key Design Decisions

**Async everywhere** - All I/O uses asyncio. Device communication runs curl as async subprocesses. Database calls are synchronous but fast (local SQLite).

**WebSocket broadcast** - All connected clients receive every status update. The server maintains a set of active WebSocket connections and broadcasts to all on any state change.

**Phase-based updates** - Devices are grouped into phases (CPEs first, then APs, then second bank pass) to maintain network connectivity during updates.

**Gradual rollout** - Scheduled updates use a canary pattern. The first night updates 1 AP to learn the target firmware version. Subsequent nights scale to 10%, 50%, and 100%. Any failure pauses the rollout.

**curl for device communication** - Tachyon devices require specific TLS handling that is simplest to achieve with curl's `-k` flag for self-signed certs.

## Frontend

Six HTML templates rendered by Jinja2:
- `login.html` - Authentication form
- `monitor.html` - Main UI with firmware, update, auto-update, and network topology
- `setup.html` - Initial site configuration
- `setup_wizard.html` - Guided first-run setup
- `backup_setup.html` - Backup configuration
- `ssl_setup.html` - SSL certificate setup

JavaScript in `static/js/` handles WebSocket connections and real-time DOM updates. No build step or framework dependencies.
