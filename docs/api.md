# API Reference

All API endpoints require authentication unless noted. Authenticated requests must include a valid session cookie (`session_id`). Unauthenticated API requests return `401`; unauthenticated page requests redirect to `/login`.

## Authentication

### `GET /login`
Renders the login page. No auth required. Redirects to `/setup` on first run.

### `POST /login`
Authenticate and create a session. Rate-limited to 5 attempts per IP per 60 seconds.

- **Body**: `username` (form), `password` (form)
- **Response**: Redirect to `/` on success, re-render login with error on failure
- **Auth flow**: Local username/password
- **Cookie set**: `session_id` (httponly, secure, samesite=lax, 24h TTL)

### `POST /logout`
Destroy the current session and redirect to `/login`. No auth required (operates on current cookie).

### `GET /setup`
Initial password setup page. Accessible without auth on first run only.

### `POST /setup`
Set or change admin password.

- **Body**: `new_password` (form), `confirm_password` (form), `current_password` (form, required after first run)
- **Validation**: Minimum 8 characters, passwords must match

### `GET /setup-wizard`
Multi-step setup wizard for SSL and backup configuration. Auth required.

### `POST /setup-wizard`
Handle wizard steps (SSL certificate setup, Git backup, completion).

- **Body**: `step` (form), `action` (form), plus step-specific fields

## Pages

### `GET /`
Main dashboard (monitor view). Redirects to `/setup` or `/setup-wizard` if incomplete.

### `GET /ssl-setup`
SSL/TLS certificate configuration page.

### `POST /ssl-setup`
Request a Let's Encrypt certificate.

- **Body**: `domain` (form), `email` (form)

### `GET /backup-setup`
Git backup configuration page.

### `POST /backup-setup`
Configure Git backup repository.

- **Body**: `repo_url` (form), `auth_method` (form), `token` (form, optional), `ssh_key` (form, optional)

### `POST /backup-run`
Trigger an immediate Git backup.

## WebSocket

### `WebSocket /ws`
Real-time bidirectional connection. Requires valid session cookie.

On connect, sends: current topology, active job state, job history (last 20), scheduler status, and rollout status.

**Server message types:**
- `topology_update` — AP/CPE/switch discovery data
- `device_update` — Single device update progress
- `job_started` — Update job initiated
- `job_completed` — Update job finished with summary
- `job_history` — Historical job record
- `scheduler_status` — Scheduler state change
- `rollout_status` — Rollout phase change
- `ping` — Keep-alive (sent after 5 min idle)

## Tower Sites

### `GET /api/sites`
List all tower sites.

- **Response**: `{ "sites": [...] }`

### `POST /api/sites`
Create a tower site.

- **Body** (form): `name`, `location` (optional), `latitude` (optional), `longitude` (optional)
- **Response**: `{ "id": ..., "name": "..." }`

### `PUT /api/sites/{site_id}`
Update a tower site.

- **Body** (form): `name`, `location`, `latitude`, `longitude`

### `DELETE /api/sites/{site_id}`
Delete a tower site. Associated APs/switches are unlinked (not deleted).

## Access Points

### `GET /api/aps`
List all access points with their tower site assignments. Device passwords are redacted.

- **Query**: `site_id` (optional, filter by tower site)
- **Response**: `{ "aps": [...] }`

### `POST /api/aps`
Add an access point. Triggers an immediate poll.

- **Body** (form): `ip`, `username`, `password`, `tower_site_id` (optional)
- **Response**: `{ "id": ..., "ip": "..." }`

### `PUT /api/aps/{ip}`
Update an access point's credentials, site assignment, or enabled state.

- **Body** (form): `username`, `password`, `tower_site_id`, `enabled`

### `DELETE /api/aps/{ip}`
Remove an access point and its cached CPEs.

### `POST /api/aps/{ip}/poll`
Trigger an immediate poll of a single AP. Returns success status.

## Devices

### `POST /api/devices`
Add a device with auto-classification. Probes the device to determine type (AP vs switch based on model), then inserts into the appropriate table.

- **Body** (form): `ip`, `username`, `password`, `tower_site_id` (optional)
- **Response**: `{ "id": ..., "ip": "...", "device_type": "ap"|"switch", "model": "..." }`

## Switches

### `GET /api/switches`
List all switches. Device passwords are redacted.

- **Query**: `site_id` (optional, filter by tower site)
- **Response**: `{ "switches": [...] }`

### `POST /api/switches`
Add a switch. Triggers an immediate poll.

- **Body** (form): `ip`, `username`, `password`, `tower_site_id` (optional)

### `PUT /api/switches/{ip}`
Update a switch's credentials, site assignment, or enabled state.

- **Body** (form): `username`, `password`, `tower_site_id`, `enabled`

### `DELETE /api/switches/{ip}`
Delete a switch.

### `POST /api/switches/{ip}/poll`
Trigger an immediate poll of a switch.

## Network Topology

### `GET /api/topology`
Get the full network topology: tower sites with their APs, CPEs, switches, plus aggregate stats.

### `POST /api/topology/refresh`
Trigger a full poll of all APs. Returns updated topology.

### `GET /api/cpes`
List all cached CPEs.

- **Response**: `{ "cpes": [...] }`

## Quick Add

### `POST /api/quick-add`
Quick-add an AP, optionally creating a new tower site.

- **Body** (form): `ip`, `username`, `password`, `site_name` (optional)
- **Response**: `{ "ap_id": ..., "site_id": ..., "ip": "..." }`

## Settings

