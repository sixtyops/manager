# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Added
- CPE authentication probing - detects CPEs with OK/failed auth status
- Login retries for failed device connections
- System time validation against NTP sources before running updates
- Rebranded UI titles to "Unofficial Tachyon Networks Bulk Updater"

## 2025-01-XX - Gradual Rollout System

### Added
- Gradual rollout for scheduled updates: canary (1 AP) -> 10% -> 50% -> 100%
- Rollout status card in Auto-Update tab with phase indicator and progress bar
- Rollout pause on failure with manual resume/cancel controls
- Target firmware version auto-detection after canary phase
- API endpoints: `GET /api/rollout/current`, `POST /api/rollout/{id}/resume`, `POST /api/rollout/{id}/cancel`
- `rollouts` and `rollout_devices` database tables

## 2025-01-XX - Device-Level Phases

### Changed
- Replaced AP-group concurrency model with flat device-level phase ordering
- Updates now run in phases: CPEs pass 1 -> APs pass 1 -> APs pass 2 -> CPEs pass 2 (bank mode)
- Bank-mode ordering ensures dual-bank firmware is applied correctly

## 2025-01-XX - Firmware Manager Tab

### Changed
- Split the single Firmware tab into separate **Firmware** (file management) and **Update** (manual updates) tabs
- Firmware page is now a standalone view for uploading, listing, and deleting firmware files

## 2025-01-XX - UI Improvements

### Changed
- IP addresses in the UI are now clickable links that open device web interfaces in a new tab

## 2025-01-XX - Authentication and Monitoring

### Added
- RADIUS authentication with local username/password fallback
- Session-based auth with 24-hour TTL and HTTPOnly cookies
- Login page
- Background network poller discovering APs and CPEs every 60 seconds
- Network topology monitor page with signal health indicators
- SQLite persistence for devices, sessions, settings, and job history
- Simplified firmware management page
- Improved poller error reporting

## 2025-01-XX - Docker Support

### Added
- Dockerfile and docker-compose.yml
- Persistent volumes for firmware files and database

## 2025-01-XX - Initial Release

### Added
- Web-based firmware update tool for Tachyon Networks devices
- Parallel device updates with configurable concurrency
- Real-time WebSocket progress monitoring
- Firmware upload and IP list parsing
- Support for TNA-301, TNA-302, TNA-303x, TNA-303L, TNS-100
