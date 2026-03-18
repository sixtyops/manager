# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Added
- Webhooks feature display name added to license feature list
- Free-tier auto-update limit: APs capped at 4 per auto-update run; switch auto-updates now require Pro

### Changed
- Free-tier device nag threshold lowered from 10 to 4 to match the free AP limit
- Config backup endpoints (/api/configs/poll) now require Pro (Config Backup feature)
- Config compliance endpoints (/api/config-enforce/status, /api/config-enforce/log) now require Pro
- Config templates endpoint (/api/config-prefill) now requires Pro
- Enabling config auto-enforce now requires a Pro license
- Config push rollout controls (advance, resume, cancel) now require admin or operator role

### Added
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

### Fixed
- Crashed update jobs now properly clear active job state

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
