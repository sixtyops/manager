# CLAUDE.md

## Communication style (required)

**Always respond in simplified technical English. Make your points up front,
then give detail.** Lead with the conclusion in 1–3 short bullets. Then the
detail, in short plain sentences. In docs, every section starts with
**Key points**, then **Detail**. Never bury the answer under background.

## Project Overview

SixtyOps Manager — automated firmware update tool for Tachyon wireless
network devices (APs, CPEs, switches). Python/FastAPI backend, single-page
HTML/JS frontend, SQLite database, Docker deployment.

## Strategy & Roadmap

- **North Star:** see `docs/north-star.md` — vision, target customer, phases,
  success metrics.
- **Current phase:** Phase 1 (Lean Launch). Filter open issues by label
  `phase-1` for in-scope work; `launch-p0` for must-close-before-design-partners.
- **Monetization:** per-AP + per-switch billing; SMs (CPEs) are free. Phase 2
  workstream (label `phase-2`, `gtm`).
- **Repo state:** `sixtyops/manager` is PUBLIC (the GHCR image is public too).
  `main` and `v*` release tags are protected by repository rulesets (force-push
  and deletion blocked; PRs required on `main`; only admins may create/move
  release tags — the self-update trust root). Admins can bypass for emergencies.
  See `docs/repo-hardening.md`. (Public-repo rulesets are free; this supersedes
  the old "Team plan needed" note in #116.)

## Branching Model

### Repository lead authority (2026-10-02)

The lead agent owns issue triage, queue labels, implementation, review,
validated PR merges, and development releases for `sixtyops/manager`.
Ongoing ownership includes feature rearchitecture, bug fixes, edge cases,
security, and resilience. Proactively find, track, prioritize, and resolve gaps
in these areas. Add regression checks where they prove the fix. Keep changes
small and aligned with the product goal; escalate major design and risk decisions.
Isaac explicitly delegated these duties on 2026-10-02. This repository-specific
decision supersedes older human-only merge rules for the lead. Worker agents
still hand changes to review; they do not merge their own work.

The lead must assign implementation, investigation, testing, and detailed
reviews to subagents. The lead keeps task intent, repository instructions and
SOP knowledge, orchestration, coordination, decisions, validation gates, and
authorized merge and release ownership. Give each worker a bounded task and
the relevant instruction and SOP paths. Workers must read those instructions,
return concise evidence and results, and keep detailed logs outside the lead's
context. Reuse active agents and do not duplicate jobs. Preserve loop locks,
budgets, the three-work-item limit, and review gates. If no subagent capacity is
available, leave work queued; do not silently implement it in the lead context.
Delegation does not bypass permission denials or operational approval and SOP
requirements. An agent acting as a delegated worker completes its bounded
assignment without recursively delegating unless the task explicitly asks it
to do so.

- Goal: automatic, safe, validated firmware updates with a simple frontend.
  Follow the target contract in [PR 412](https://github.com/sixtyops/manager/pull/412)
  and the delivery order in [issue 413](https://github.com/sixtyops/manager/issues/413).
  Distinguish target behavior from shipped behavior.
- Keep one issue per PR. Mark small, dependency-ready work `agent:ready`.
  Finish active fixes first and keep at most three active build or fix PRs.
  Use GPT-6.1 Sol for safety work and review. Luna may do bounded low-risk work.
  Fast mode is authorized where supported until 2026-10-03 00:00
  America/Chicago. After that cutoff, use normal mode unless Isaac extends it.
- Before each merge, verify the current head, independent review, sign-off
  packet, zero unresolved threads, passing applicable checks, and duplicate
  changes. Never bypass a failed gate or resolve another reviewer's thread.
  Engine and driver changes require bench evidence on that exact commit.
  A skipped hardware test does not pass this requirement.
- The authorized development system is
  `https://sixtyops-dev.infra.treehouse.mn/`. Follow the release SOP, verify
  rollback, record release notes and known risks before deployment, then
  verify the deployed version and health. Public dev tags reach other
  dev-channel installs too; confirm release scope before publishing.
  Stable releases and production-device changes are not covered by this grant.
- The lead wakes hourly through its Paseo heartbeat and advances eligible
  work without waiting for another operator prompt. Reuse the existing
  pickup, review, and fix loops with their locks and budgets. Ask Isaac for
  major product or safety decisions, missing physical access, and named lab
  hardware. Continue independent work when hardware proof is blocked.

- **`main`** — The only long-lived branch. Always deployable.
- **Feature branches** — Branch from `main`, PR back to `main`.

### Branching Rules (MUST follow)

1. **Never push directly to `main`** — all changes go through PRs.
2. **Feature branches must branch from `main`**.
3. **Before creating a feature branch**, run `git fetch origin && git checkout main && git pull origin main` to start from the latest `main`.

### PR Checklist

Before creating any PR, verify:
- [ ] Target branch is `main`
- [ ] Source branch was created from `main`
- [ ] All tests pass (`pytest -v`)
- [ ] CHANGELOG.md updated if user-facing change

## Release Workflow

Full step-by-step SOP (signing commands, verification, failure recovery):
**[docs/release-sop.md](docs/release-sop.md)**. Release tags must be
**GPG-signed by the release key** — CI and every fielded instance refuse
unsigned tags (see `docs/self-update-signing.md`).

Short form:

### Dev Release (pre-release for early testing)
1. On a branch, bump the version with a `-devN` suffix in
   `updater/__init__.py` and `pyproject.toml`, and update the pinned image tag
   in the website install snippet (`website/index.html`). PR → **merge first**.
2. Sign the tag at `origin/main`, verify, push by name (exact commands in the
   SOP — do not use plain `git tag` or `git push --tags`).
3. GitHub Actions verifies the signature, creates the pre-release, and pushes
   `ghcr.io/sixtyops/manager:vX.Y.Z-devN` (+ `:dev`). Dev-channel installs
   receive it automatically.

### Stable Release (manual approval required)
1. Bump to the stable version; move CHANGELOG Unreleased items under a version
   header. PR → merge.
2. Sign + verify + push the tag per the SOP.
3. **GitHub Actions > Release > Run workflow**, enter the tag, type `RELEASE`.

### Version Conventions
- **App version (Dev)**: `X.Y.Z-devN` in code, `vX.Y.Z-devN` in tags
- **App version (Stable)**: `X.Y.Z` in code, `vX.Y.Z` in tags
- `updater/__init__.py` is the source of truth for app version
- Default release-check repo is `sixtyops/manager` (`GITHUB_REPO` can override)
- Self-update checks out git tags from the mounted source repo (`/app/repo`),
  so release tags must exist in the code repo.

### Release Notes
- **Dev releases**: auto-generated from commit messages between tags.
  Each release also links to the GHCR Docker image.
- **Stable releases**: auto-generated by GitHub from PR titles, categorized
  by label (`feature`, `bug`, `chore`, `docs`, `ci`) via `.github/release.yml`.
- **PR labels are auto-applied** by `.github/workflows/auto-label.yml` from
  conventional commit prefixes in the PR title (`feat:` → `feature`,
  `fix:` → `bug`, `chore:` → `chore`, etc.). No manual labeling needed.
- **Write clear PR titles** — they become the stable release note line items.
  Use conventional format: `feat: add X`, `fix: resolve Y`, `chore: update Z`.
- **CHANGELOG.md** — Update the `## Unreleased` section when making notable
  changes. When cutting a release, move the Unreleased items under a new
  version header (e.g., `## 1.2.0 - 2025-03-15`).
- Release notes are displayed inline in the app's Settings > Updates panel.
  The backend fetches up to 2000 characters of the GitHub release body.

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
| `docs/rearchitecture.md` | Rearchitecture decisions, feature disposition, target layout (epic #316) |
| `docs/rollout-logic.md` | Target rollout model: identity, lifecycle, waves, edge cases |
| `tests/integration/` | Integration tests against real hardware (requires `SIXTYOPS_TEST_URL`) |

## Deployment Reality

- Production install path is `/opt/sixtyops` via `scripts/install.sh` (defaults
  to `main`) and creates `sixtyops.service` systemd unit that runs compose up/down.
- Standard deployment command uses both compose files:
  `docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build`.
- Base compose publishes `8000/tcp` and `1812/udp`; standalone overlay adds
  nginx ports `80/443` and certbot renewal loop (12h). The base publishes
  are env-overridable: set `BIND_IP=<host-ip>`, `HOST_PORT=<port>`, and/or
  `RADIUS_HOST_PORT=<port>` to bind to a specific host IP / different host
  port without editing `docker-compose.yml` (which would leave the tree
  dirty and break the in-app self-update path). Set them in a `.env` file
  next to `docker-compose.yml` or export them in the shell that runs
  `docker compose up` — these are *Compose substitution* vars, not the
  service's own `environment:` block. If the deployment also has a
  `docker-compose.override.yml` re-publishing the same port, drop that
  override after switching to env vars or Compose's list-merge will append
  both entries.
- Persistent directories are bind mounts: `./data`, `./firmware`, `./backups`,
  plus nginx/certbot directories in standalone mode.
- Host directories must be writable by UID/GID `1500` (container `appuser`);
  installer/deploy scripts enforce this.
- Self-update requires `/var/run/docker.sock`, mounted repo at `/app/repo`,
  and compose files mounted into container.

## Development

```bash
# Local dev (no Docker) — UI work, fast iteration
./dev.sh                # uvicorn --reload on localhost:8000
./dev.sh --fresh        # wipe DB and re-seed

# Run tests
pytest -v

# Integration tests against real hardware (see below)
SIXTYOPS_TEST_URL=https://<your-dev-host> pytest -m integration -v
```

**Local dev** (`dev.sh`): Pure Python, hot-reload, localhost only. Best for
UI/API work that doesn't need hardware.

**Docker stack**: there is no `dev-docker.sh`. Run the compose files from
*Deployment Reality* with `SEED_DATA=1` (development only, never on a
production host) for a production-like stack on the LAN.
Local seeded login: `admin / admin`. This local default does not apply to the
shared dev host or live integration tests.

Seed script (`scripts/seed_dev_data.py`) inserts sample sites, devices, CPEs,
config templates, and job history. It's idempotent — skips if data exists.
Runs automatically in the entrypoint when `SEED_DATA=1` is set.

If Docker socket errors appear, start Colima: `colima start`.
If port conflicts persist after `docker compose down`: `colima restart`.

### Integration Tests (Real Hardware)

Integration tests in `tests/integration/` run against a live SixtyOps instance
with real Tachyon devices. They are **skipped by default** — `pytest -v` won't
run them.

**Dev server:** set the host you operate via `SIXTYOPS_TEST_URL` — there is
no shared dev URL committed here.

```bash
# Run the merge-gating live-dev lane
SIXTYOPS_TEST_URL=https://<your-dev-host> \
SIXTYOPS_TEST_USER=<local-admin-user> \
SIXTYOPS_TEST_PASS=<local-admin-pass> \
SIXTYOPS_TEST_AP_IP=<ap-with-cpes> \
SIXTYOPS_TEST_SWITCH_IP=<dedicated-switch> \
SIXTYOPS_TEST_FIRMWARE_AP_IP=<firmware-test-ap> \
SIXTYOPS_TEST_CONFIG_AP_IP=<config-test-ap> \
SIXTYOPS_TEST_RADIUS_AP_IP=<radius-test-ap> \
  pytest -m "integration and dev_blocking" -v --timeout=900

# Run the separate non-blocking SSO lane
SIXTYOPS_TEST_URL=https://<your-dev-host> \
SIXTYOPS_TEST_USER=<local-admin-user> \
SIXTYOPS_TEST_PASS=<local-admin-pass> \
SIXTYOPS_TEST_OIDC_PROVIDER_URL=<provider-url> \
SIXTYOPS_TEST_OIDC_CLIENT_ID=<client-id> \
SIXTYOPS_TEST_OIDC_CLIENT_SECRET=<client-secret> \
SIXTYOPS_TEST_OIDC_REDIRECT_URI=<redirect-uri> \
  pytest -m "integration and dev_sso" -v
```

**What the tests cover:**

| Test file | What it validates |
|-----------|-------------------|
| `test_smoke.py` | Health check, devices seen recently, no persistent errors |
| `test_device_polling.py` | Dedicated AP/switch poll coverage and CPE presence |
| `test_config_backup.py` | Config poll, snapshot history, diff, tar download |
| `test_config_push.py` | Preview and no-op config push on the dedicated config AP |
| `test_config_backup_restore.py` | Round-trip: poll config → restore same → verify hash |
| `test_manager_backup.py` | Export CSV backup, verify it contains live device IPs |
| `test_firmware.py` | Upgrade dedicated firmware AP and roll back (**slow**) |
| `test_cpe_lifecycle.py` | Dedicated AP CPE signal/auth coverage |
| `test_system_surface.py` | Dashboard, portal, device history, reports, analytics |
| `test_live_crud.py` | Tower-site, device-group, and bulk-device CRUD with cleanup |
| `test_radius_live.py` | RADIUS CRUD, defaults, targeted rollout, and restore |
| `test_sso_live.py` | Separate non-blocking OIDC config and login affordance checks |

**Safety:** The blocking lane mutates only dedicated lab devices named by env
vars. Firmware tests restore the original version. Config restore tests push
back the same config. The RADIUS rollout lane is explicitly targetable so it
does not walk the whole enabled fleet.

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `SIXTYOPS_TEST_URL` | *(none — tests skip)* | Base URL of the shared dev instance |
| `SIXTYOPS_TEST_USER` | *(required for live auth)* | Dedicated local admin username |
| `SIXTYOPS_TEST_PASS` | *(required for live auth)* | Dedicated local admin password |
| `SIXTYOPS_TEST_AP_IP` | *(required for `dev_blocking`)* | Dedicated AP with attached CPEs |
| `SIXTYOPS_TEST_SWITCH_IP` | *(required for `dev_blocking`)* | Dedicated switch |
| `SIXTYOPS_TEST_FIRMWARE_AP_IP` | *(required for `dev_blocking`)* | Dedicated firmware test AP |
| `SIXTYOPS_TEST_CONFIG_AP_IP` | *(required for `dev_blocking`)* | Dedicated config test AP |
| `SIXTYOPS_TEST_RADIUS_AP_IP` | *(required for `dev_blocking`)* | Dedicated RADIUS rollout AP |

See [docs/dev-hardware-validation.md](docs/dev-hardware-validation.md) for the
full workflow and GitHub Actions contract.

## Writing and style

Use plain words, put the bottom line first, and cut filler and AI tells in all
GitHub text and all new code comments. Apply the Simplified Technical English
rules above: short sentences, one idea per sentence, active voice, and one term
per concept. Keep UI labels, errors, and recovery instructions consistent.

All agents: read the applicable `ai-copywriter` instructions before writing
when the file is available. The installed copy is
`~/.claude/skills/ai-copywriter/SKILL.md`; the repo does not ship it. Readable
skill instructions can be used by any agent. Verify required tools separately.
Use its technical and UI writing guidance within this repo's STE rules.
Technical copy stays factual and neutral. Marketing defaults do not override
operator clarity or require extra questions when the task context is clear.
If the skill is unavailable, apply the rules in this section directly and
report the fallback. Do not claim that an unread or missing skill was loaded.

The rule covers:

- Issue and PR titles and bodies
- PR comments, issue comments, and review-thread replies
- Commit messages (they show on GitHub)
- Sign-off packets and bot status comments
- Code comments and docstrings in new or changed code

It does not ask you to rewrite old comments.

## UI work and skills

**Key points:** UI workers read the skills that apply to the task. The existing
design system and the [one-right-way goal](docs/one-right-way.md) take precedence
over generic design or marketing defaults.

**Detail:** Before changing UI, read [UI_STYLE_GUIDE.md](UI_STYLE_GUIDE.md),
[UI principles](docs/ui-principles.md), and the relevant existing flow. Reuse
its tokens, components, and operator terms. Keep "Keep my fleet current" as
the primary task. Distinguish shipped behavior from the target contract.

Read `.claude/skills/frontend-design/SKILL.md` for UI work, including
`static/` and `updater/templates/`. Apply the copywriter and STE guidance in
[Writing and style](#writing-and-style) to UI text and documentation. Search
skill names first in repo `.claude/skills`, then installed `~/.claude/skills`
and `~/.codex/skills`. Read relevant UI/frontend, copywriter, and STE files
before using them. Honor skills the user explicitly names. Do not load every
skill or install a skill, plugin, or dependency just to satisfy this rule.

Use available accessibility or usability skills when they help the task.
Check keyboard access, visible focus, clear labels, color-independent status,
responsive layout, and reduced motion where applicable. Read only the related
skill instructions and resources. Verify each skill's files and tool needs;
its install location alone does not make it Claude-only or portable.

If a skill is missing or its tools are unavailable, state the limit and use
the repo guidance for the parts that can proceed. No dedicated STE skill is
bundled here; the communication and writing rules remain mandatory. Report a
missing explicitly requested skill. Stop only the part that requires its
unavailable capability. Preserve delegation, permission boundaries, tests,
review gates, and any required bench proof.

## Rules

- Run tests before committing (`pytest -v` — all must pass)
- Never commit secrets or credentials
- Dev releases can be frequent; stable releases should be deliberate
- `scripts/install.sh` always pulls from `main` — main must be stable
- All PRs target `main`
- One feature per branch/PR — don't bundle unrelated changes
- Keep commits focused and atomic with conventional commit messages
- For UI work, follow [UI work and skills](#ui-work-and-skills).
- **Fleet rollouts must advance one wave per maintenance window** (10% → 50% →
  100%, no canary phase) and **halt the whole job if a device doesn't come back
  online**. New firmware is held before the first wave by the **Firmware Hold**
  (release date + `firmware_canary_hold_days`), which clears early, per model
  family, once a device is confirmed working on it. This is enforced by the
  fail-closed gate in `updater/rollout_gate.py` and guarded by
  `tests/test_rollout_invariants.py`. Don't reintroduce a canary phase or
  per-wave auto-advance without routing through the gate, and don't let the
  hold-clear path bypass the one-wave-per-window rule. Manual per-device updates
  intentionally bypass the hold. See [docs/gradual-rollout.md](docs/gradual-rollout.md).
  Keep all rollout logic in one engine; never copy the wave loop (rule 21 in
  [docs/rearchitecture.md](docs/rearchitecture.md)).
