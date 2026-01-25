# API Reference

All API endpoints require authentication unless noted. Authenticated requests must include a valid session cookie (`session_id`). Unauthenticated API requests return `401`; unauthenticated page requests redirect to `/login`.

## Authentication

### `GET /login`
Renders the login page. No auth required. Redirects to `/setup` on first run.

### `POST /login`
Authenticate and create a session. Rate-limited to 20 attempts per IP per 5 minutes.

- **Body**: `username` (form), `password` (form)
- **Response**: Redirect to `/` on success, re-render login with error on failure
- **Auth flow**: Local username/password for the management UI
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
Handle wizard steps (SSL certificate setup, SFTP backup, completion).

- **Body**: `step` (form), `action` (form), plus step-specific fields

### `GET /auth/oidc/login`
Start the OIDC login flow. No auth required. Rate-limited to 60 requests per IP per 5 minutes.

- **Response**: Redirect to the configured OIDC provider
- **Notes**: Only available when OIDC is configured

### `GET /auth/oidc/callback`
Complete the OIDC login flow. No auth required. Rate-limited to 60 requests per IP per 5 minutes.

- **Query**: Provider callback parameters such as `code`, `state`, or `error`
- **Response**: Redirect to `/` on success, back to `/login` on failure

### `GET /api/auth/config`
Get a summary of the authentication configuration used by the UI.

- **Response**: Built-in Radius summary, OIDC summary, and device-default credential summary

### `GET /api/auth/radius`
Get built-in Radius server configuration and a short stats snapshot. Classified as a dangerous feature.

- **Response**: `enabled`, `host`, `port`, `secret_set`, `configured`, `running`, `healthy`, `container_status`, `health_status`, `last_error`, `secret_last_rotated_at`, `secret_age_days`, `rotation_recommended`, `rotation_status`, `rotation_recommend_after_days`, and `stats`

### `PUT /api/auth/radius`
Update built-in Radius server settings. Classified as a dangerous feature.

- **Body** (JSON): `enabled`, `host`, `port`, `secret`
- **Validation**:
  - `host` is required when enabling the server
  - `secret` is required when enabling the server
  - If `secret` is omitted, the existing secret is preserved
- **Effect**: Updates RADIUS server configuration and restarts the in-process server

### `GET /api/auth/radius/users`
List built-in Radius users. Classified as a dangerous feature.

- **Response**: `{ "users": [{ "id", "username", "enabled", "created_at", "updated_at", "last_auth_at" }, ...] }`

### `POST /api/auth/radius/users`
Create a built-in Radius user. Classified as a dangerous feature.

- **Body** (JSON): `username`, `password`, `enabled`
- **Validation**:
  - `username` is required
  - Reserved usernames `admin` and `root` are rejected
  - `password` is required
- **Effect**: Updates RADIUS user config and restarts the in-process server

### `PUT /api/auth/radius/users/{user_id}`
Update a built-in Radius user. Classified as a dangerous feature.

- **Body** (JSON): `username`, `password`, `enabled`
- **Notes**: If `password` is omitted, the existing password is preserved

### `DELETE /api/auth/radius/users/{user_id}`
Delete a built-in Radius user. Classified as a dangerous feature.

- **Response**: `{ "success": true }`

### `GET /api/auth/radius/clients`
List manual built-in Radius client overrides. Classified as a dangerous feature.

- **Response**: `{ "clients": [{ "id", "client_spec", "shortname", "enabled", "created_at", "updated_at" }, ...] }`
- **Notes**: These overrides are merged with inventory-derived AP, switch, and CPE IPs when building the allowed client list

### `POST /api/auth/radius/clients`
Create a manual built-in Radius client override. Classified as a dangerous feature.

- **Body** (JSON): `client_spec`, `shortname`, `enabled`
- **Validation**: `client_spec` must be a valid IP address or CIDR

