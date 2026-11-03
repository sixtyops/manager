# Rearchitecture: back to the firmware core

> **Status:** structural decisions recorded 2026-08-29. The active firmware
> queue plan is epic [#695](https://github.com/sixtyops/manager/issues/695).
> **Supersedes** the "firmware, config, RADIUS, and monitoring" framing of
> North Star v1. `docs/north-star.md` is now v2.

## Why

The product's core goal is **automated firmware updates with opinionated
validation and rules**. Over the last several releases the codebase grew to
~26k lines of Python and a 12.4k-line single-file frontend, with ~180 routes
in one flat `app.py`. The firmware core is roughly 40% of that.

The specific problems this plan fixes:

- **Firmware queue behavior and docs diverged.** The serial queue now derives
  pending devices from inventory. PRs [#718](https://github.com/sixtyops/manager/pull/718), [#721](https://github.com/sixtyops/manager/pull/721), and [#722](https://github.com/sixtyops/manager/pull/722) removed wave
  selection, saved membership, and predictions. RADIUS and config workflows
  are not part of the firmware queue.
- **Job construction is copy-pasted three times** (scheduled, manual bulk,
  manual single) with **four different eligibility predicates** that disagree
  on cooldown, bank-awareness, and exact-build vs newer-than semantics. Manual
  bypass of the Firmware Hold is implemented as "manual paths never look at
  the gate," not as an explicit bypass.
- **The scheduler and UI need shared admission checks.** The queue derives its pending entries from current inventory at each admission.
- **Dead surface:** device groups, analytics, three of four report endpoints,
  freeze windows, and quick-add have routes and tables but no UI.

## Decisions

### Execution

| Question | Decision |
|---|---|
| Approach | **Strangler in place.** Same repo, same package, PR by PR, tests green throughout. |
| Order | **Delete first where there is no shared seam, then extract.** Clean deletions (RADIUS, enforce, self-update apply, SFTP, telemetry, dead endpoints, gating, and API tokens) go first. Deletions that touch the update engine (notification channels, vendor abstraction, manual bulk) follow the extraction PR that creates their seam, so the engine is edited once. |
| Database compatibility | **Staged schema, then a bridge release.** A shared schema builder first reproduces the current schema and tests use it; identity and rollout tables are added by the issues that need them; legacy tables, dead canary columns, and the migration chain are dropped last. Before that drop, one final self-applying **bridge release** ships a full export (devices, users, settings, config snapshots, firmware confirmations, job history, and the Fernet key), refuses to auto-apply anything newer, and tells the operator to reinstall and import. |
| Distribution | **Pinned GHCR image.** `docker-compose.yml` gains `image: ghcr.io/sixtyops/manager:vX.Y.Z`; `install.sh` stops cloning source; upgrade is "edit the tag, `docker compose pull && up -d`". Release-side signed-tag verification stays in CI. |
| Frontend | **Split into modules, still vanilla.** No framework, no build step. Separate JS/CSS files per area. |
| Vendor abstraction | **Tachyon only.** Remove `VendorDriver` and the Mikrotik/Cambium stubs. |
| Test bar | **Unit tests green on every PR; live `dev_blocking` hardware lane required for any PR touching the update engine, scheduler, or gate.** |

### Feature disposition

This disposition records the earlier audit. Shipped queue behavior is listed in Rollout rules below.

**Keep as-is (trustworthy)**

- Firmware update engine, scheduler, `rollout_gate.py`, Firmware Hold,
  smoke tests, firmware fetcher + checksum verification, `firmware_policy.py`.
- Config snapshots / backup / restore / diff / recycle bin (single encrypted
  write path, real-DB tests).
- Local users + roles (viewer/operator/admin), audit log.
- Job and per-device update history with CSV export.
- Signal health / rain fade / link budget (RSSI sanity feeds the smoke test;
  the charts stay).
- Device portal, switch-port → AP topology cascade (needed for switch/PoE
  serial queue ordering).
- Slack and email notifications.
- Let's Encrypt / SSL manager.
- Weather Guard, NTP clock validation, freeze windows, pre-update reboot.

**Keep with fixes**

| Area | Required fixes |
|---|---|
| Config templates | Encrypt `config_templates.form_data` (currently plaintext; holds secrets). Make `validate_fragment_safety` recursive and reject non-dict fragments. |
| Config push (immediate, single/multi device) + rollback | Take the per-IP push lock in the rollback route (#39). Verify post-apply hash against the pushed merged config, and verify rollback against the target hash (#37). Collapse the five copies of "login → fetch → snapshot → merge → dry-run → apply" into one primitive. |
| Config compliance | Keep only as a **read-only drift report**. Fix the subset-vs-replace mismatch on lists before it is ever used to drive enforcement. |
| Release check | Keep the GitHub release check, in-app "vX.Y available" banner, and Slack/email notice on new stable release. |
| Freeze windows | Build the missing UI in the schedule drawer. |
| SSO / OIDC | Provisionally kept; needs a short quality review (not covered by the audit) before the extract phase. |
| Config snapshot pruning | Count-based pruning can age out the pre-push safety snapshot; tag safety snapshots and exempt them. Raise the swallowed fetch error from `debug`. |

**Delete**

| Area | Reason |
|---|---|
| RADIUS (server, users, LDAP, rollout, push, UI, 5 tables, docs) | Restricted mode doesn't restrict; rollout has no snapshot/lock/verify/persistence and overwrites the manager's fallback device credentials on success. Device-admin auth has better homes elsewhere. |
| ~~Config auto-enforce + phased config-push rollout~~ **Removed** | In-process four-phase loops that bypass the gate; auto-rollback is unlocked and unverified. Normalization returns later as a strategy on the shared engine. |
| Self-update **apply** path | Docker-socket container recreate, client-side GPG verification, repo mount, `entrypoint.sh` socket/ownership prep (the entrypoint's `SEED_DATA` seeding and privilege drop to `appuser` must be preserved). `docker-compose.yml` uses `build: .` with no `image:`, so the replacement upgrade procedure requires choosing a distribution model first: pinned GHCR image + `image:` in compose (`docker compose pull && up -d`), or git checkout + `up -d --build`. Release-side tag signing in `release.yml` stays; the trusted key moves out of `updater/trusted_keys/`. |
| SFTP off-box backup | Archive ships the SQLite file plus the encryption key. Replace with "back up `./data`" docs. |
| SNMP traps, generic webhooks, syslog forwarding | NOC integrations; Slack + email suffice. |
| Vendor driver abstraction, Mikrotik/Cambium stubs | Unearned. |
| Device groups, analytics endpoints, 3 of 4 report endpoints, quick-add | No UI; dead. |
| Feature-gating scaffold (`features.py`, `license.py`, `require_feature`/`require_pro` on ~60 routes) | No-op since the open-source conversion. |
| Manual **bulk** update | Fleets move via rollouts. Single-device manual update stays (see rules). |
| `devmode.py` dummy poller | Replaced by the seed script. |

## Rollout rules (shipped queue and remaining target)

**Key points:** The serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715) and follow-up PRs. Wave selection, membership, and predictions have been removed. The remaining safety contract is target work.

**Detail:** Shipped work derives pending devices from Manager inventory and runs CPEs, then APs, then switches, one device at a time. The first used windows for each new firmware family or artifact use the configured 30-120 minute ramp. A persisted cutoff stops new starts and lets active recovery finish. A failure stops the queue; after resume, Manager checks that device first. Scheduled updates use one bank.

A failed device must be checked first in a later window. If it already runs
the target firmware, verify recovery without flashing again. Otherwise require
operator resolution before continuing. New and returning devices join the
derived queue when they need the selected artifact and pass admission checks.
Update Mode defaults to one bank, and the operator can select both banks. Manual per-device updates keep their deliberate
Firmware Hold bypass and do not advance scheduled timing.

The target keeps bounded reported AP peer-return checks, RSSI smoke checks,
and reported CPE `eth0` link-up when that link was up before reboot. It does
not treat bridge learning or byte deltas as proof of customer traffic. Missing
required recovery evidence stops the queue.

Remaining target work includes the specified readiness and recovery gates that are not yet complete. Keep scheduled admission closed until required proof passes. See [gradual-rollout.md](gradual-rollout.md) for shipped behavior and [rollout-logic.md](rollout-logic.md) for queue rules.

## Shipped layout

The scheduler derives the queue from current Manager inventory and uses one update engine. PRs [#718](https://github.com/sixtyops/manager/pull/718), [#721](https://github.com/sixtyops/manager/pull/721), and [#722](https://github.com/sixtyops/manager/pull/722) removed wave selection, saved membership, and predictions. Serial rediscovery and serial checks before each write shipped in [#706](https://github.com/sixtyops/manager/pull/706). Address-change holds were removed in [#707](https://github.com/sixtyops/manager/pull/707), [#708](https://github.com/sixtyops/manager/pull/708), and [#710](https://github.com/sixtyops/manager/pull/710).

## Historical delivery plan

The earlier staged rollout plan is superseded. Track current queue proof in [epic #604](https://github.com/sixtyops/manager/issues/604). Scheduled admission stays closed until the required proof passes.

## Open follow-ups

This list records older architecture work. Track current firmware queue proof in [#604](https://github.com/sixtyops/manager/issues/604).

- SSO/OIDC quality review (C7) during Phase A, before Phase B starts.
- Decide whether `docs/gradual-rollout.md` is folded into this document or
  kept as the operator-facing explanation of the rules above.
- Issue triage: #223 is absorbed by B6; #37/#39 by C2; the `compliance/P*`
  issues (#51–#60) and #36/#38/#40 should be closed or re-scoped against the
  "drift report only" decision; #189 (self-update hardening) and #120
  (self-update smoke test) are obsolete after A6.
- "Back up `./data`" docs must name the Fernet key file explicitly, or
  restores decrypt nothing.

## Historical review outcome

The archived review below covered a superseded percentage rollout proposal. Use the shipped queue rules above and [epic #604](https://github.com/sixtyops/manager/issues/604) for current behavior and status.
