# Gradual Rollout Auto-Update System

**Key points:** This document describes shipped behavior. It does not define
the complete target safety contract. Read [one-right-way.md](one-right-way.md)
for the target and [rollout-logic.md](rollout-logic.md) for the design rules.

**Bottom line:** Auto-update rolls new firmware to the fleet in **10% → 50% → 100%**
waves, **one wave per maintenance window**, and **halts the whole job the moment a
device doesn't come back online**. New firmware is held for a configurable soak
(the **Firmware Hold**) before the first wave; the hold clears early once a device
is confirmed working on it. There is no separate canary phase — the first 10% wave
*is* the canary, protected by halt-on-failure.

The scheduler auto-detects which in-scope devices are behind their per-family target
(`tna-30x` / `tna-303l` / `tns-100`), creates a rollout, and advances it one wave per
window. A single fail-closed gate (`rollout_gate.py`) is the only place that decides
whether a wave may start. Failures pause the rollout for manual review. Manual
per-device updates bypass the release-date Firmware Hold. Address-change
qualification applies to both scheduled and manual updates, as described below.

## Scheduled safety readiness

**Key points:** Keep unattended schedules paused until safety qualification is
complete. `scheduled_rollout_safety_ready` defaults to `false`, including on
existing installs. `schedule_enabled=true` still requires this readiness record.
Manual per-device updates remain available and bypass the Firmware Hold.

**Detail:** The scheduler calls `rollout_gate.scheduled_admission_decision` before
creating a rollout, starting a job, or advancing an empty wave. Each boundary
reads current readiness after asynchronous waits. Queued scheduled reboots,
uploads, and installs read readiness again after capacity acquisition and device
preparation. Each upload attempt also reads readiness before retry backoff and
again before starting its upload process. Let a submitted operation finish its recovery. Revoking readiness
stops the next scheduled action, including another bank pass. The Auto-Update control shows the safety reason while
readiness is pending, including when the schedule is off.