### `PUT /api/auth/radius/clients/{override_id}`
Update a manual built-in Radius client override. Classified as a dangerous feature.

- **Body** (JSON): `client_spec`, `shortname`, `enabled`

### `DELETE /api/auth/radius/clients/{override_id}`
Delete a manual built-in Radius client override. Classified as a dangerous feature.

- **Response**: `{ "success": true }`

### `GET /api/auth/radius/stats`
Get built-in Radius status, auth counters, and recent auth history. Classified as a dangerous feature.

- **Response**: `enabled`, `configured`, `running`, `healthy`, `container_status`, `health_status`, `port`, `secret_set`, `last_error`, `secret_last_rotated_at`, `secret_age_days`, `rotation_recommended`, `rotation_status`, `rotation_recommend_after_days`, `admin_accounts`, `known_clients`, `active_devices_24h`, `auth_success_rate`, `logins_today`, `recent_logins`
- **Notes**: Auth history is persisted directly in SQLite by the in-process RADIUS server

### `POST /api/auth/radius/secret-review`
Start tracking a legacy Radius shared secret from today without changing the secret value. Classified as a dangerous feature.

- **Response**: Updated built-in Radius config summary
- **Notes**:
  - Only works when a shared secret exists
  - Only available for older installs where the secret predates rotation tracking
  - This does not rotate the secret or push changes to devices

### `GET /api/auth/radius/rollout`
Get the current staged Radius device-migration rollout, if any. Classified as a dangerous feature.

- **Response**: `{ "rollout": null | { "id", "phase", "status", "pause_reason", "service_username", "created_at", "updated_at", "progress", "devices" } }`
- **Notes**: Current scope is enabled APs, switches, and CPEs whose inherited parent-AP credentials currently work. CPE entries include `parent_ap_ip` when available.

### `POST /api/auth/radius/rollout/start`
Start a staged Radius migration rollout for enabled APs, switches, and manageable CPEs. Classified as a dangerous feature.

- **Prerequisites**:
  - Built-in Radius enabled with a device host and shared secret
  - Saved and enabled Radius config template with `method=radius`
  - Saved Radius config template server, port, and secret must match the built-in Radius host, port, and secret
- **Behavior**:
  - Forces a fresh AP poll before rollout so CPE inventory and inherited-auth state are current
  - Uses the device's currently stored management credentials to push the Radius config
  - For CPEs, reuses the parent AP credentials because CPE credentials are not stored separately
  - Verifies the cutover by logging back into the device with the appliance's hidden Radius automation account
  - If a CPE cannot log in with inherited AP credentials, the rollout pauses and the operator must correct the AP credentials before resuming
  - Re-resolves remaining devices between phases so later CPE phases pick up updated parent AP credentials
  - Pauses automatically on failure

### `POST /api/auth/radius/rollout/{rollout_id}/resume`
Resume a paused Radius migration rollout. Classified as a dangerous feature.

### `POST /api/auth/radius/rollout/{rollout_id}/cancel`
Cancel an active or paused Radius migration rollout. Classified as a dangerous feature.

### `PUT /api/auth/device-defaults`
Update global default credentials used when talking to managed devices.

- **Body** (JSON): `enabled`, `username`, `password`
- **Notes**: If `password` is omitted, the existing password is preserved

### `GET /api/auth/oidc`
Get OIDC configuration for the management UI. Classified as a dangerous feature.

- **Response**: `enabled`, `provider_url`, `client_id`, `redirect_uri`, `allowed_group`, `scopes`, `configured`

### `PUT /api/auth/oidc`
Update OIDC configuration for the management UI. Classified as a dangerous feature.

- **Body** (JSON): `enabled`, `provider_url`, `client_id`, `client_secret`, `redirect_uri`, `allowed_group`, `scopes`
- **Notes**: If `client_secret` is omitted, the existing secret is preserved

