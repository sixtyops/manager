# Repository guide

**Key points:** SixtyOps Manager automates firmware updates for Tachyon
wireless network devices.

**Detail:** The app uses Python/FastAPI, a single-page HTML/JS frontend,
SQLite, and Docker. Paths in the table are relative to the repository root.

## Key Files

| File | Purpose |
|------|---------|
| `updater/__init__.py` | Version string |
| `updater/app.py` | All API routes, WebSocket, update logic |
| `updater/templates/monitor.html` | Entire frontend (single-page app) |
| `updater/database.py` | SQLite schema and data access |
| `updater/scheduler.py` | Auto-update scheduler + gradual rollout (10%→50%→100%, one wave per window) |
| `updater/rollout_gate.py` | Fail-closed phase gate: one wave per maintenance window + Firmware Hold |
| `updater/release_checker.py` | Self-update: checks GitHub releases API |
| `updater/tachyon.py` | Tachyon device communication client (hardware vendor) |
| `scripts/install.sh` | Production installer (always pulls `main`) |
| `docker-compose.yml` | Base runtime (sixtyops-mgmt + nginx, no 80/443 publish) |
| `docker-compose.standalone.yml` | Standalone overlay (publishes 80/443 + certbot) |
| `entrypoint.sh` | Runtime prep for self-update (docker socket group + repo ownership) |
| `.github/release.yml` | GitHub auto-generated release notes config |
| `docs/north-star.md` | Product direction (v2: Tachyon firmware automation) |
| `docs/rearchitecture.md` | Rearchitecture decisions, feature disposition, target layout |
| `docs/rollout-logic.md` | Target rollout model: identity, lifecycle, waves, edge cases |
| `tests/integration/` | Integration tests against real hardware (requires `SIXTYOPS_TEST_URL`) |

## Strategy & Roadmap

- **North Star:** see [north-star.md](north-star.md): vision, target customer, phases,
  success metrics.
- **Current phase:** Phase 1 (Lean Launch). Filter open issues by label
  `phase-1` for in-scope work; `launch-p0` for must-close-before-design-partners.
- **Monetization:** per-AP + per-switch billing; SMs (CPEs) are free. Phase 2
  workstream (label `phase-2`, `gtm`).
- **Repo state:** `sixtyops/manager` is PUBLIC (the GHCR image is public too).
  `main` and `v*` release tags are protected by repository rulesets (force-push
  and deletion blocked; PRs required on `main`; only admins may create/move
  release tags: the self-update trust root). Admins can bypass for emergencies.
  See [repo-hardening.md](repo-hardening.md).

## Local development

Run `./dev.sh` for Python hot reload on localhost:8000. Use it for UI/API
work that does not need hardware. `./dev.sh --fresh` deletes the local
database and seeds it again. Confirm before deleting existing data.