Complete [#416](https://github.com/sixtyops/manager/issues/416),
[#369](https://github.com/sixtyops/manager/issues/369),
[#337](https://github.com/sixtyops/manager/issues/337),
[#341](https://github.com/sixtyops/manager/issues/341),
[#338](https://github.com/sixtyops/manager/issues/338),
[#367](https://github.com/sixtyops/manager/issues/367),
[#417](https://github.com/sixtyops/manager/issues/417), and
[#422](https://github.com/sixtyops/manager/issues/422), with exact-head bench proof.
Include the engine prerequisites
[#330](https://github.com/sixtyops/manager/issues/330),
[#381](https://github.com/sixtyops/manager/issues/381), and
[#391](https://github.com/sixtyops/manager/issues/391).
Complete [#418](https://github.com/sixtyops/manager/issues/418) before traffic
enforcement. Protect mandatory snapshots and verify recovery through
[#312](https://github.com/sixtyops/manager/issues/312). Wire the window-instance
model from [#535](https://github.com/sixtyops/manager/pull/535) into runtime
admission through [#369](https://github.com/sixtyops/manager/issues/369).
Align [#339](https://github.com/sixtyops/manager/issues/339) with the failed-window
stop: resume in a later window after the failed check passes.

Complete the identity and recovery prerequisites in
[#530](https://github.com/sixtyops/manager/issues/530):
[#288](https://github.com/sixtyops/manager/issues/288),
[#289](https://github.com/sixtyops/manager/issues/289),
[#332](https://github.com/sixtyops/manager/issues/332),
[#333](https://github.com/sixtyops/manager/issues/333),
[#291](https://github.com/sixtyops/manager/issues/291), nullable-address storage,
[#290](https://github.com/sixtyops/manager/issues/290),
[#335](https://github.com/sixtyops/manager/issues/335),
[#393](https://github.com/sixtyops/manager/issues/393),
[#394](https://github.com/sixtyops/manager/issues/394), and
[#217](https://github.com/sixtyops/manager/issues/217).
Qualify trusted identity continuity before candidate authentication or address
promotion. Keep automatic parent updates held until topology and child recovery
proof pass.

Record exact source and target build hashes, model, role, management VLAN,
DHCP/static behavior, topology, and recovery case. Prove the composed behavior on
the exact head: induced failures leave the next device unchanged; midnight and
restart preserve the window stop; held-family additions and late children remain
safe; complete child, association, and traffic recovery pass. Qualify bounded
address recovery, a different responder at the old IP, and a missing VLAN lease.
Use bench evidence to set traffic floors, deadlines, and idle/missing-counter
fallbacks. Enable only the proven model/build/topology scope. Keep unqualified
scope held in code and record the approved scope with the readiness decision.

Then have the operator record readiness through the admin settings API:
`POST /api/settings/save` with `{"scheduled_rollout_safety_ready":"true"}`.
Record the qualification evidence with that decision. This flag records the
operator decision; it does not check issue status or establish bench proof.
Set it to `false` to pause new scheduled admission again. The schedule toggle
changes `schedule_enabled` independently of this flag.

## The gate (`rollout_gate.py`)

`phase_run_decision(rollout, window_key, *, first_wave_held)` — fail-closed, two rules:

1. **One wave per maintenance window.** A wave job stamps `rollouts.last_phase_window`
   when it starts; while that equals the current window the gate holds the next wave,
   so waves never cascade through one night. DB-backed, survives a restart.
2. **Firmware Hold on the first wave.** When `first_wave_held` is true the `pct10`
   wave holds. The caller (`scheduler._held_families` + `_pending_split_by_hold`)
   computes it per firmware family: a family is **held** while its firmware's
   release-date + `firmware_canary_hold_days` hasn't elapsed **and** no in-scope
   device of that family is **confirmed working** on it. `pct50`/`pct100` never
   re-consult the hold, so a started rollout always finishes on schedule.

Every hold logs a `phase_held` or `blocked_firmware_hold` event.
`tests/test_rollout_invariants.py` drives the scheduler across ticks/windows to lock
both rules in.

## Firmware Hold & confirmed-working clear

- **Hold** = `firmware_canary_hold_days` (default/min 6) measured from the firmware's
  Tachyon **release date** (parsed from the filename `-YYYYMMDD-`; registry `added_at`
  fallback). It gates the first fleet wave only.
- **Confirmed working** = a device was updated to the target version, **passed its
  post-update smoke tests**, and is currently healthy (on-version, no `last_error`,
  seen <24h). Recorded in `firmware_confirmations (ip, version, confirmed_at)`; written
  in the post-update path (`app.py`) on smoke-pass.
- **Per family.** A confirmed `tna-30x` device never clears a held `tna-303l` family.
  **If any family in scope is held, the whole first wave waits** (all-or-nothing),
  including devices of cleared families. This is deliberate: a partial first wave would
  stamp the window, and the held family would then skip its own 10% wave and ship at
  50% (`scheduler.py` `_pending_split_by_hold`, pinned by `test_rollout_invariants.py`).
  See rule 8 in [rearchitecture.md](rearchitecture.md).
- No manual bypass exists — the hold clears only by elapsed days or a confirmed device.
  A **manual** per-device update (`/api/start-update`, `/api/update-device`) ignores the
  hold entirely, which is how the operator creates the confirming device.

## Halt-on-first-failure

A device that fails to return online cancels the update job (`app.py`) and the rollout
is paused (`scheduler.on_job_completed` on `failed_count > 0`). Smoke-test warnings,
failed checks, and exceptions mark the device failed and request job cancellation,
including with `smoke_test_strict=false`. Already active device operations can
continue. Persistent window stops and serialized recovery require the remaining
safety fixes.

## Per-device update state

`/api/fleet-status` reports `update_state ∈ {up_to_date, needs_update, on_hold}` plus a
`hold` object (`clears_at`, `confirmed_by`). A device is `on_hold` when it's behind, in
scope, auto-update is on, and its family's hold hasn't cleared. The UI shows a distinct
"on hold" glyph. The release-date hold permits a manual update. The address-change
hold shows its reason and waits for qualification or an approved named lab plan.

## Database (`database.py`)

- `rollouts` — `phase` is one of `pct10` / `pct50` / `pct100` (default `pct10`);
  `last_phase_window` drives Rule 1. `create_rollout` inserts `phase='pct10'`. A
  migration remaps any in-flight `canary` rollout to `pct10`.
- `rollout_devices` — per-device phase assignment + status.
- `firmware_confirmations` — confirmed-working records.
- `PHASE_ORDER = ["pct10", "pct50", "pct100"]`; `advance_rollout_phase` /
  `complete_rollout_phase` walk it. `mark_device_firmware_confirmed`,
  `get_confirmed_ips_for_version`, `get_firmware_hold_info` support the hold logic.

## Phase progression

| Window | Wave  | Batch              | After success |
|--------|-------|--------------------|---------------|
| 1      | pct10 | ~10% of remaining  | advance to pct50 |
| 2      | pct50 | ~50% of remaining  | advance to pct100 |
| 3      | pct100| all remaining      | rollout completed |

"Remaining" = in-scope APs whose firmware or manageable CPEs are behind, plus in-scope
switches behind their family target, minus devices already updated this rollout and
held-family devices. An empty wave auto-advances without consuming the window. New
firmware selection cancels the active rollout and starts a fresh one at `pct10`.

## Safety rules

1. **Failure pauses the rollout** — review and resume manually.
2. **One wave per maintenance window** — fail-closed gate via DB-backed `last_phase_window`.
3. **Firmware Hold soaks new firmware** before the first wave; clears early on a
   confirmed-working device, per family.
4. **New firmware = new rollout** — starts at `pct10`.
5. **Manual updates bypass the hold** — the operator's deliberate override / canary.
6. Time validation, scope, and weather checks still gate every wave.

## Target contract

**Key points:** The target adds safety gates that are not all shipped. A
pre-start block is a hold, not an outage. A failed check stops the whole
maintenance window. The operator cannot resume that rollout in the same
window or bypass a failed gate. Only one device write and recovery sequence
can run at a time.

**Detail:** The target requires a saved risk statement before each wave, a
fresh restorable backup and committed config snapshot before every write, and
recovery proof before flashing. It verifies version, CPE re-association, RSSI,
and customer traffic after each update. Traffic proof needs a tested fallback
for missing or idle traffic. Unknown, stale, or conflicting topology blocks an
automatic parent update. Operators can record a named Hold exception only
after the same safety gates pass. See [one-right-way.md](one-right-way.md).

These target rules do not describe the current scheduler or manual update
routes. In particular, shipped manual per-device updates bypass the Hold and
shipped failure handling does not enforce every target stop rule.

## Failed-window stop

Every update runs the pre-update reboot and requires authenticated recovery on
the expected serial with the original firmware version. After flashing, the
engine requires the expected serial, target firmware version, and every
role-specific smoke check with no warnings. Missing or failed proof holds the
device and stops the firmware window. Resolve the hold before retrying in a
later window. Stored settings cannot disable reboot or smoke proof.

**Key points:** Store the first failed device gate under the maintenance window
instance key. Keep that window closed to new jobs and device actions. Run one
device through recovery before starting another device.

**Detail:** A failed snapshot, pre-update reboot, firmware action, recovery, or
smoke test writes a row to `firmware_window_stops`. Keep the stop after a
manager restart. Apply it to each new job, manual update, scheduled update,
and rollout resume. In a later maintenance window, the operator records a
failure resolution through `POST /api/firmware-window-stops/{stop_id}/resolve`.
Run the normal rollout gates before starting a scheduled wave. Keep the failed
window stopped after the resolution record is saved.

## Address-change qualification hold

**Key points:** Qualify both exact builds and artifact SHA-256 values for the
model family, role, management VLAN, IP mode, and recovery case before writes.
Scheduled and manual jobs share this hold.
The qualification table in `updater/rollout_gate.py` starts empty. The Devices
view shows the hold reason with a clock glyph.

**Detail:** `address_change_decision` requires a release plus revision for both
builds. It accepts only a matching `AddressTransition` with a recorded
bench-proof reference. Its key includes `family`, `role`, `source`, `target`,
`source_sha256`, `target_sha256`, `management_vlan`, `management_ip_mode`, and
`recovery_case`. Use lowercase 64-digit hashes. Use VLAN `0` for an observed
untagged management interface or its tagged VLAN ID from 1 through 4094.
Use `dhcp` or `static` for the observed IP mode. Qualify `pre_update_reboot`
and `firmware_flash` separately. A reboot proof covers recovery on the installed
source artifact before any flash. Unknown or missing values stay held.
Release age and confirmed-working firmware affect only the separate Firmware Hold.

The shared job runner checks saved inventory before connecting, then reads
live model, firmware, installed-artifact hash, and management scope before the
pre-update reboot and before each flash. It hashes the selected target file
at each check, so files with the same numeric build have separate qualifications.
A held member uses `deferred` with an address-change reason in job history.
The existing scheduler keeps that wave pending for a later window. Qualification
is read again at each boundary and survives restart as reviewed source data.
Keep a member held through all later passes when its pre-reboot scope is held.

Read source evidence independently from inventory fields `firmware_sha256`,
`management_vlan`, and `management_ip_mode`, and from the same keys in live
`VendorDeviceInfo.extra`. Current inventory storage and the Tachyon driver
provide none of these fields. They therefore keep devices held, including
named lab exceptions, until verified scope collection is available. A build
number, a registry checksum for another file, and a bench plan supply no
observed source-artifact or management evidence.

For approved lab validation, set `SIXTYOPS_ADDRESS_BENCH_PLAN_FILE` to a private
JSON file in the lab Manager environment. Keep the tested source unchanged.
Use this structure with the approved plan's actual values:

```json
{
  "name": "approved-lab-plan",
  "approval": "approval record reference",
  "expires_at": "2026-10-28T18:00:00+00:00",
  "devices": [
    {"ip": "192.0.2.10", "family": "tna-30x", "role": "ap",
     "source": "1.12.3.54970", "target": "1.15.0.55142",
     "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "target_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
     "management_vlan": 100, "management_ip_mode": "dhcp",
     "recovery_case": "firmware_flash"}
  ]
}
```

Send the plan name as `address_bench_exception` on `/api/start-update` or
`/api/update-device`. Select it explicitly for each manual job. The gate reads
the file again at each check. Use normalized four-part builds and the actual
artifact hashes in the plan. Include a separate entry for `pre_update_reboot`
when that phase is enabled. Match the independent observed scope at each check.
An absent, expired, or malformed plan keeps the device held. Scheduled jobs
use qualification proof. Each accepted bench exception records its name,
approval, device, and transition in the schedule log. Keep private lab details
outside the repository.

Use isolated named lab addresses for these plans. Current inventory and jobs
still use IP addresses; this gate supplies transition containment, while trusted
identity and address recovery remain separate work under
[#530](https://github.com/sixtyops/manager/issues/530). Add qualification only
after exact-head bench proof and independent review. Record source and target
builds and hashes, model, role, management VLAN/DHCP behavior, recovery timing,
identity continuity, association, and traffic results with that proof. Keep
one-wave-per-window admission and all other checks in effect.