### `POST /api/auth/test-oidc`
Test reachability of the configured OIDC discovery document. Classified as a dangerous feature.

## Pages

### `GET /`
Main dashboard (monitor view). Redirects to `/setup` or `/setup-wizard` if incomplete.

### `GET /ssl-setup`
SSL/TLS certificate configuration page.

### `POST /ssl-setup`
Request a Let's Encrypt certificate.

- **Body**: `domain` (form), `email` (form)

### `GET /backup-setup`
SFTP backup configuration page.

### `POST /backup-setup`
Configure SFTP backup server.

- **Body**: `host` (form), `port` (form), `path` (form), `username` (form), `auth_method` (form), `password` (form, optional), `ssh_key` (form, optional)

### `POST /backup-run`
Trigger an immediate SFTP backup.

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

`schedule_enabled`, `schedule_days`, `schedule_start_hour`, `schedule_end_hour`, `parallel_updates`, `bank_mode`, `allow_downgrade`, `timezone`, `zip_code`, `weather_check_enabled`, `min_temperature_c`, `temperature_unit`, `schedule_scope`, `schedule_scope_data`, `rollout_canary_aps`, `rollout_canary_switches`, `firmware_beta_enabled`, `firmware_quarantine_days`, `firmware_update_cooldown_days`, `slack_webhook_url`, `autoupdate_enabled`

- **Body** (JSON): Object of key-value pairs to set
- **Notes**:
  - `rollout_canary_aps` must contain enabled AP IPs in the effective rollout scope
  - `rollout_canary_switches` must contain enabled switch IPs in the effective rollout scope
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

### `POST /api/rollout/canary/trigger`
Start only the canary phase immediately, even outside the configured maintenance window.

- Uses the saved canary AP and switch settings when present
- Still enforces time validation, weather checks, and firmware hold/quarantine rules
- Still enforces rollout scope and canary validation
- Does not consume the nightly rollout window for later phases
- Does not apply the normal scheduled-job end-of-window cutoff

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

## Backup & Restore

### `GET /api/backup/status`
Get SFTP backup configuration and status. Classified as a dangerous feature.

### `POST /api/backup/run`
Trigger an immediate SFTP backup. Classified as a dangerous feature.

### `POST /api/backup/test-connection`
Test connectivity to the configured SFTP server. Classified as a dangerous feature.

### `GET /api/backup/list`
List available backup archives on the SFTP server. Classified as a dangerous feature.

### `POST /api/backup/restore`
Restore the management database from an SFTP backup archive. Classified as a dangerous feature.

- **Body** (form): `archive_name`

### `POST /api/backup/export`
Export device inventory as CSV with encrypted passwords.

- **Body** (JSON): `passphrase` (min 8 characters)
- **Response**: CSV file download

### `POST /api/backup/import`
Import device inventory from a CSV with encrypted passwords.

- **Body** (multipart form): `file`, `passphrase`, `conflict_mode` (`"skip"` or `"update"`)

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
    "release_url": "https://github.com/sixtyops/manager/releases/tag/v0.2.0",
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

### `POST /api/ssl/setup`
Request a Let's Encrypt certificate.

- **Body** (form): `domain`, `email`

## System

### `GET /api/system/info`
Get system information.

- **Response**: `{ "version", "appliance_mode", "appliance_version", "os", "os_version", "uptime_seconds", "disk_usage", "machine_id" }`

### `POST /api/system/network`
Update network configuration (appliance mode only). Requires admin role.

- **Body** (JSON): `mode` (`"dhcp"` or `"static"`), `address`, `netmask`, `gateway`, `dns`

## Features

### `GET /api/features`
Get the feature map with stability classification. All features are always enabled.

- **Response**: `{ "features": {...}, "feature_info": {...} }`

### `GET /api/license`
Get feature state. Backward-compatible shape — all features are unlocked.

- **Response**: `{ "tier", "status", "is_pro", "has_key", "features", "feature_info" }`

