# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Added
- Switch → AP topology cascade: APs are now nested under their upstream switch in the device tree, with a port badge showing the switch port they're connected to (ordered by port number)
- OIDC admin group mapping: configure an "Admin Group" in SSO settings to auto-promote members to admin role on login
- Role badge in the header shows the current user's role; write-operation UI (Add Devices, delete, bulk actions) is hidden from viewer accounts
- Initial config priming: devices without a cached config are polled on the next poll cycle instead of waiting until the 4 AM daily run, so compliance works from day one
- Check Compliance now triggers a fresh config poll (`?refresh=true`) with visible "Polling devices…" feedback instead of reading stale cache
- NTP server defaults (132.163.97.1 and 129.6.15.28) in the config template editor; toggle stays off by default
- Toast notification CSS (fixed top-right, typed color borders, slide-in) — previously toasts rendered as unstyled plaintext in the page body
- Last-admin / self-delete guards on the Local Users table (disabled button + tooltip)
- Config snapshot recycle bin: deleting a device now soft-deletes its config history rather than orphaning rows. Deleting an AP also cascades to its CPEs' snapshots. A new "Config Snapshot Recycle Bin" panel in the Config drawer lets admins restore or permanently purge entries
- MAC-based config-history auto-rebind: when a managed device's IP changes (DHCP renumber, replacement at the same MAC), its prior config history is automatically re-linked to the new IP. The UI surfaces a toast and refreshes when this happens
- Manager backup export now includes device config snapshots and the recycle bin (Fernet-encrypted with the same passphrase). Re-import is idempotent on `(ip, fetched_at)` so DR no longer resets config history

### Changed
- `docker-compose.yml` now publishes ports through env-overridable defaults: `${BIND_IP:-0.0.0.0}:${HOST_PORT:-8000}:8000` and `${BIND_IP:-0.0.0.0}:${RADIUS_HOST_PORT:-1812}:1812/udp`. Default behavior is unchanged (binds `0.0.0.0:8000` and `0.0.0.0:1812/udp`). Operators on multi-tenant hosts can now set `BIND_IP=<host-ip>` / `HOST_PORT=<port>` / `RADIUS_HOST_PORT=<port>` in their environment instead of hand-deleting the upstream `ports:` block — which would leave the working tree dirty and break the in-app self-update path on every release
- Manual config push (`/api/config-push` and `/api/config-push/preview`, including phased rollouts) now honors each template's `device_types` filter — an AP-only template targeted at a switch is reported as "skipped" in preview and silently bypassed at apply, instead of being merged into a config it doesn't belong in. The push job and rollout state expose a new `skipped` counter alongside `success`/`failed`.
- Config tar download (`/api/configs/{ip}/download/{config_id}`) now writes the CONTROL file as a `key=value` manifest (`hardware_id`, `fetched_at`, `config_hash`, `manager_version`) instead of just the bare hardware id, so a future re-import path can verify the snapshot's origin and integrity
- Bridge/FDB table polled from Tachyon switches on each poll cycle to maintain AP-to-port mapping
- Chassis connector replaced with inline `eth[n]` port badge on nested AP rows (cleaner, no orphaned line art)
- System > Updates panel normalized into a label/control grid; RADIUS Server stat cards removed; RADIUS Clients & Logs rewritten for clarity; About panel redesigned with inline version chip

### Removed
- Appliance build infrastructure (OVA/QCOW2 image generation, Packer configs, build-appliance workflow)

