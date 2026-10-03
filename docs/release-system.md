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
                            │ dev-channel installs receive the offer
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

Release-channel selection controls the update offer. A public dev tag can
reach other dev-channel installations; applying an update is a separate
action. The actual dev host's channel, runtime version, and automatic-update
configuration are not verified by this document. Fresh source installs use
`main` through the installer; a channel does not prove which code is deployed.

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
3. **Signature gate:** the tag must carry a valid GPG signature from the
   trusted release key or the run fails and nothing publishes (see
   [self-update-signing.md](self-update-signing.md) and the runner notes in
   [repo-hardening.md](repo-hardening.md)).
4. GitHub Release creation:
   - prerelease for dev tags
   - full release for manual stable flow
5. GHCR image push:
   - always pushes `ghcr.io/sixtyops/manager:<tag>` (dev pushes also move `:dev`)
   - stable flow also pushes `:latest`

### 4) Build Appliance (`.github/workflows/build-appliance.yml`)

- Triggers:
  - manual `workflow_dispatch` with `app_version`
  - release `published` events (non-prerelease only)
- Builds appliance via Packer (OVA + QCOW2).
- Attaches artifacts to the release and updates `appliance-latest`.

## How App Self-Update Consumes Releases

**Key points:** Automatic updates require a healthy retained immutable image
and a watchdog. A successful launch response is not final recovery proof.

**Detail:** Implementation: `updater/release_checker.py`

- Default release source repo: `GITHUB_REPO=sixtyops/manager`
- Release channels:
  - `stable` → calls `/releases/latest` (skips pre-releases)
  - `dev` → calls `/releases?per_page=10` and uses the newest
- Tag parsing:
  - strips leading `v`
  - compares parsed versions against current app version