### `GET /api/license/instance-id`
Get the persistent instance ID. Requires admin role.

- **Response**: `{ "instance_id": "..." }`

## API Tokens

### `GET /api/tokens`
List API tokens for the current user (admins see all tokens).

- **Response**: `{ "tokens": [...] }`

### `POST /api/tokens`
Create a new API token. The token value is returned only once.

- **Body** (JSON): `name`, `scopes` (optional, `"read"` or `"read,write"`), `expires_days` (optional)
- **Response**: `{ "id", "token", "prefix", "name" }`

### `DELETE /api/tokens/{token_id}`
Delete an API token. Admin or token owner only.

## Audit Log

### `GET /api/audit-log`
Get audit log entries. Requires admin role.

- **Query**: `limit` (default 100), `offset` (default 0), `username` (optional), `action` (optional)
- **Response**: `{ "entries": [...], "total": ... }`

## Config Management

### `GET /api/configs`
List all devices with their latest config summary. Classified as a dangerous feature.

- **Response**: `{ "configs": { [ip]: { "id", "config_hash", "model", "fetched_at" } } }`

### `GET /api/configs/{ip}`
Get config snapshot history for a device.

- **Query**: `limit` (default 20)
- **Response**: `{ "history": [...] }`

### `GET /api/configs/{ip}/latest`
Get the latest config JSON for a device.

### `GET /api/configs/{ip}/snapshot/{config_id}`
Get a specific config snapshot.

### `GET /api/configs/{ip}/diff`
Diff two config snapshots for a device.

- **Query**: `a` (config_id), `b` (config_id)
- **Response**: `{ "diff_lines", "config_a", "config_b", "identical" }`

### `GET /api/configs/{ip}/download/{config_id}`
Download a config snapshot as a `.tar` file.

### `POST /api/configs/{ip}/poll`
Trigger immediate config fetch for one device. Requires admin or operator role.

### `POST /api/configs/poll`
Trigger immediate config fetch for all devices. Requires admin or operator role.

## Config Templates

### `GET /api/config-templates`
List all config templates. Classified as a dangerous feature.

- **Response**: `{ "templates": [...] }`

### `POST /api/config-templates`
Create a config template. Requires admin or operator role.

- **Body** (JSON): `name`, `category`, `config_fragment` (JSON), `form_data` (optional), `description` (optional), `scope` (optional, `"global"` or `"site"`), `site_id` (optional), `device_types` (optional)
- **Response**: `{ "id", "success" }`

### `PUT /api/config-templates/{template_id}`
Update a config template. Requires admin or operator role.

- **Body** (JSON): Any subset of `name`, `category`, `config_fragment`, `form_data`, `description`, `enabled`, `scope`, `site_id`, `device_types`

### `DELETE /api/config-templates/{template_id}`
Delete a config template. Requires admin or operator role.

## Config Compliance

### `GET /api/config-compliance`
Get per-device config compliance status using scoped templates. Classified as a dangerous feature.

- **Response**: `{ "devices": { [ip]: { "compliant", "checked_at" } } }`

### `GET /api/config-enforce/status`
Get current auto-enforce status and recent log entries.

- **Response**: `{ "enabled", "running", "failure_count", "recent" }`

### `GET /api/config-enforce/log`
Get config enforcement log entries.

- **Query**: `ip` (optional), `limit` (default 50)
- **Response**: `{ "entries": [...] }`

## Config Prefill

### `GET /api/config-prefill/{category}`
Get pre-fill data for a config category by analyzing fleet configs.

- **Supported categories**: `snmp`, `ntp`, `radius`, `users`, `syslog`, `watchdog`, `discovery`
- **Response**: `{ "prefilled", "data", "reason", "device_count", "match_count" }`

## Config Push

### `POST /api/config-push/preview`
Preview what a template merge would produce for a device, without pushing. Classified as a dangerous feature.