### Fixed
- Self-update now detects uncommitted local changes to tracked files in the manager repo *before* attempting `git checkout` and returns a structured `{success: false, dirty_tree: true, dirty_files: [...], suggested_command}` response instead of the opaque "Your local changes would be overwritten by checkout" error. Untracked files are not flagged because they don't block checkout. Operators get a clear message and a copy-pasteable `git stash` command instead of having to read the raw git error
- OIDC user roles edited in the Local Users table reverted to the `oidc_default_role` (or `viewer`) on the user's next login. Now the IdP is the source of truth only when an `admin_group` is configured; with no `admin_group`, manual UI changes persist across logins. The Local Users table also disables the role dropdown for OIDC users when an `admin_group` is configured (with a tooltip explaining the role is IdP-managed), and shows a toast confirmation when a role/enable change is saved
- Config tab summary chips ("0 Devices / Compliant / Non-Compliant / Unchecked") stayed at zero on initial page load when the WebSocket topology arrived after `loadConfigData()`; `updateUI()` now re-runs `updateConfigStats()` whenever topology updates so the chips reflect the live fleet
- Signal vs Distance chart silently swallowed CPEs that hadn't been polled yet — the `-100` fallback fell below the `-75` y-axis minimum, hiding the dot. CPEs without signal data now floor at `-88` and the y-axis extends to `-90` so Critical-tier and unpolled CPEs are visible; tooltip distinguishes "Signal: not yet polled" from real readings (also switched to `??` so a real `0` reading isn't dropped)
- "Update Available" banner in Settings > Updates stayed hidden even when an update was detected (inline `display:none` overrode the `.hidden` class toggle)
- Switch → AP topology cascade wasn't populating because `TachyonDriver` didn't expose `get_bridge_table()`; added passthrough so bridge entries are stored and APs render under their upstream switch
- Tachyon config GET/POST hit the wrong endpoint (`/cgi.lua/apiv1/config`) and returned HTTP 401 "Authorization Failed"; corrected to `/cgi.lua/config` so config backup, compliance, and push all work
- Signal vs Distance chart was empty whenever every AP at a site sat behind a managed switch — `updateChart()` only walked `site.aps[]` and missed `site.switches[].aps[]`; now iterates both, and selecting a switch in the topology scopes the chart to its nested APs
- Topology index (`rebuildTopologyIndex`) skipped APs and CPEs nested under managed switches, so `findAP`/`findCPE` returned null for those rows — broke "Edit notes", AP/CPE checkbox selection, and chart point highlight for switch-nested devices
- Site-wide iterators (Config tab badge, model/firmware filter dropdowns, "X Devices / Y Compliant" bar, site/all checkbox toggles + indeterminate state, CPE preservation across polls) walked only `site.aps[]` and undercounted or skipped switch-nested APs/CPEs; now traverse both branches via a shared `walkSiteAPs` helper, so site-row checkboxes also toggle every nested device row
- "Update site" firmware action and the config-push paths (rollout target builder, preview device picker — which also had a `site.access_points` typo — and "Push to selected") missed APs and CPEs nested under managed switches; switch-nested devices now flash with their site, count toward the confirmation summary, and resolve correctly for config push. "Push to selected" now drops unknown IPs with a toast instead of silently sending them as `type: 'ap'`
- Config-push rollout `all_aps` scope was sending `{type: 'site', id}` per site, which the backend resolves to APs + CPEs + switches — so picking "all APs" silently pushed to CPEs and switches too. The rollout target builder now enumerates per role for `all_aps`/`all_switches`/`all_cpes` (matching the existing pattern for the unassigned-site bucket) and only uses the site shortcut for `all` scope
- Crashed scheduled jobs no longer leave the rollout stuck "active" — `_finalize_crashed_job` was passing `learned_version=` instead of `learned_versions=`, so the scheduler call raised `TypeError` and the rollout never progressed
- Per-device window cutoff was off-by-one: at exactly `end_hour` the deferral fell into the overnight branch and computed ~24 hours remaining, so devices kept updating past the window
- Freeze windows configured with a date-only `end_date` (e.g., "2026-05-05") now cover the entire end-day inclusively; previously the lexical string compare excluded the entire end-day
- Scheduler now recovers orphaned active rollouts after a restart: pending devices whose update job died with the previous process are flipped to `deferred` so they retry next window instead of being silently skipped
- `_ran_today` startup recovery uses the configured timezone for the date key, preventing a same-day double-run when the container's system TZ differs from the configured TZ
- `trigger_canary_now` no longer raises `ValueError` when settings like `parallel_updates` or `min_temperature_c` are malformed; uses the safe `_as_int` / `_as_float` helpers
- Time-source drift validation samples the system clock after the external HTTP response so request latency cannot register as drift
- Stale job-completion events post-restart are now logged and reconciled if the active rollout still tracks the job_id, instead of silent drop
- Pre-rollback safety snapshot is now mandatory: if `/api/config-push/rollback/{ip}` can't capture the current config (device unreachable for fetch, empty response), the rollback is refused with HTTP 409 instead of proceeding silently. Operators can override with `force=true` in the request body, which logs a warning and writes a `config.rollback.force` audit-log entry. The response now includes `safety_snapshot_saved`, and the UI re-prompts the operator before forcing
- Deleting a device now also purges its rows in `config_enforce_log`, `device_update_history`, and `device_uptime_events`. Previously these audit/history tables accumulated orphaned rows keyed by stale IPs, and reusing an IP for a different device blended the histories. New `scripts/cleanup_orphaned_device_data.py` (with `--dry-run`) cleans up rows that orphaned before this fix shipped. `device_configs` is intentionally still soft-deleted via the recycle bin
- Daily config-poll window catch-up: the last successful poll time is now persisted to `settings.last_config_poll_at`, and on the next poll tick the manager checks whether that timestamp is older than 25h. If so it runs a catch-up poll instead of silently skipping the day. Previously the in-memory `_last_config_poll` was lost on restart, so a manager that was down during the configured poll hour would miss an entire day of compliance data with no signal to the operator
- Per-device config-poll outcome is now persisted to `devices.last_config_poll_at/_status/_error` (status one of `ok`, `timeout`, `http_status`, `json_decode`, `auth`, `unknown`). Previously a failed `get_config()` returned `None`, the device disappeared from `device_configs`, and the operator had no signal that polling was broken on a specific device. Tachyon driver now exposes `fetch_config()` that returns `(config, status, error)` for granular classification
- Signal vs Distance chart rendered fully blank (no axes, grid, or threshold lines) when every CPE in scope had a null `link_distance` — Chart.js v4's auto-scale collapsed the x-axis to a degenerate `[0, 0]` range, throwing in the user-supplied tick callback before the render could complete. The x-axis now anchors at `min: 0` with `suggestedMax: 100` and the tick callback guards non-numeric values; `initChart` is wrapped in try/catch so future Chart.js construction failures surface in the console instead of silently blanking the canvas

