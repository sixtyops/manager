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

### Changed
- Bridge/FDB table polled from Tachyon switches on each poll cycle to maintain AP-to-port mapping
- Chassis connector replaced with inline `eth[n]` port badge on nested AP rows (cleaner, no orphaned line art)
- System > Updates panel normalized into a label/control grid; RADIUS Server stat cards removed; RADIUS Clients & Logs rewritten for clarity; About panel redesigned with inline version chip

### Removed
- Appliance build infrastructure (OVA/QCOW2 image generation, Packer configs, build-appliance workflow)

### Fixed
- "Update Available" banner in Settings > Updates stayed hidden even when an update was detected (inline `display:none` overrode the `.hidden` class toggle)
- Switch → AP topology cascade wasn't populating because `TachyonDriver` didn't expose `get_bridge_table()`; added passthrough so bridge entries are stored and APs render under their upstream switch
- Tachyon config GET/POST hit the wrong endpoint (`/cgi.lua/apiv1/config`) and returned HTTP 401 "Authorization Failed"; corrected to `/cgi.lua/config` so config backup, compliance, and push all work

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