- **Body** (JSON): `ip`, `template_ids`
- **Response**: `{ "original", "merged", "diff_lines", "changed", "snapshot_date" }`

### `POST /api/config-push`
Push config template(s) to devices. Requires admin or operator role.

- **Body** (JSON): `template_ids`, `targets` (array of `{ "type", "ip" or "id" }`)
- **Response**: `{ "job_id", "device_count", "template_names" }`

### `GET /api/config-push/jobs/{job_id}`
Get status of an immediate config push job.

- **Response**: `{ "job_id", "total", "success", "failed", "cancelled", "done" }`

### `POST /api/config-push/jobs/{job_id}/cancel`
Cancel a running config push job.

### `POST /api/config-push/rollback/{ip}`
Rollback a device to a previous config snapshot. Requires admin or operator role.

- **Body** (JSON, optional): `config_id`
- **Response**: `{ "status", "ip", "rolled_back_to" }`

### `GET /api/config-push/rollout`
Get current config push rollout status.

- **Response**: `{ "rollout": { "progress", "devices", "total_target_devices", ... } | null }`

### `POST /api/config-push/rollout/start`
Start a phased config push rollout. Requires admin or operator role.

- **Body** (JSON): `template_ids`, `targets`
- **Response**: `{ "rollout_id", "device_count", "template_names" }`

### `POST /api/config-push/rollout/{rollout_id}/advance`
Manually advance to execute the next phase. Requires admin or operator role.

### `POST /api/config-push/rollout/{rollout_id}/resume`
Resume a paused config push rollout. Requires admin or operator role.

### `POST /api/config-push/rollout/{rollout_id}/cancel`
Cancel a config push rollout. Requires admin or operator role.

## Notifications

### `POST /api/snmp/test`
Send a test SNMP trap to verify configuration. Requires admin role.

- **Response**: `{ "success", "message" }`

### `POST /api/webhooks/test`
Send a test webhook to verify configuration. Requires admin role.

- **Response**: `{ "success", "message" }`

### `GET /api/syslog/status`
Get syslog forwarder status. Requires admin role.

### `POST /api/syslog/test`
Send a test syslog message. Requires admin role.

- **Response**: `{ "success", "message" }`

### `GET /api/email/status`
Get email notification configuration status. Requires admin role.

### `POST /api/email/test`
Send a test email to verify SMTP configuration. Requires admin role.

- **Response**: `{ "success", "message" }`

## Analytics

### `GET /api/analytics/summary`
Get aggregate update statistics over a time window.

- **Query**: `days` (1–365, default 90)

### `GET /api/analytics/trends`
Get daily success/failure trends.

- **Query**: `days` (1–365, default 30)
- **Response**: `{ "trends": [...] }`

### `GET /api/analytics/models`
Get update success/failure breakdown by device model.

- **Query**: `days` (1–365, default 90)
- **Response**: `{ "models": [...] }`

### `GET /api/analytics/errors`
Get top error messages from failed updates.

- **Query**: `days` (1–365, default 90), `limit` (1–200, default 10)
- **Response**: `{ "errors": [...] }`

### `GET /api/analytics/reliability`
Get per-device reliability stats, worst performers first.

- **Query**: `days` (1–365, default 90), `limit` (1–200, default 20)
- **Response**: `{ "devices": [...] }`

## Uptime

### `GET /api/uptime/device`
Get availability/uptime data for a specific device.

- **Query**: `ip` (required), `days` (1–365, default 30)

### `GET /api/uptime/fleet`
Get fleet-wide availability stats, worst performers first.

- **Query**: `device_type` (optional, `"ap"` or `"switch"`), `days` (1–365, default 30)
- **Response**: `{ "devices": [...] }`

### `GET /api/uptime/events`
Get raw uptime events for a device.

- **Query**: `ip` (required), `days` (1–365, default 30), `limit` (1–1000, default 100)
- **Response**: `{ "events": [...] }`