## 1.3.0 - 2026-04-08

### Added
- Release validation script (`scripts/validate_release.py`) for automated API-level smoke testing against live deployments
- "Dangerous" feature classification: 6 features that make sweeping network/auth changes are labeled with amber badges in the UI (config backup/restore, config templates, config compliance, config push, RADIUS, SSO/OIDC)
- `/api/features` endpoint returning feature map with enabled/dangerous status
- About panel in Settings (replaces License panel) showing version, instance ID, and GitHub link
- Config auto-enforce: automatically detect config drift and push corrections in phases (canary → 10% → 50% → 100%)
- Site-scoped config templates: site templates override global per category
- Config enforce log: audit trail of all auto-enforcement actions
- Syslog and Watchdog config template categories (replacing Discovery)
- Config push confirmation dialog and "All Switches" scope option
- SLA/uptime tracking with automatic state transition detection in poller
- Per-device and fleet-wide availability percentage calculations
- Uptime API endpoints: /api/uptime/device, /api/uptime/fleet, /api/uptime/events
- Device notes field for APs and switches
- Bulk device operations: enable, disable, delete, move to site
- OpenAPI documentation with tagged endpoints, Swagger UI at /docs, ReDoc at /redoc
- Bandwidth throttling for firmware uploads (configurable KB/s limit per device)
- Update analytics dashboard with summary stats, daily trends, model breakdown, error analysis, and device reliability
- SNMP trap notifications for firmware update job completion (SNMPv2c)
- SNMP trap configuration UI in Settings > Notifications panel
- Test trap button for verifying SNMP configuration
- Inline release notes display in Settings > Updates panel
- GitHub release notes categorization via `.github/release.yml`
- SHA256 integrity verification for firmware files before device upload
- Overall update timeout safety net (30 min APs/CPEs, 45 min switches)
- Concurrency limit (10) for RADIUS rollout device pushes
- Self-update safety gate: block app updates while firmware jobs are running
- Device offline/recovered email notifications
- RADIUS open client mode (accept any device with correct secret, default)
- HTTPS/SSL tab in App Settings
- Setup wizard replaced with App Settings auto-open on first run
- Weather temperature display on startup (no longer waits for first scheduler tick)

