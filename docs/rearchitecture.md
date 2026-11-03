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

- **The wave/rollout concept exists four times** (firmware, config-enforce,
  config-push, RADIUS). Only the firmware copy routes through the fail-closed
  gate in `rollout_gate.py`. The other three still carry the deleted `canary`
  phase, are not window-gated, and the RADIUS one runs all four phases
  back-to-back in a single task — the exact cascade the gate exists to prevent.
- **Job construction is copy-pasted three times** (scheduled, manual bulk,
  manual single) with **four different eligibility predicates** that disagree
  on cooldown, bank-awareness, and exact-build vs newer-than semantics. Manual
  bypass of the Firmware Hold is implemented as "manual paths never look at
  the gate," not as an explicit bypass.
- **The gate is bypassed by its own UI prediction.** `compute_next_attempt`
  re-derives the one-wave-per-window and hold rules instead of calling
  `phase_run_decision`.
- **Fleet-membership rules are unstated.** Rollout scope is re-resolved every
  tick, so a device added mid-rollout silently lands in whatever wave runs next.
- **Dead surface:** device groups, analytics, three of four report endpoints,
  freeze windows, and quick-add have routes and tables but no UI.
- Docs and code disagree on partial holds (docs say cleared families run;
  code blocks the whole first wave if any family is held).

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
  wave ordering).
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

## Rollout rules (the target contract)

