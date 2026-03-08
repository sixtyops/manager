# Release System

This document describes how releases are produced, published, and consumed by
the app updater.

## Repositories and Artifacts

- **Code repo:** `isolson/firmware-updater`
- **Public downloads/releases repo:** `isolson/tachyon-manager-releases`
- **Container registry:** `ghcr.io/isolson/firmware-updater`
- **Appliance artifacts:** `.ova` and `.qcow2`

## Branch and Tag Model

- `dev` is staging and receives normal feature work first.
- `main` is production.
- Version source for app releases is `updater/__init__.py`.
- App tags are expected in `vX.Y.Z` / `vX.Y.Z-devN` format.
- Appliance platform version source is `appliance/VERSION`.

## GitHub Workflows

### 1) CI (`.github/workflows/ci.yml`)

- Runs on pushes/PRs to `main` and `dev`.
- Executes:
  - `pytest -v`
  - `docker build`
- On PRs, warns if `appliance/` changed so an appliance rebuild is not forgotten.

### 2) Installer Smoke (`.github/workflows/install-smoke.yml`)

- Runs on pushes/PRs to `main` and `dev`.
- Executes `scripts/install.sh` in CI against a local bare git remote.
- Validates app reachability on `https://localhost/login`.
- Re-runs installer to validate upgrade/idempotency path.

### 3) Release (`.github/workflows/release.yml`)

- **Dev release trigger:** tag push matching `v*-dev*`.
- **Stable release trigger:** manual `workflow_dispatch` with:
  - `tag` (must already exist)
  - `confirm=RELEASE`

Pipeline:
1. Validation step (manual release only).
2. Test step (`pytest -v`).
3. GitHub Release creation:
   - prerelease for dev tags
   - full release for manual stable flow
4. GHCR image push:
   - always pushes `ghcr.io/isolson/firmware-updater:<tag>`
   - stable flow also pushes `:latest`

### 4) Build Appliance (`.github/workflows/build-appliance.yml`)

- Triggers:
  - manual `workflow_dispatch` with `app_version`
  - release `published` events, but only non-prerelease releases
- Builds appliance via Packer.
- Produces both `.ova` and `.qcow2`.
- Attaches artifacts to the release (when triggered by release event).
- Updates `appliance-latest` release in:
  - `isolson/firmware-updater`
  - `isolson/tachyon-manager-releases` (public mirror)

## How App Self-Update Consumes Releases

Implementation: `updater/release_checker.py`

- Default release source repo is:
  - `GITHUB_REPO=isolson/tachyon-manager-releases`
- Release channels:
  - `stable` -> calls `/releases/latest`
  - `dev` -> calls `/releases?per_page=10` and uses first item
- Tag parsing:
  - strips leading `v`
  - compares parsed versions against current app version

Apply behavior:

- **Appliance mode** (`TACHYON_APPLIANCE=1`):
  - pulls `ghcr.io/isolson/firmware-updater:v<target>`
  - restarts via watchdog with rollback behavior
- **Non-appliance mode**:
  - fetches/checks out `v<target>` tag in mounted repo (`/app/repo`)
  - rebuilds/restarts via compose watchdog

## Important Coupling and Constraints

1. The updater expects semver-like app tags (`vX.Y.Z` / `vX.Y.Z-devN`) for version comparison.
2. In non-appliance mode, the same target tag must exist in the code repo (`isolson/firmware-updater`) because update apply uses git checkout by tag.
3. If release data source (`GITHUB_REPO`) contains non-semver tags (for example only `appliance-latest`), app update detection will not produce normal upgrade behavior.
4. Appliance compatibility can be gated by adding this HTML comment to release notes:
   - `<!-- min_appliance_version: X.Y -->`

## Release Notes

- GitHub auto-generated release notes are categorized by `.github/release.yml`
  labels (`feature`, `bug`, `chore`, `docs`, `ci`, etc.).
- The app shows release notes in Settings > Updates (truncated payload in API/UI).

## Recommended Stable Release Procedure

1. Merge `dev` -> `main`.
2. Bump `updater/__init__.py` to stable version.
3. Create and push stable tag (`vX.Y.Z`) on `main`.
4. Run Release workflow manually with `tag=vX.Y.Z` and `confirm=RELEASE`.
5. Verify:
   - GitHub Release exists and is not prerelease
   - GHCR has `vX.Y.Z` and `latest`
   - (if needed) Build Appliance workflow run completed
   - `appliance-latest` assets updated in both repos
6. Smoke-check updater endpoints/UI in a running instance:
   - `POST /api/updates/check`
   - `GET /api/updates`
   - optional: `POST /api/updates/apply` in a safe environment