### Changed
- **Open-source conversion**: all features are now free and unlocked with no license key required
- Removed all billing/licensing infrastructure (license server, Stripe, activation, validation, grace periods, device counting, free-tier limits, nag banners)
- `updater/license.py` replaced by `updater/features.py`; `license.py` is now a thin re-export shim
- `require_feature()` and `require_pro()` are no-ops (kept in endpoint signatures for minimal diff)
- Repo references updated from `isolson/firmware-updater` to `sixtyops/manager`
- Docker image updated from `ghcr.io/isolson/firmware-updater` to `ghcr.io/sixtyops/manager`
- Website pricing section replaced with open-source feature list
- Privacy policy updated to remove license validation references
- Config push rollout controls (advance, resume, cancel) now require admin or operator role
- Complete rebrand from Tachyon to SixtyOps across codebase, Docker, appliance, and CI
- App Settings modal uses fixed height to prevent layout jumping between tabs
- Email notification subjects changed from `[Tachyon]` to `[SixtyOps]`
- Simplified to single-branch (`main`) workflow — no more `dev` staging branch

### Removed
- License key activation, deactivation, and validation endpoints
- License validator background task and grace period logic
- Free-tier device limits and nag banner
- `SIXTYOPS_FORCE_PRO` environment variable
- `website/billing.html`
- Stripe and billing references throughout codebase
- In-app subscription checkout with Stripe and auto-activation via instance_id
- Contextual license status banners (cancelled, over limit, expired, grace period)

### Fixed
- Crashed update jobs now properly clear active job state
- Website deploy pipeline (AWS OIDC credentials + S3/CloudFront)
- Logo alignment (icon sits on text baseline)
- Local Users tab not loading on initial Auth tab open
- Border radius normalization (5px → 6px)
- Appliance now boots on both Proxmox (virtio) and ESXi (SCSI) hypervisors via UUID-based fstab/bootloader and SCSI initramfs drivers
- Appliance SSL cert generation failure now stops the service instead of silently continuing
- Appliance boot disk detection is now automatic instead of hardcoded to /dev/vda
- Proxmox installation instructions corrected to use virtio disk controller

## 1.1.1-dev1 - 2026-02-19

### Added
- Release workflow protections (dev/stable split, manual approval for stable)
- Development documentation (CLAUDE.md, contributing section)
- System update overlay with progress tracking
- Settings notification dot for available updates

### Changed
- Updates panel layout and label clarity improvements

## 1.1.0 - 2026-02-19

### Added
- SSO/OIDC authentication save fix
- Updates panel layout improvements

## 1.0.5 - 2026-02-18

### Fixed
- Data directory permissions for Docker volumes

## 1.0.0 - 2026-02-17

### Added
- Gradual rollout for scheduled updates: canary (1 AP) -> 10% -> 50% -> 100%
- Rollout status card in Auto-Update tab with phase indicator and progress bar
- Rollout pause on failure with manual resume/cancel controls
- Target firmware version auto-detection after canary phase
- API endpoints: `GET /api/rollout/current`, `POST /api/rollout/{id}/resume`, `POST /api/rollout/{id}/cancel`
- `rollouts` and `rollout_devices` database tables
- CPE authentication probing - detects CPEs with OK/failed auth status
- Login retries for failed device connections
- System time validation against NTP sources before running updates
- Rebranded UI titles to "Unofficial Tachyon Networks Bulk Updater"
- CSV backup/restore for device lists
- Real-time status broadcasts
- Pre-rollout predictions
- Single-page monitor with settings drawer
- Firmware fetcher and UI polish

### Changed
- Device-level phase ordering replaces AP-group concurrency model
- Updates run in phases: CPEs pass 1 -> APs pass 1 -> APs pass 2 -> CPEs pass 2
- Split single Firmware tab into separate Firmware (file management) and Update (manual) tabs
- IP addresses in UI are now clickable links

### Security
- RADIUS authentication with local username/password fallback
- Session-based auth with 24-hour TTL and HTTPOnly cookies
- Resource cleanup and security hardening
- API security hardening

### Infrastructure
- Dockerfile and docker-compose.yml with persistent volumes
- Docker Compose split into base + standalone overlay
- Background network poller discovering APs and CPEs every 60 seconds
- SQLite persistence for devices, sessions, settings, and job history
