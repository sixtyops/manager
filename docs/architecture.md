# Architecture

## Overview

Charlotte is a FastAPI application with an async-first architecture. The backend is Python, the frontend is server-rendered HTML with vanilla JavaScript, and real-time updates flow over WebSocket.

```
┌─────────────────────────────────────┐
│     Browser (HTML/JS Templates)     │
│  login.html │ monitor.html │ setup  │
└──────────────┬──────────────────────┘
               │ HTTP + WebSocket
┌──────────────▼──────────────────────┐
│       FastAPI App (app.py)          │
│  REST API │ WebSocket │ Templates   │
└──────┬───────┬───────┬──────────────┘
       │       │       │
┌──────▼──┐ ┌──▼────┐ ┌▼───────────┐
│Scheduler│ │Poller │ │TachyonClient│
│scheduler│ │poller │ │tachyon.py   │
│  .py    │ │ .py   │ │             │
└────┬────┘ └───┬───┘ └──────┬──────┘
     │          │             │
┌────▼──────────▼─────┐      │ HTTPS/curl
│  SQLite (database.py)│      │
└──────────────────────┘      ▼
                         Network Devices
```

## Modules

### `app.py` - HTTP and WebSocket Server

The main FastAPI application. Handles all routing, serves HTML templates, manages WebSocket connections, and coordinates update jobs.

Key responsibilities:
- 30+ REST endpoints for sites, APs, CPEs, firmware, updates, settings, rollouts
- WebSocket broadcast to all connected clients
- Update job orchestration with phase-based device ordering
- App lifespan management (starts/stops poller and scheduler)

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

SQLite database with 9 tables. Handles schema creation, migrations, and all CRUD operations. Tables: `tower_sites`, `access_points`, `cpe_cache`, `sessions`, `settings`, `job_history`, `schedule_log`, `rollouts`, `rollout_devices`.

### `auth.py` - Authentication

Dual authentication: RADIUS (primary) with local username/password fallback. Sessions stored in SQLite with 24-hour TTL. Cookie-based session validation for both HTTP and WebSocket requests.

### `models.py` - Data Models

Pydantic models for API validation: `Device`, `CPEInfo`, `APWithCPEs`, `NetworkTopology`. Enums for `DeviceType` and `SignalHealth`.

### `services.py` - External Integrations

Helpers for IP geolocation, weather forecasts (weather.gov), timezone detection, and NTP time validation.

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
