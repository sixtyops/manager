# API Reference

All API endpoints require authentication unless noted. Authenticated requests must include a valid session cookie.

## Authentication

### `GET /login`
Renders the login page. No auth required.

### `POST /login`
Authenticate and create a session.

- **Body**: `username` (form), `password` (form)
- **Response**: Redirect to `/` on success, re-render login with error on failure
- **Auth**: RADIUS first, then local fallback

### `POST /logout`
Destroy the current session and redirect to `/login`.

## Pages

### `GET /`
Main UI (Auto-Update tab). Renders `index.html`.

### `GET /firmware`
Firmware file management page. Renders `firmware.html`.

### `GET /update`
Manual update page. Renders `index.html` with the Update tab active.

## WebSocket

### `WebSocket /ws`
Real-time bidirectional connection. On connect, sends current topology, scheduler status, and rollout state.

**Server message types:**
- `topology_update` - AP/CPE discovery data
- `device_update` - Single device update progress
- `job_completed` - Update job finished
- `scheduler_status` - Scheduler state change
- `rollout_status` - Rollout phase change

## Tower Sites

### `GET /api/sites`
List all tower sites.

### `POST /api/sites`
Create a tower site.
- **Body** (JSON): `name`, `location` (optional), `latitude` (optional), `longitude` (optional)

### `PUT /api/sites/{site_id}`
Update a tower site.
- **Body** (JSON): `name`, `location`, `latitude`, `longitude`

### `DELETE /api/sites/{site_id}`
Delete a tower site and unlink its APs.

## Access Points

### `GET /api/aps`
List all access points with their tower site assignments.

### `POST /api/aps`
Add an access point.
- **Body** (JSON): `ip`, `username`, `password`, `tower_site_id` (optional), `enabled` (optional)

### `PUT /api/aps/{ip}`
Update an access point's credentials, site assignment, or enabled state.
- **Body** (JSON): `username`, `password`, `tower_site_id`, `enabled`

### `DELETE /api/aps/{ip}`
Remove an access point and its cached CPEs.

### `POST /api/aps/{ip}/poll`
Trigger an immediate poll of a single AP. Returns device info and CPE count.

## Network Topology

### `GET /api/topology`
Get the full network topology: tower sites with their APs and CPEs, plus aggregate stats.

### `POST /api/topology/refresh`
Trigger a full poll of all APs. Returns immediately; results arrive via WebSocket.

### `GET /api/cpes`
List all cached CPEs, optionally filtered by `ap_ip` query parameter.

## Quick Add

### `POST /api/quick-add`
Bulk-add APs from a text block. Parses lines as `ip`, `ip username password`, or `ip:port username password`.
- **Body** (JSON): `text`, `tower_site_id` (optional)

## Settings

### `GET /api/settings`
Get all configuration settings as key-value pairs.

### `PUT /api/settings`
Update settings.
- **Body** (JSON): Object of key-value pairs to set

## External Services

### `GET /api/time`
Get current time in the configured timezone and NTP validation status.

### `GET /api/weather`
Get weather forecast for the configured location. Returns temperature and conditions.

### `GET /api/location`
Auto-detect server location from IP address. Returns city, region, lat/lon, timezone.

## Scheduler

### `GET /api/scheduler/status`
Get scheduler state including: status (`idle`, `running`, `blocked_weather`, etc.), next run info, and active rollout summary.

## Rollouts

### `GET /api/rollout/current`
Get the active or paused rollout with device progress counts.

### `POST /api/rollout/{rollout_id}/resume`
Resume a paused rollout. Only works if status is `paused`.

### `POST /api/rollout/{rollout_id}/cancel`
Cancel an active or paused rollout.

## Firmware Files

### `POST /api/upload-firmware`
Upload a firmware file.
- **Body**: `file` (multipart form)
- **Response**: `{ "filename": "...", "size": ... }`

### `GET /api/firmware-files`
List all uploaded firmware files with name, size, and modification date.

### `DELETE /api/firmware-files/{filename}`
Delete a firmware file.

## Updates

### `POST /api/start-update`
Start a firmware update job.
- **Body** (JSON):
  - `device_type`: `"tachyon"`
  - `ips`: list of IP addresses
  - `firmware_file`: filename for TNA-30x models
  - `firmware_file_303l`: filename for TNA-303L models (optional)
  - `firmware_file_tns100`: filename for TNS-100 models (optional)
  - `concurrency`: parallel update limit (default 2)
  - `bank_mode`: `"both"` or `"single"`
  - `force`: force install even if version matches (default false)
  - `scheduled`: whether this was triggered by the scheduler
- **Response**: `{ "job_id": "..." }`

### `GET /api/job/{job_id}`
Get the status of an update job including per-device results.