**Key points:** The target replaces percentage waves with a serial queue.
The serial queue shipped in [PR #715](https://github.com/sixtyops/manager/pull/715).

**Detail:** Derive pending work from Manager inventory and the selected
firmware. Run CPEs, then APs, then switches. Allow one device write and recovery
sequence at a time. Keep Firmware Hold, readiness, eligibility, maintenance
window, freeze, clock, weather, and artifact checks. Persist the window cutoff
and first failure. Stop new starts at cutoff or after a failed check. Finish
active recovery. Continue remaining work only in a later eligible window.

A failed device must be checked first in a later window. If it already runs
the target firmware, verify recovery without flashing again. Otherwise require
operator resolution before continuing. New and returning devices join the
derived queue when they need the selected artifact and pass admission checks.
Scheduled updates use one bank. Manual per-device updates keep their deliberate
Firmware Hold bypass and do not advance scheduled timing.

The target keeps bounded reported AP peer-return checks, RSSI smoke checks,
and reported CPE `eth0` link-up when that link was up before reboot. It does
not treat bridge learning or byte deltas as proof of customer traffic. Missing
required recovery evidence stops the queue.

Current behavior remains the source of truth for what ships today. See
[gradual-rollout.md](gradual-rollout.md) for shipped wave behavior and
[rollout-logic.md](rollout-logic.md) for the target contract.

## Target layout

The target keeps one update engine, shared admission checks, current Manager
inventory, and existing job and history records. It removes percentage phase
selection, saved rollout membership, wave reservation, and straggler handling
after their runtime callers move. Keep durable failure records, confirmations,
window identity, and read-only device recovery.

## PR sequence

The executable delivery plan is [epic #695](https://github.com/sixtyops/manager/issues/695).
Each PR is one bounded slice, targets `main`, and states shipped behavior
separately from target behavior. Keep scheduled queue admission closed until
its required proof passes.

1. `docs: replace wave contract with serial queue`
2. `refactor: remove unused wave evaluator`
3. `refactor: simplify AP recovery evidence`
4. `refactor: remove unused recovery probes`
5. `feat: store firmware window timing`
6. `feat: define queue cutoff`
7. `feat: execute exact serial entries`
8. `feat: enforce device admission deadline`
9. `refactor: derive pending firmware queue`
10. `feat: replace wave scheduling`, then remove detached wave selection,
    membership lifecycle, and wave predictions in bounded PRs
11. `refactor: replace wave API state`
12. `refactor: delete wave gate`
13. Delete wave reservation, membership creation, and phase persistence in
    separate bounded PRs.

Engine and driver PRs need independent review and exact-head bench proof. Keep
all work behind existing readiness until the required safety evidence passes.

## Open follow-ups

- SSO/OIDC quality review (C7) during Phase A, before Phase B starts.
- Decide whether `docs/gradual-rollout.md` is folded into this document or
  kept as the operator-facing explanation of the rules above.
- Issue triage: #223 is absorbed by B6; #37/#39 by C2; the `compliance/P*`
  issues (#51–#60) and #36/#38/#40 should be closed or re-scoped against the
  "drift report only" decision; #189 (self-update hardening) and #120
  (self-update smoke test) are obsolete after A6.
- "Back up `./data`" docs must name the Fernet key file explicitly, or
  restores decrypt nothing.

## Historical review outcome (2026-08-29)

This review records decisions for the superseded percentage-wave proposal. It is historical context, not active delivery guidance. The serial queue contract above and [issue #695](https://github.com/sixtyops/manager/issues/695) replace its B-slice plan. B4 and the wave downgrade fence proposals are superseded; retain their review artifacts for history.

Two independent reviews (Codex CLI and a separate Claude agent) were run
against the first draft. The following points were raised by **both**
reviewers. Those findings belong to the superseded wave proposal. The archived
resolutions were: (1) keep all-or-nothing holds; (2) rollout state model in
B4; (3) AP+CPE accounting unit and fail-closed topology; (4) bridge release
A10; (5) A7/A9/A10 of the first draft moved behind their seams as B2b, B6b,
B7b; (6) dependencies folded into A3/A4a/A6a/A8; (7) rule 22 recomposed;
(8) rule 20 and rule 24; (9) rules annotated with current-vs-target. The
original findings are kept for the record:

1. **Rule 8 (partial first wave) is a safety regression as written.** If the
   first wave runs only cleared families and stamps the window, the held
   family never gets its own 10% wave: `phase_run_decision` only consults the
   hold on `pct10` (`rollout_gate.py:64-66`), so held firmware would ship at
   50% the moment its hold clears. The current all-or-nothing behavior was
   chosen deliberately (`scheduler.py:612-617`) and is pinned by
   `test_rollout_invariants.py:285-314`. Options: per-family phase state
   (schema change), or keep all-or-nothing and fix the docs instead.
2. **Rules 1, 10, and 13 contradict each other.** "Percent of remaining" is a
   moving denominator; "frozen membership" is a fixed set; "deferred devices
   retry in the next wave" moves devices between waves. Today `deferred`
   means *window cutoff* (not unreachable), a wave with deferred devices
   re-runs the same phase (`scheduler.py:1166-1181`), and a pre-reboot login
   failure is recorded as `failed` and pauses the rollout
   (`app.py:5766-5776`). Needs one state model: percentages against frozen
   membership persisted at creation; pre-flash unreachable → `deferred`,
   keeps its wave, eligible in any later wave, excluded from the next
   denominator; post-flash non-return → halt. *(This old proposal is
   superseded: target rules 9–11 hold the current wave and stop new writes
   for the whole window after any failed check.)*
3. **Rule 3 (switch/PoE) vs sizing.** Waves are sized on APs and switches
   separately and CPEs ride with their AP, so "10%" is already 10% of APs
   plus 10% of switches. Excluding a switch whose APs are in the batch can
   make a batch unfillable. Needs an accounting unit (AP+CPE group) and a
   fail-closed rule for unknown/stale topology.
4. **"Fresh install acceptable" needs an operator transition anyway.** The
   installer upgrades in place and keeps `./data`; the CSV backup carries
   devices only (no users, settings, snapshots, confirmations, job history,
   or the Fernet key). Minimum: a final "bridge" release that still
   self-applies, exports, and refuses to start on the new schema with a
   clear message; plus the A6 distribution-model decision.
5. **"Delete first" is unsafe at three seams.** A7 (notification channels)
   should follow B9's completion event; A9 (vendor abstraction) touches the
   update engine and poller (`app.py:5527`, `poller.py:530`) and needs the
   live lane; A10 (manual bulk) deletes the endpoint the AP "update now"
   button actually calls (`monitor.html:6899-6910` → `/api/start-update`)
   and one of the two implementations B1/B2 are meant to reconcile.
6. **Hidden PR dependencies:** A3 must fix
   `tests/integration/validation_matrix.py` (imports `Feature`) or the live
   lane stops collecting; A4 must first extract the device-default
   credentials from `radius_config.py` (used by the poller, `poller.py:595-615`)
   and remove `SIXTYOPS_TEST_RADIUS_AP_IP` from `dev-hardware.yml`; A6 must
   rewrite `release.yml:98` in the same PR; A8 contradicts the north-star's
7. **Rule 22 overstates the gate.** `phase_run_decision` checks status,
   window, and hold only; weather, NTP, freeze, buffer, membership, and
   topology live elsewhere. Rephrase as a composed `evaluate_wave_start`
   with the gate as one invariant inside it.
8. **Confirmation semantics need pinning:** manual updates already record
   confirmation (`app.py:5649-5655`); confirmation is scope-restricted
   (`scheduler.py:787-788`), so an out-of-scope manual update does not clear
   the hold; devices that passed in a wave that then paused still count as
   confirmed; CPE-only families can only clear by elapsed days.
9. **"Always on" rules that are not currently enforced:** pre-update snapshot
   failure is non-fatal (`app.py:5569-5570`); pre-update reboot is a setting
   (`app.py:5144`); smoke exceptions are non-fatal (`app.py:5622-5639`);
   `hold_days<=0` disables the hold (`scheduler.py:837-853`).

**Epic reviews (Codex, two rounds).** Round 1 found the fresh schema ordered before its users, a circular bridge/schema dependency, and ~12 oversized issues; fixed by staging the schema, splitting the bridge, and splitting issues. Round 2 found the bridge release depending on the removal of the self-update it needs, `merge_devices` referencing tables created later, rollout activation landing before persisted membership, and the legacy drop not guaranteed last; all fixed in the issue graph. The epic (#316) is the executable plan; this document is the rationale.

Points raised by only one reviewer and worth carrying: the window key is a
calendar date (`scheduler.py:618`), which breaks for multiple windows per day
or windows crossing midnight; rule 14 should key on firmware checksum, not
filename; rule 15 should keep a terminal `removed` row rather than deleting
membership; B10 should be many small extractions with re-exports, not one PR;
`strategies/` should wait until config normalization actually returns.