## Reports

### `GET /api/reports/update-summary`
Get update summary report.

- **Query**: `days` (1–365, default 30)

### `GET /api/reports/fleet-status`
Get fleet status report.

### `GET /api/reports/export/jobs`
Export job history as CSV download.

- **Query**: `days` (1–365, default 30)

### `GET /api/reports/export/devices`
Export device history as CSV download.

- **Query**: `days` (1–365, default 30)

## Device Groups

### `GET /api/device-groups`
List all device groups.

- **Response**: `{ "groups": [...] }`

### `POST /api/device-groups`
Create a device group. Requires admin or operator role.

- **Body** (JSON): `name`, `description` (optional), `filter_json` (optional)
- **Response**: `{ "id", "name" }` (201)

### `GET /api/device-groups/{group_id}`
Get a specific device group.

### `PUT /api/device-groups/{group_id}`
Update a device group. Requires admin or operator role.

- **Body** (JSON): Any subset of `name`, `description`, `filter_json`

### `DELETE /api/device-groups/{group_id}`
Delete a device group. Requires admin or operator role.

### `GET /api/device-groups/{group_id}/resolve`
Resolve device group to list of matching IPs.

- **Response**: `{ "group_id", "name", "device_ips", "count" }`

## Bulk Device Operations

### `POST /api/devices/bulk-enable`
Enable multiple devices. Requires admin or operator role.

- **Body** (JSON): `device_type` (`"ap"` or `"switch"`), `ips` (array)
- **Response**: `{ "success", "affected" }`

### `POST /api/devices/bulk-disable`
Disable multiple devices. Requires admin or operator role.

- **Body** (JSON): `device_type` (`"ap"` or `"switch"`), `ips` (array)
- **Response**: `{ "success", "affected" }`

### `POST /api/devices/bulk-delete`
Delete multiple devices. Requires admin role.

- **Body** (JSON): `device_type` (`"ap"` or `"switch"`), `ips` (array)
- **Response**: `{ "success", "deleted" }`

### `POST /api/devices/bulk-move`
Move multiple devices to a site. Requires admin or operator role.

- **Body** (JSON): `device_type` (`"ap"` or `"switch"`), `ips` (array), `site_id`
- **Response**: `{ "success", "affected" }`

## Device History

### `GET /api/device-history`
Get filterable device update/config history.

- **Query**: `ip` (optional), `action` (optional), `status` (optional), `from_date` (optional), `to_date` (optional), `limit` (default 100), `offset` (default 0)
- **Response**: `{ "history": [...], "total": ... }`

### `GET /api/job-history`
Get paginated job history summaries.

- **Query**: `page` (default 1), `per_page` (default 50)
- **Response**: `{ "jobs": [...], "total", "page", "per_page" }`

### `GET /api/job-history/{job_id}/devices`
Get device-level history for a specific job.

- **Response**: `{ "devices": [...] }`

## Device Portal

### `GET /api/device-portal/{ip}`
Auto-login portal: authenticates to a device and redirects to its web UI.

- **Response**: HTML page with auto-login form

## Vendors

### `GET /api/vendors`
Get registered vendor drivers and their firmware type metadata.

## Freeze Windows

### `GET /api/freeze-windows`
List all maintenance freeze windows.

- **Response**: `{ "windows": [...], "active_freeze": ... }`

### `POST /api/freeze-windows`
Create a maintenance freeze window. Requires admin role.

- **Body** (JSON): `name`, `start_date`, `end_date`, `reason` (optional)
- **Response**: `{ "id", "name" }`

### `PUT /api/freeze-windows/{window_id}`
Update a freeze window. Requires admin role.

- **Body** (JSON): Any subset of `name`, `start_date`, `end_date`, `reason`, `enabled`

### `DELETE /api/freeze-windows/{window_id}`
Delete a freeze window. Requires admin role.