### `GET /api/settings`
Get all configuration settings as key-value pairs. Sensitive values (password hashes, secrets) are redacted.

- **Response**: `{ "settings": {...}, "resolved_temperature_unit": "c"|"f" }`

### `PUT /api/settings`
Update settings. Only whitelisted keys are accepted:

`schedule_enabled`, `schedule_days`, `schedule_start_hour`, `schedule_end_hour`, `parallel_updates`, `bank_mode`, `allow_downgrade`, `timezone`, `zip_code`, `weather_check_enabled`, `min_temperature_c`, `temperature_unit`, `schedule_scope`, `schedule_scope_data`, `firmware_beta_enabled`, `firmware_quarantine_days`, `slack_webhook_url`, `autoupdate_enabled`

- **Body** (JSON): Object of key-value pairs to set
- **Response**: `{ "success": true }`

### `PUT /api/auth/device-defaults`
Update global default device credentials (used when communicating with APs/switches).

- **Body** (JSON): `enabled`, `username`, `password`

### `POST /api/slack/test`
Send a test notification to the configured Slack webhook.

## External Services

### `GET /api/time`
Get current time in the configured timezone and NTP validation status.

### `GET /api/weather`
Get weather forecast for the configured location. Returns temperature and conditions.

### `GET /api/location`
Auto-detect server location from IP address or configured zip code.

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

### `POST /api/rollout/{rollout_id}/reset`
Reset a paused rollout — cancels it so a fresh rollout starts next maintenance window.

## Firmware Files

### `POST /api/upload-firmware`
Upload a firmware file. Filename is validated against a safe character whitelist.

- **Body**: `file` (multipart form)
- **Response**: `{ "filename": "...", "size": ... }`

### `GET /api/firmware-files`
List all uploaded firmware files with name, size, modification date, source, channel, and hold period status.

### `DELETE /api/firmware-files/{filename}`
Delete a firmware file.

### `POST /api/firmware-fetch`
Trigger an on-demand firmware check and download from upstream.

### `POST /api/firmware-reselect`
Re-run firmware auto-selection (e.g., after toggling beta channel).

### `GET /api/firmware-fetch/status`
Get firmware fetch status (last check time, errors, auto-fetched files).

## Fleet Status

### `GET /api/fleet-status`
Get firmware version status for all devices (APs, CPEs, switches). Shows current vs target version, per-device status (`current`, `behind`, `unknown`), and fleet-wide summary.

## Firmware Updates

### `POST /api/start-update`
Start a firmware update job for multiple devices.

- **Body** (form):
  - `firmware_file` — filename for TNA-30x models (validated)
  - `device_type` — `"tachyon"`
  - `ip_list` — newline-separated IP addresses
  - `concurrency` — parallel update limit (default 2)
  - `firmware_file_303l` — filename for TNA-303L models (optional, validated)
  - `firmware_file_tns100` — filename for TNS-100 models (optional, validated)
  - `bank_mode` — `"both"` or `"one"`
- **Response**: `{ "job_id": "...", "device_count": ... }`

### `POST /api/update-device`
Start a firmware update for a single device (AP, CPE, or switch).

- **Body** (form): `ip`, `firmware_file`, `firmware_file_303l` (optional), `firmware_file_tns100` (optional), `bank_mode`
- **Response**: `{ "job_id": "...", "device_count": 1 }`

### `GET /api/job/{job_id}`
Get the status of an update job including per-device results.

## Backup & Export

### `POST /api/backup/export`
Export device inventory as CSV with encrypted passwords.

- **Body** (JSON): `passphrase` (min 8 characters)
- **Response**: CSV file download

### `POST /api/backup/import`
Import device inventory from a CSV with encrypted passwords.

- **Body** (multipart form): `file`, `passphrase`, `conflict_mode` (`"skip"` or `"update"`)

### `GET /api/backup/git-status`
Get Git backup configuration and status.

## App Updates

### `GET /api/updates`
Get current app update status.

- **Response**:
  ```json
  {
    "current_version": "0.1.0",
    "enabled": true,
    "last_check": "2026-01-15T03:00:00",
    "available_version": "0.2.0",
    "release_url": "https://github.com/isolson/firmware-updater/releases/tag/v0.2.0",
    "release_notes": "Bug fixes and new features...",
    "update_available": true,
    "docker_socket_available": true,
    "can_update": true,
    "blocked_reason": ""
  }
  ```
- `can_update` is `false` during active firmware rollouts or maintenance windows
- `docker_socket_available` indicates whether automatic updates can be applied

### `POST /api/updates/check`
Manually trigger a check for new releases on GitHub.

- **Response**:
  ```json
  {
    "current_version": "0.1.0",
    "latest_version": "0.2.0",
    "update_available": true,
    "release_url": "https://github.com/...",
    "release_notes": "...",
    "error": null
  }
  ```

### `POST /api/updates/apply`
Pull the latest Docker image and restart the container.

- Requires Docker socket to be mounted (`/var/run/docker.sock`)
- Blocked during active firmware rollouts or maintenance windows
- **Response (success)**:
  ```json
  { "success": true, "message": "Update started. The application will restart shortly." }
  ```
- **Response (blocked)**:
  ```json
  { "success": false, "message": "Cannot update now: ...", "blocked_reason": "..." }
  ```
- **Response (no Docker socket)**:
  ```json
  { "success": false, "manual": true, "message": "...", "commands": ["docker compose pull tachyon-mgmt", "docker compose up -d tachyon-mgmt"] }
  ```

## SSL

### `GET /api/ssl/status`
Get SSL certificate status (enabled, domain, expiry).
