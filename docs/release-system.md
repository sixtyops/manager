# Release System

**Bottom line:** code reaches customers via three staged hops — PR → main → dev tag → stable tag — with automated validation at every hop and one human approval at the stable cut.

```
       feature branch
            │  PR opened
            ▼
  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────────┐
  │ ci.yml           │  │ install-smoke.yml│  │ dev-hardware.yml      │
  │ unit tests +     │  │ fresh install +  │  │ dev_blocking lane     │
  │ docker build     │  │ upgrade idempot. │  │ vs real Tachyon hw    │
  └──────────────────┘  └──────────────────┘  └───────────────────────┘
                            │ all green
                            ▼
                          main
                            │ maintainer tags vX.Y.Z-devN
                            ▼
                  release.yml (auto)
                  → GitHub pre-release
                  → ghcr.io/sixtyops/manager:vX.Y.Z-devN
                            │ dev-channel installs auto-update
                            ▼
                  dev soak (on the operating team's dev host)
                            │ maintainer tags vX.Y.Z + workflow_dispatch (confirm=RELEASE)
                            ▼
                  release.yml (manual)
                  → GitHub release
                  → ghcr.io/sixtyops/manager:vX.Y.Z + :latest
                            │
                            ▼
                  customer installs (stable channel)
```

Customers on the stable channel never run `main` HEAD. The dev host runs the dev channel and auto-updates from `vX.Y.Z-devN` tags as they land.

This document describes how releases are produced, published, and consumed by
the app updater.

## Repositories and Artifacts

- **Code repo:** `sixtyops/manager`
- **Container registry:** `ghcr.io/sixtyops/manager`

## Branch and Tag Model

- `main` is the only long-lived branch. All PRs target `main`.
- Feature branches are created from `main` and merged back via PR.
- Version source is `updater/__init__.py`.
- App tags use `vX.Y.Z` (stable) and `vX.Y.Z-devN` (pre-release) format.

## GitHub Workflows

### 1) CI (`.github/workflows/ci.yml`)

- Runs on pushes/PRs to `main`.
- Executes:
  - `pytest -v`
  - `docker build`
- On PRs, warns if `appliance/` changed so an appliance rebuild is not forgotten.

### 2) Installer Smoke (`.github/workflows/install-smoke.yml`)

- Runs on PRs to `main` that touch installer-related paths.
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
   - always pushes `ghcr.io/sixtyops/manager:<tag>`
   - stable flow also pushes `:latest`

### 4) Build Appliance (`.github/workflows/build-appliance.yml`)

- Triggers:
  - manual `workflow_dispatch` with `app_version`
  - release `published` events (non-prerelease only)
- Builds appliance via Packer (OVA + QCOW2).
- Attaches artifacts to the release and updates `appliance-latest`.

## How App Self-Update Consumes Releases

Implementation: `updater/release_checker.py`

- Default release source repo: `GITHUB_REPO=sixtyops/manager`
- Release channels:
  - `stable` → calls `/releases/latest` (skips pre-releases)
  - `dev` → calls `/releases?per_page=10` and uses the newest
- Tag parsing:
  - strips leading `v`
  - compares parsed versions against current app version

Apply behavior (Docker / non-appliance mode):
- Fetches and checks out `v<target>` tag in mounted repo (`/app/repo`)
- Rebuilds and restarts via compose watchdog with rollback on failure

## Important Constraints

1. The updater expects semver-like app tags (`vX.Y.Z` / `vX.Y.Z-devN`).
2. The target tag must exist in the code repo because update apply uses
   `git checkout` by tag.
3. Release notes are displayed in the app's Settings > Updates panel
   (truncated to 2000 characters).

## Release Notes

- GitHub auto-generated release notes are categorized by `.github/release.yml`
  labels (`feature`, `bug`, `chore`, `docs`, `ci`, etc.).
- PR labels are auto-applied by `.github/workflows/auto-label.yml` from
  conventional commit prefixes in the PR title.

## Testing a Change on the Dev Host Without a Release Tag

Cutting a `-devN` tag for every UI tweak or experimental change is heavy. To deploy a feature branch directly to the dev host for hands-on testing without a tag:

1. SSH to the dev host (see your operations runbook for the SSH alias and the deploy directory — the upstream installer defaults to `/opt/sixtyops`).
2. Fetch and check out the feature branch:
   ```bash
   git fetch origin <branch-name>
   git checkout <branch-name>
   ```
3. Rebuild and restart the management container:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build sixtyops-mgmt
   ```
4. Verify the new code is live:
   ```bash
   curl -sk https://<your-dev-host>/healthz
   docker compose logs --tail=50 sixtyops-mgmt
   ```

To return the host to the dev release channel, check out the latest dev tag and rebuild:

```bash
git fetch origin --tags --force
git checkout vX.Y.Z-devN
docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build sixtyops-mgmt
```

**Caveat — shared resource:** the dev host is a singleton. Other PRs' `dev_blocking` CI runs hit the same host while a feature branch is deployed there, so their results reflect the deployed branch, not their own. Coordinate with anyone whose PR is mid-CI before deploying, and revert to the latest dev tag when finished.

## Recommended Release Procedure

### Dev Release
1. Bump `updater/__init__.py` to `X.Y.Z-devN`.
2. Commit, tag `vX.Y.Z-devN`, push with tags.
3. CI auto-creates a pre-release and pushes the Docker image.

### Stable Release
1. Bump `updater/__init__.py` to `X.Y.Z`.
2. Update CHANGELOG.md with a version header.
3. Commit, tag `vX.Y.Z`, push with tags.
4. Run Release workflow manually with `tag=vX.Y.Z` and `confirm=RELEASE`.
5. Verify:
   - GitHub Release exists and is not prerelease
   - GHCR has `vX.Y.Z` and `latest`
6. Smoke-check updater endpoints/UI in a running instance:
   - `POST /api/updates/check`
   - `GET /api/updates`