The source path verifies the release signature, retains the prior
image, then checks out the target tag and launches the build/swap watchdog.
The appliance path retains the prior image before pull and guarded swap;
image integrity remains a separate follow-up. Both paths refuse an unguarded
fallback. Acknowledged terminal failures permit retry only after daemon,
healthy runtime, applicable restored-source, and cleanup proof. Unknown
launches remain fenced. The shipped single-worker process lock and best-effort
completion messages do not establish multi-process coordination or durable
notification delivery. See [recovery limits](self-update-signing.md#if-a-bad-release-ships).

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

## Release Procedure

Step-by-step (including tag signing, verification, and failure recovery) lives
in **[release-sop.md](release-sop.md)** — the single source of truth for
cutting a release. Tags must be GPG-signed; the short form there is: merge the
version-bump PR, sign the tag at `origin/main`, push the tag by name.

## Dev5 draft notes and readiness

**Key points:** `1.4.1-dev5` is a draft candidate, not ready to publish or deploy.
The refreshed code candidate is `617b7f9bd4d68766868e7fc924b14e14c0ad98ed`,
which includes [guarded recovery](https://github.com/sixtyops/manager/pull/451).
Reassess the final candidate commit before publication.

**Detail:** The earlier assessment at
`5594fa7d07aade8b27e3d581a56ff495956d3539` is historical evidence from
[PR 449](https://github.com/sixtyops/manager/pull/449), before the recovery fix.
It does not describe current recovery code. The published baseline is
[v1.4.1-dev4](https://github.com/sixtyops/manager/releases/tag/v1.4.1-dev4),
at `975741e8fac68ae377ba512aee1aed8059dad7b4`. Its
[Release run](https://github.com/sixtyops/manager/actions/runs/27728461351)
passed. The [existing version PR](https://github.com/sixtyops/manager/pull/265)
already set the app to `1.4.1-dev5` and package to `1.4.1.dev5`.
No duplicate version bump is needed. The website uses the installer, not a
pinned image. Preserve existing CHANGELOG entries; some Unreleased entries
already shipped in dev4.

Coverage references include the
[logging sanitizer](https://github.com/sixtyops/manager/pull/431),
[stored-secret hooks](https://github.com/sixtyops/manager/pull/436),
[Slack URL protection](https://github.com/sixtyops/manager/pull/444),
[SNMP migration](https://github.com/sixtyops/manager/pull/441), and
[guarded self-update recovery](https://github.com/sixtyops/manager/pull/451).
The recovery fix passed synthetic validation and
[exact-head CI](https://github.com/sixtyops/manager/actions/runs/37087732268).
These results do not establish dev-host delivery, host/data recovery, or
final-candidate bench proof.

### Publication gates

**Key points:** Public scope, host recovery, and final-commit bench evidence
remain unresolved. Passing CI does not prove a deployed version or rollback.

**Detail:** Record these gates before publication:

- Confirm the public dev-channel scope. A dev tag publishes a public
  prerelease and moves the public `:dev` image. Other installations can
  consume it. The checker offers updates; applying them is a separate action.
  A grant for one dev host does not by itself settle this wider scope.
- Verify the dev host's runtime version, source ref or image digest, install
  mode, repo/socket mounts, prior artifact, and restorable complete backup.
  These facts and a successful host rollback drill are not established here.
  A healthy `/healthz` response alone does not prove them. Use the
  [observed recovery paths](self-update-signing.md#if-a-bad-release-ships).
- Establish bench proof on the final candidate for the older unreleased
  [firmware-policy changes](https://github.com/sixtyops/manager/pull/259) and
  [CPE signal changes](https://github.com/sixtyops/manager/pull/260).
  Their unit and replay evidence does not establish this proof. Recent merges
  leave the firmware engine unchanged, but the full dev4-to-candidate range
  includes these engine and driver changes. Follow [one-right-way](one-right-way.md).
- Complete final-commit review, sign-off, and applicable CI. Verify both the
  release and image artifacts: the release job precedes image publication,
  so an image-push failure can leave a release without its image. Keep signing
  and protected-operation gates; a rejection is not permission to bypass them.

### Curated app-display draft

**Key points:** The marked body below is below the app's 2000-character limit.
It is a draft, not evidence of publication.

**Detail:** The current workflow generates dev notes from commit subjects.
It does not read this document. The release maintainer must deliberately
attach the validated curated body to the actual release and read it back.
Refresh the body and gates for the final candidate first.

<!-- dev5-notes:start -->
## 1.4.1-dev5 draft

**Key points:** Not released or ready to deploy. Public dev tags reach other installs.
Runtime, backup, host recovery and final-commit bench proof remain unverified.

**Detail:** Changes since v1.4.1-dev4:
- Redact registered secrets and canonical Slack webhook URLs from app/syslog
  output. Other secret sources and noncanonical URLs remain outside coverage.
- Use pysnmp 7.1.30 and fixed pyasn1 0.6.4 for notifications. Wait for UDP
  readiness; clean up failures and cancellation. No real receiver proof.
- Retain the healthy immutable image before self-update. Guard build/swap
  recovery. Retry acknowledged failures only after terminal daemon, healthy
  prior-image/source and cleanup proof. Unknown launches stay blocked and can
  need host reconciliation. Locking covers the shipped single-worker app;
  messages are best effort. Synthetic tests do not prove host/data recovery.
- Validate release inputs before shell use/checkout; preserve signing gates.
- Lock local usernames for 60 seconds after ten failures in 60 seconds.
  Return expired sessions to login.
  Improve Settings/setup help and share the database schema builder.
- Centralize firmware policy, block missing-family batches, and enroll devices
  after job creation. Prefer reported CPE rxPower. These older engine/driver
  changes still lack established final-candidate bench proof.

Limits: No real Slack, SNMP, Entra or hardware proof. Open OIDC/session-cache
changes are excluded. Security baseline unfinished. Reverting code can restore
logging exposure and vulnerable SNMP dependencies. Keep the prior artifact and
complete restorable data backup. No app rollback button. Public distribution
scope awaits a decision. One-right-way describes the target, not shipped proof.

Image only after successful publication: ghcr.io/sixtyops/manager:v1.4.1-dev5.
<!-- dev5-notes:end -->
