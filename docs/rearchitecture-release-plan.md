# Rearchitecture release plan and exit criteria

**Key points:**

- This plan says which releases carry the rearchitecture (epic
  [#316](https://github.com/sixtyops/manager/issues/316)) and what "done" means.
- Three steps cannot be undone: the bridge release, the first new-schema
  release, and the dev host cutover. Each has a gate below.
- Everything here is **target**, not shipped. Nothing in this plan sets a date.

**Detail:** This document covers release order only. It does not repeat the
design. The design is in [rearchitecture.md](rearchitecture.md) and
[rollout-logic.md](rollout-logic.md). The safety contract is in
[one-right-way.md](one-right-way.md). Delivery order for the safe firmware path
is in epic [#413](https://github.com/sixtyops/manager/issues/413). Signing and
tagging steps are in [release-sop.md](release-sop.md). Do not copy those steps
here.

## Shipped and target

**Key points:**

- Shipped: the old schema, the self-update apply path, and dev releases only.
- Target: a new schema, a pinned GHCR image, and a bridge that moves operators
  across.
- A line marked *Target* is a plan. It is not a promise to a customer.

**Detail:**

| Item | Shipped today | Target |
|---|---|---|
| App version in code | `1.4.1-dev5` (`updater/__init__.py`) | Chosen at each cut |
| Schema | Old schema with a migration chain | New schema from the shared schema builder; legacy tables and migration chain dropped ([#327](https://github.com/sixtyops/manager/issues/327)) |
| Upgrade path | In-app self-update checks out a git tag and rebuilds | Edit the pinned image tag, then `docker compose pull && up -d` ([#280](https://github.com/sixtyops/manager/issues/280)); self-update apply removed ([#281](https://github.com/sixtyops/manager/issues/281)) |
| Operator move to the new schema | Not available | Bridge export, reinstall, import ([#285](https://github.com/sixtyops/manager/issues/285), [#325](https://github.com/sixtyops/manager/issues/325), [#365](https://github.com/sixtyops/manager/issues/365), [#366](https://github.com/sixtyops/manager/issues/366)) |
| Releases on GitHub | Pre-releases (dev). The newest published at the time of writing, 2026-10-10, is `v1.4.1-dev4`. | Dev releases, then the bridge release, then the first new-schema stable |

Check the live release list before each cut. This table can go stale.

## The four release stages

**Key points:**

1. Dev releases on the old schema, through Phase A.
2. The bridge release ([#326](https://github.com/sixtyops/manager/issues/326)).
   It is the last old-schema stable.
3. A dev-only period through Identity and Phase B.
4. The first new-schema stable.

**Detail:** Each stage has an entry gate. Do not start a stage before its gate
passes. Version numbers are chosen at cut time, following the conventions in
[CLAUDE.md](../CLAUDE.md#version-conventions).

### Stage 1: dev releases on the old schema (Phase A)

**Key points:**

- Phase A work merges to `main` as small PRs. Dev tags carry it.
- The schema stays the old one. Schema-changing PRs add an additive, idempotent
  migration in the schema builder.
- The self-update apply path stays until the bridge ships.

**Detail:** Phase A is clean deletions, the distribution model, the bridge, and
the staged schema. The epic lists its issues; this plan does not repeat them.
Dev tags follow [release-sop.md](release-sop.md): bump PR first, then a signed
tag at `origin/main`. A public dev tag reaches every dev-channel install.
Confirm the scope before you publish.

Entry gate: none beyond the normal release SOP.

Exit gate: the bridge export ([#285](https://github.com/sixtyops/manager/issues/285))
is merged and tested on the old schema. Stage 2 can then start.

### Stage 2: the bridge release (one-way door 1)

**Key points:**

- The bridge release is the last stable release on the old schema.
- It still self-applies. It exports the full data set and refuses to
  auto-apply anything newer.
- It is a human gate ([#326](https://github.com/sixtyops/manager/issues/326)).
  Stable releases need explicit approval from Isaac.

**Detail:** The bridge export carries devices, users, settings, config
snapshots, firmware confirmations, job history, and the Fernet key. After
the bridge, the operator reinstalls and imports. The bridge must ship before
the self-update apply path is removed ([#281](https://github.com/sixtyops/manager/issues/281)).
If that order breaks, fielded installs have no way to reach the new schema.

Entry gate:

- Bridge export merged ([#285](https://github.com/sixtyops/manager/issues/285)).
- Security review of the bridge archive and install path
  ([#386](https://github.com/sixtyops/manager/issues/386)) done. It is a human
  gate.
- Release SOP followed, including signed tag and the manual stable approval.

Exit gate: the bridge release is published and its signature verifies. After
this point, a rollback of the bridge release is a new fix-forward release, not
a tag move.

### Stage 3: dev-only period (Identity and Phase B)

**Key points:**

- Identity and Phase B ship as dev releases only.
- No stable release is cut in this period.
- Stable-channel installs stay on the bridge release.

**Detail:** Identity makes a device a record keyed by `device_id`, not an IP
address. Phase B builds the rollout engine. Both change the update path.
Every engine or driver PR needs bench evidence on the exact head, as
[CLAUDE.md](../CLAUDE.md#repository-lead-authority-2026-10-02) requires.
A skipped hardware test does not count.

The safe firmware path in epic [#413](https://github.com/sixtyops/manager/issues/413)
sets the order of the engine work. That epic is the live source for order. This
plan does not list its issues.

Gates that must land before the engine changes they protect:

- The hardware lane is a real merge gate ([#330](https://github.com/sixtyops/manager/issues/330)).
- The scheduled-rollout baseline on hardware exists
  ([#381](https://github.com/sixtyops/manager/issues/381)). It runs before the
  engine changes it protects.

Exit gate: all Identity and Phase B issues in the epics are closed or
re-scoped in writing. The rollout invariants in
[rollout-logic.md §8](rollout-logic.md#8-invariants-to-pin-in-tests) have tests.

### Stage 4: the first new-schema stable (one-way door 2)

**Key points:**

- The first new-schema stable follows [#327](https://github.com/sixtyops/manager/issues/327),
  [#384](https://github.com/sixtyops/manager/issues/384), and
  [#385](https://github.com/sixtyops/manager/issues/385).
- It refuses to start on an old-schema database.
- It needs a rehearsal first ([#379](https://github.com/sixtyops/manager/issues/379)).

**Detail:** [#327](https://github.com/sixtyops/manager/issues/327) drops the
legacy tables, dead canary columns, and the migration chain. It runs last
among the schema-changing work. [#384](https://github.com/sixtyops/manager/issues/384)
is the final documentation sweep. [#385](https://github.com/sixtyops/manager/issues/385)
writes the release notes and CHANGELOG. The CHANGELOG must move `Unreleased`
items under a version header, as the stable release steps require.

The bridge rehearsal ([#379](https://github.com/sixtyops/manager/issues/379))
runs after [#327](https://github.com/sixtyops/manager/issues/327). It proves
that an operator can go from the bridge release to the new schema with the
install docs alone.

Entry gate:

- [#327](https://github.com/sixtyops/manager/issues/327),
  [#384](https://github.com/sixtyops/manager/issues/384), and
  [#385](https://github.com/sixtyops/manager/issues/385) merged.
- The bridge rehearsal ([#379](https://github.com/sixtyops/manager/issues/379))
  passed.
- Live baseline ([#381](https://github.com/sixtyops/manager/issues/381)) green on
  the new engine.
- Release SOP followed, including the manual stable approval.

Exit gate: the stable release is published, the signature verifies, and the
pinned image tag exists in GHCR.

## Migration order

**Key points:**

- The dev host moves first, after the bridge rehearsal passes. Design partners
  move after the dev host and the first new-schema stable.
- Each move is one-way. Take a backup of `./data`, including the Fernet key
  file, before it.
- No migration date is set in this plan.

**Detail:**

1. **Dev host.** The dev host moves to the new schema after
   [#327](https://github.com/sixtyops/manager/issues/327) and the bridge
   rehearsal ([#379](https://github.com/sixtyops/manager/issues/379)) pass. It
   uses the bridge archive and the new install path. This is the third one-way
   door. It is the live check, and it must pass before any design partner
   moves. This plan does not say whether the dev host uses a dev tag or the
   stable tag; decide that at the time, with the release scope in view. Record the backup, the import result, and the deployed
   version and health. The lead may deploy to the dev host under the grant in
   [CLAUDE.md](../CLAUDE.md#repository-lead-authority-2026-10-02). Verify the
   deployed version and health afterward. CI does not prove runtime state.
2. **Design partners.** Each partner moves on their own schedule, agreed with
   them. A partner moves only after the first new-schema stable is published.
   Before each move, confirm the partner is on the bridge release and has a
   backup of `./data` with the Fernet key. Production-device and stable-release
   actions need Isaac. They are outside the lead's grant.
3. **Partners who stay behind.** A partner who has not moved stays on the bridge
   release. The bridge release does not auto-apply newer releases. Track each
   such partner in the exit criteria below until they move or have a scheduled
   move.

Fleet safety rules do not change during migration. One wave per maintenance
window, halt on failure, and the Firmware Hold stay in force on both sides of
the move.

## Exit criteria for epic #316

**Key points:** The epic is done when all eight checks pass. Each check has an
owner issue or a recorded fact.

**Detail:**

| # | Check | Evidence |
|---|---|---|
| 1 | All invariants in [rollout-logic.md §8](rollout-logic.md#8-invariants-to-pin-in-tests) are tested | Tests in `tests/test_rollout_invariants.py` pass in CI; each invariant maps to a test name |
| 2 | Live baseline ([#381](https://github.com/sixtyops/manager/issues/381)) is green on the new engine | `dev_blocking` hardware lane result on the exact release head |
| 3 | Bridge rehearsal ([#379](https://github.com/sixtyops/manager/issues/379)) passed | Rehearsal record linked from the issue |
| 4 | Docs sweep ([#384](https://github.com/sixtyops/manager/issues/384)) merged | Merged PR |
| 5 | First new-schema stable is cut | Published release and GHCR tag |
| 6 | Dev host runs the new schema | Deployed version and health read back |
| 7 | Every design partner is migrated or has a scheduled migration | One line per partner in the epic; no partner is unaccounted for |
| 8 | No open child issue is left unowned | Each open child is closed, re-scoped in writing, or moved to a named follow-up |

Rules for these checks:

- A skipped hardware test does not pass check 2.
- A synthetic test does not pass check 3 or check 6. Only a real run counts.
- A scheduled migration in check 7 names the partner and the agreed plan. It
  does not need a date in this repository.
- The lead updates the epic body with this list. The epic body is the live
  tracker. This table is the reference.

## Risks and open items

**Key points:** Three items can change this plan. Each needs a decision from
Isaac or a bench result.

**Detail:**

- **Stable approval.** The bridge release and the first new-schema stable are
  stable releases. Stable releases are outside the lead's grant. Isaac approves
  each one.
- **Bench readiness.** Hardware proof for engine work depends on named bench
  devices. Isaac names them. Missing hardware blocks live proof. It does not
  block document and fixture work.
- **Stage 3 length.** The dev-only period has no set length. It ends when its
  exit gate passes. Do not shorten it by skipping a gate.
- **Parked work.** Broad router, database, and frontend extraction, and
  monetization, are parked in epic [#413](https://github.com/sixtyops/manager/issues/413).
  If they return before the first new-schema stable, update this plan first.

Update this document when the epic order changes. Keep issue numbers as the
source of truth for scope, and keep this file to release order, gates, and exit
criteria.
