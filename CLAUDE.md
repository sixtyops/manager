# CLAUDE.md

## Project Overview

Tachyon Firmware Updater — automated firmware update tool for Tachyon wireless
network devices (APs, CPEs, switches). Python/FastAPI backend, single-page
HTML/JS frontend, SQLite database, Docker deployment.

## Branching Model

- **`main`** — Production. Always reflects the latest stable release.
  Only receives merges from `dev` after staging testing.
- **`dev`** — Staging. All feature work merges here first.
- **Feature branches** — Branch from `dev`, PR back to `dev`.

**Never push directly to `main`. Never create stable releases without testing on dev first.**

## Release Workflow

### Dev Release (automatic)
1. Merge feature branch → `dev`
2. Update `updater/__init__.py` version (e.g., `"1.2.0-dev1"`)
3. Tag and push: `git tag v1.2.0-dev1 && git push origin v1.2.0-dev1`
4. GitHub Actions auto-creates a pre-release

### Stable Release (manual approval required)
1. Create PR from `dev` → `main`, merge
2. Update `updater/__init__.py` version on main (e.g., `"1.2.0"`)
3. Tag on main: `git tag v1.2.0 && git push origin v1.2.0`
4. Go to **GitHub Actions > Release > Run workflow**
5. Enter the tag name and type `RELEASE` to confirm

### Version Conventions
- Dev: `X.Y.Z-devN` in code, `vX.Y.Z-devN` in tags
- Stable: `X.Y.Z` in code, `vX.Y.Z` in tags
- `updater/__init__.py` is the single source of truth

## Key Files

| File | Purpose |
|------|---------|
| `updater/__init__.py` | Version string |
| `updater/app.py` | All API routes, WebSocket, update logic |
| `updater/templates/monitor.html` | Entire frontend (single-page app) |
| `updater/database.py` | SQLite schema and data access |
| `updater/release_checker.py` | Self-update: checks GitHub releases API |
| `updater/tachyon.py` | Device communication client |
| `scripts/install.sh` | Production installer (always pulls `main`) |

## Development

```bash
# Local dev
uvicorn updater.app:app --reload --port 8000

# Run tests
pytest -v

# Docker
docker compose up -d --build
```

## Rules

- Run tests before committing
- Never commit secrets or credentials
- Dev releases can be frequent; stable releases should be deliberate
- `scripts/install.sh` always pulls from `main` — main must be stable
