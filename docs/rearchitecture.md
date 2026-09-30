# Rearchitecture: back to the firmware core

> **Status:** decisions recorded 2026-08-29. The executable plan is epic
> [#316](https://github.com/sixtyops/manager/issues/316): one issue per PR.
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
| Order | **Delete first where there is no shared seam, then extract.** Clean deletions (RADIUS, enforce, self-update apply, SFTP, telemetry, dead endpoints, gating, API tokens) go first. Deletions that touch the update engine (notification channels, vendor abstraction, manual bulk) follow the extraction PR that creates their seam, so the engine is edited once. |
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
| Config auto-enforce + phased config-push rollout | In-process four-phase loops that bypass the gate; auto-rollback is unlocked and unverified. Normalization returns later as a strategy on the shared engine. |
| Self-update **apply** path | Docker-socket container recreate, client-side GPG verification, repo mount, `entrypoint.sh` socket/ownership prep (the entrypoint's `SEED_DATA` seeding and privilege drop to `appuser` must be preserved). `docker-compose.yml` uses `build: .` with no `image:`, so the replacement upgrade procedure requires choosing a distribution model first: pinned GHCR image + `image:` in compose (`docker compose pull && up -d`), or git checkout + `up -d --build`. Release-side tag signing in `release.yml` stays; the trusted key moves out of `updater/trusted_keys/`. |
| SFTP off-box backup | Archive ships the SQLite file plus the encryption key. Replace with "back up `./data`" docs. |
| Anonymous telemetry | Not needed for the core. |
| SNMP traps, generic webhooks, syslog forwarding | NOC integrations; Slack + email suffice. |
| API tokens | No UI, no consumers. |
| Vendor driver abstraction, Mikrotik/Cambium stubs | Unearned. |
| Device groups, analytics endpoints, 3 of 4 report endpoints, quick-add | No UI; dead. |
| Feature-gating scaffold (`features.py`, `license.py`, `require_feature`/`require_pro` on ~60 routes) | No-op since the open-source conversion. |
| Manual **bulk** update | Fleets move via rollouts. Single-device manual update stays (see rules). |
| `devmode.py` dummy poller | Replaced by the seed script. |

## Rollout rules (the opinionated part)

> The precise definition — identity model, lifecycle state machine, wave
> construction, and the edge-case table — is in
> [docs/rollout-logic.md](rollout-logic.md). The rules below are the summary.

These are the invariants the rebuilt core enforces. They are written as
rules so they can be pinned by `tests/test_rollout_invariants.py`. Items
marked **always on** cannot be disabled by the operator; operators configure
*when* and *how fast*, never *how safe*.

Where a rule differs from current behavior, the difference is called out;
"always on" describes the target, not necessarily today's code.

### Waves

1. A rollout progresses **10% → 50% → 100%, cumulative, of the units**
   (rule 3) built from the frozen membership (rule 13): for 100 units the
   waves are 10, 40, 50. No canary phase. **Always on.** *(Today each
   percentage is taken of the then-remaining candidates, giving 10/45/45.)*
   Rounding is `ceil`, minimum batch 1 unit. Removed and disabled members are
   not in the denominator. Deferred units stay in the denominator and retry.
   See [rollout-logic.md §4.2](rollout-logic.md#42-sizing).
2. **One wave per maintenance window**, DB-backed so restarts cannot cascade.
   **Always on.** The window key must identify a *window instance*, not a
   calendar date, so multiple windows per day and windows crossing midnight
   are handled. *(Today the key is `YYYY-MM-DD`, `scheduler.py:618`.)*
3. The accounting unit is the **AP with its attached CPEs**; switches are
   separate units. Within a wave, units run **CPEs → APs → switches**. A
   switch is eligible only after every AP it powers (per the switch-port
   topology) has succeeded in an *earlier* wave; if a switch's topology is
   unknown or stale it is held to the final wave. No site-spread or
   model-spread rules. **Always on.**
4. Parallelism is read from a settings snapshot taken **at the start of each
   wave**, not at rollout creation; window and weather settings are read
   live. Changes are logged. Updates are always **single-bank** (the vendor
   recommendation) and there is no bank-mode setting. *(Today bank mode is a
   setting; both are snapshotted once at rollout creation,
   `scheduler.py:591-596`.)*

### Firmware Hold and confirmation

5. New firmware is held for `firmware_canary_hold_days` from its release
   date, per model family. **Always on** (days are configurable).
6. The hold for a family clears early once **one device in that family
   passes a clean smoke test** and is still healthy at read time. A confirmed
   device in one family never clears another family. No additional soak period.
7. Post-update smoke test (version match, CPE re-association, RSSI sanity)
   is required for a device to count as confirmed. **Always on.**
8. **If any family in scope is held, the first wave waits** (all-or-nothing).
   This is current behavior (`scheduler.py:612-617`, pinned by
   `test_rollout_invariants.py:285-314`) and is kept deliberately: a
   partial first wave would let the held family skip its own 10% wave.
   `docs/gradual-rollout.md` is corrected to say this.

### Failure handling

9. Once a device is past the **point of no return** (firmware upload has
   begun), any failure or non-return **stops launching new devices, lets
   in-flight flashes finish, and pauses the rollout**. **Always on.**
   *(Today a generic failure marks the device failed and pauses at job end;
   only strict smoke failure and non-return cancel immediately,
   `app.py:5647-5665`.)*
10. A device that is unreachable **before** the point of no return (login,
    snapshot, or pre-update reboot never reaches the device) is marked
    **deferred**: it keeps its wave assignment, the wave completes and the
    rollout advances without it, and it is eligible in any later wave.
    *(Today `deferred` means window cutoff, a pre-reboot login failure is
    recorded as `failed`, and a wave with deferred devices re-runs the same
    phase, `app.py:5766-5776`, `scheduler.py:1166-1181`.)* A device that was
    deliberately rebooted and did not recover is a **failure**, not a
    deferral.
11. Every device gets a **pre-update config snapshot** committed before
    upload begins; snapshot failure defers the device. **Always on.**
    *(Today snapshot failure is non-fatal, `app.py:5569-5570`.)*
12. **Pre-update reboot** proves the device recovers before flashing.
    **Always on.** *(Today it is a setting, `app.py:5144`.)*

### Fleet membership

13. **Rollout membership is persisted at creation** (one `rollout_devices`
    row per member, wave unassigned). A device added to the fleet
    mid-rollout is appended as a **straggler** and joins **only the final
    (100%) wave**; the straggler cutoff is the moment the final wave starts,
    so late additions cannot keep a rollout open. *(Today rows are created
    only when a device is assigned to a wave, `scheduler.py:736-739`.)*
14. The rollout is keyed to the **firmware artifact (checksum), not the
    filename**. Selecting a different artifact for any family **cancels the
    in-progress rollout**; a new rollout starts with the Hold computed from
    that artifact's release date.
15. A device deleted or disabled mid-rollout keeps its membership row with a
    terminal reason (`removed`, `disabled`) for auditability; it is excluded
    from wave sizing.

### Secondary gates

16. **Weather Guard** (minimum temperature via weather.gov) blocks a wave.
17. **NTP clock validation** hard-blocks on clock drift; unreachable NTP
    degrades to the system clock.
18. **Freeze windows** block waves outright.
19. No new job starts with fewer than 15 minutes left in the window; in-flight
    devices may run past window end; devices past the per-device cutoff are
    deferred.

### Manual updates

20. Exactly **one** manual path: "update this device now." It runs through
    the same job builder as the scheduler with an explicit, audit-logged
    `bypass_hold=True` that is set by the route, never by request input.
    It bypasses **only** the Hold — snapshot, pre-reboot, smoke test, and
    version checks still apply. It is the intended way to test new firmware
    and clear the Hold: a clean smoke pass on the rollout's artifact records
    a confirmation regardless of scheduler scope. *(Today confirmation is
    scope-restricted, `scheduler.py:787-788`.)* For an AP, "update now"
    includes its attached CPEs (matching the current `/api/start-update`
    behavior of the AP button, not `/api/update-device`). Bulk manual
    selection and the whole-site button do not exist.

### Engine shape

21. There is **one rollout engine** ("roll an action across the fleet under
    the gate"), one `rollouts`/`rollout_devices` table pair, and one wave
    loop. Firmware flashing is the first action strategy. Config normalization
    is a future strategy; it must not get its own loop.
22. Wave start is decided by **one composed function**
    (`evaluate_wave_start(context)`) that runs the named gates in order —
    status, one-wave-per-window, Hold, freeze, NTP, window, end buffer,
    weather — and returns a reason code. `phase_run_decision` is one invariant
    inside it. The UI's "next attempt" prediction calls the same function;
    nothing re-derives its rules.
23. Four questions are kept distinct and each has one implementation:
    **membership** (was it in the rollout at creation / straggler),
    **validity** (still present, enabled, in scope), **action eligibility**
    (still needs this artifact — the one predicate shared by scheduler and
    manual path), and **scheduling eligibility** (reachable, not held,
    topology-safe).
24. Confirmations recorded by a wave that ends in a pause are **voided**; the
    Hold clears only from a wave or manual update that completed cleanly.
    A CPE-only family clears by elapsed days or by a manual CPE update.

## Target layout

```
updater/
  main.py                 create_app(); the one FastAPI instance
  api/                    routers: devices, firmware, rollouts, configs,
                          config_push, auth, settings, notifications, ws
  core/
    gates.py              evaluate_wave_start, WaveStartContext, ordered gates
    scheduler.py          the tick loop (moved from updater/scheduler.py last)
    engine.py             rollout state machine, membership, units, waves,
                          halt, defer, events
    eligibility.py        the one needs_update predicate
    jobs.py               build_job + the job runner (flash one device, smoke)
    identity.py           match, merge, reconcile addresses
    lifespan.py           startup/shutdown, recovery hook
    broadcast.py          WebSocket connection state
  device/
    tachyon.py            the Tachyon client (only vendor)
    credentials.py        default device credentials
  poller.py               inventory, CPE, topology, config polling;
                          calls core/identity
  db/                     schema.py + devices, rollouts, configs, auth, settings
  config/push.py          push_one: lock, snapshot, merge|replace, dry-run,
                          apply, verify
  firmware_policy.py      artifacts, families, targets
  firmware_fetcher.py
  backup.py               CSV export, bridge export
  bridge_import.py
  notify/                 slack.py, email.py (JobCompleted subscribers)
  templates/monitor.html  shell only
  static/css/, static/js/ one file per area
```

Not in the final tree: `app.py` (or a one-line shim), `scheduler.py`,
`tachyon.py`, `vendors/`, `radius_*`, `sftp_backup.py`, `telemetry.py`,
`snmp.py`, `webhooks.py`, `syslog_forwarder.py`, `devmode.py`,
`features.py`, `license.py`, the apply path of `release_checker.py`.

Rules that keep the tree honest:

- `core/` never imports `api/` or `poller.py`. `poller.py` calls `core`.
- Behavior changes land in a module's final home. Pure moves happen
  **before** the behavior change (job runner before the point-of-no-return
  work; routers and db modules after their feature work).
- No compatibility shims survive the epic. The last issue deletes them.

## PR sequence

The executable plan is **epic #316** and its child issues, one PR each.
The phases are:

- **Phase A** — clean deletions, distribution model, bridge release, staged
  schema. 34 issues. Runs mostly in parallel.
- **Identity** — serial + all MACs, CPEs as devices, never delete on offline,
  address reconciliation, rekey by `device_id`. 9 issues, in order.
- **Phase B** — the rollout engine: one eligibility predicate, one job
  builder, composed gate, rollout state model (11 small issues), completion
  event, engine/scheduler split, switch/PoE batching, router and db module
  extraction. Live hardware lane on every engine PR.
- **Phase C** — config fixes (encrypt `form_data`, lock + verify rollback,
  one push primitive, snapshot tagging), freeze-window UI, frontend split.

Rules that apply to every PR: one issue per PR; CHANGELOG entry for every
user-visible change; tests and docs for a removed feature go in the same PR
as the removal; use function and route names, not line numbers.

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

## Review outcome (2026-08-29)

Two independent reviews (Codex CLI and a separate Claude agent) were run
against the first draft. The following points were raised by **both**
reviewers. Each has been resolved in the rules and PR tables above; the
resolutions are: (1) keep all-or-nothing holds; (2) rollout state model in
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
   denominator; post-flash non-return → halt. *(Superseded in part: rule 1
   keeps deferred units in the denominator.)*
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
   telemetry-based "active design partner" metric.
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
