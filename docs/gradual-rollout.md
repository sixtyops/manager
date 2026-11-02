# Gradual Rollout Auto-Update System

**Key points:** This document describes shipped behavior. It does not define
the complete target safety contract. Read [one-right-way.md](one-right-way.md)
for the target and [rollout-logic.md](rollout-logic.md) for the design rules.

**Key points:** Current code runs percentage waves. The serial queue in
[issue #695](https://github.com/sixtyops/manager/issues/695) is a target and
will not ship until its cutover PRs land.

**Detail:** Today the scheduler advances in-scope devices through 10%, 50%,
and 100% waves, with one wave per maintenance window. It uses
`rollout_gate.py` to check scheduled admission. Firmware Hold applies before
the first scheduled wave. Manual per-device updates bypass that hold. Failure
handling and device recovery do not yet enforce the full target contract.
The **Update Mode** setting defaults to **Single bank (recommended)**. Select
**Both banks** to update both banks. Address-change qualification applies to
both scheduled and manual updates, as described below.

The target derives an ordered queue from Manager's current inventory. It runs
CPEs, then APs, then switches, with one device write and recovery sequence at
a time. Limit the first used maintenance windows for each new firmware family
or artifact with the configured minute ramp; later windows use the normal
schedule. It keeps Firmware Hold and stops new starts at a persisted cutoff,
and stops the queue after the first failure. An active device finishes its
recovery. Remaining devices wait for a later window. New and returning devices
join the derived queue. Scheduled updates use one bank. See
[one-right-way.md](one-right-way.md) and [rollout-logic.md](rollout-logic.md).

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

The serial queue plan in [#695](https://github.com/sixtyops/manager/issues/695)
supersedes the rollout scope in [#338](https://github.com/sixtyops/manager/issues/338).
Complete [#416](https://github.com/sixtyops/manager/issues/416),
[#369](https://github.com/sixtyops/manager/issues/369),
[#337](https://github.com/sixtyops/manager/issues/337),
[#341](https://github.com/sixtyops/manager/issues/341),
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

## Shipped wave gate

Current code uses `phase_run_decision` to enforce one percentage wave per
maintenance window and Firmware Hold on the first wave. The scheduler computes
Hold by firmware family. A confirmed working device can clear Hold for its
family. These are shipped wave rules. The target keeps Firmware Hold but
replaces percentage progression with serial queue admission.

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
- No manual bypass exists — the hold clears by elapsed days or a confirmed device.
  A **manual** per-device update (`/api/start-update`, `/api/update-device`) ignores the
  hold entirely, which is how the operator creates the confirming device.

## Failure handling

Today a device that does not return online cancels its update job, and the
scheduler pauses the rollout when the job reports a failure. Other failures do
not yet enforce the full target stop rule. Persistent window stops and serial
recovery remain target work.

The target records the first failure for the maintenance window and prevents
new device starts for the rest of that window. An active device finishes
recovery. The operator resolves the failure before work resumes in a later
window. The failed window never reopens.

## Per-device update state

`/api/fleet-status` reports `update_state ∈ {up_to_date, needs_update, on_hold}` plus a
`hold` object (`clears_at`, `confirmed_by`). A device is `on_hold` when it's behind, in
scope, auto-update is on, and its family's hold hasn't cleared. The UI shows a distinct
"on hold" glyph. The release-date hold permits a manual update. The address-change
hold shows its reason and waits for qualification or an approved named lab plan.

## Shipped rollout state

Today `rollouts.phase` stores `pct10`, `pct50`, or `pct100`. The
`rollout_devices` table stores per-device phase assignment. Firmware
confirmations support the current Hold behavior. These structures remain in
use until the cutover PRs land.

## Target queue behavior

The target uses one ordered queue of devices that need the selected firmware.
It does not save percentage membership or calculate wave sizes. A device that
becomes eligible joins the queue from current inventory. Run CPEs before APs,
then switches. Keep one device write and recovery sequence active at a time.
Persist the maintenance-window cutoff and first failure. Stop new starts at the
cutoff, finish active recovery, and continue remaining work in a later window.

## Target contract

**Key points:** The target derives an ordered queue from current inventory. It
runs one device write and recovery sequence at a time. This contract is not
shipped. Current code still runs percentage waves until the cutover PRs land.

**Detail:** The target requires a saved risk statement, a fresh restorable
backup, a committed config snapshot, and recovery proof before each write. It
checks version and role-specific recovery evidence after each update. For AP
recovery, use bounded reported peer-return checks, RSSI smoke checks, and
reported CPE `eth0` link-up when that link was up before reboot. Do not infer
customer application traffic from bridge learning or byte counters. Unknown,
stale, or conflicting topology blocks an automatic parent update.

Stop new starts at the persisted window cutoff and finish active recovery. A
failed check stops new starts for the whole window. Resolve the failure before
continuing in a later window. Manual per-device updates keep their deliberate
Firmware Hold bypass. See [one-right-way.md](one-right-way.md) for the target
contract and [rollout-logic.md](rollout-logic.md) for its rules.

These target rules do not describe the current scheduler or manual update
routes. Shipped manual per-device updates bypass Firmware Hold. Shipped failure
handling does not enforce every target stop rule.

## Failed-window stop

Every update runs the pre-update reboot and requires authenticated recovery on
the expected serial with the original firmware version. After flashing, the
engine requires the expected serial, target firmware version, and every
role-specific smoke check with no warnings. Missing or failed proof holds the
device and stops the firmware window. Resolve the hold before retrying in a
later window. Stored settings cannot disable reboot or smoke proof.

AP recovery checks bounded reported peer return and reported CPE `eth0`
link-up when it was up before reboot. The existing RSSI smoke check also runs.
Bridge learning, byte counters, and link rates do not prove customer application
traffic. Missing required recovery evidence stops the queue.

After AP firmware recovery, Manager reboots each reachable CPE in the saved
pre-update peer list, then checks that the peers and customer paths returned.
The Tachyon client has no documented wireless-only restart call, so it uses
the documented `POST /reboot` endpoint. Unreachable CPEs are skipped and logged.

**Key points:** Store the first failed device gate under the maintenance window
instance key. Keep that window closed to new jobs and device actions. Run one
device through recovery before starting another device.

**Detail:** A failed snapshot, pre-update reboot, firmware action, recovery, or
smoke test writes a row to `firmware_window_stops`. Keep the stop after a
manager restart. Apply it to each new job, manual update, scheduled update,
and rollout resume. In a later maintenance window, the operator records a
failure resolution through `POST /api/firmware-window-stops/{stop_id}/resolve`.
Run the normal rollout gates before starting scheduled queue work. Keep the failed
window stopped after the resolution record is saved.

## Address-change qualification hold

**Key points:** Qualify the device-reported source build and target artifact
SHA-256 for the model family, role, management VLAN, IP mode, and recovery case
before writes.
Scheduled and manual jobs share this hold.
The qualification table in `updater/rollout_gate.py` starts empty. The Devices
view shows the hold reason with a clock glyph.

**Detail:** Owner decision on 2026-11-01: trust the full firmware version
reported by the Tachyon device as the source identity; do not require an
installed-image hash. `address_change_decision` requires a release plus
revision for both builds. It accepts only a matching `AddressTransition` with
a recorded bench-proof reference. Its key includes `family`, `role`, `source`,
`target`, `target_sha256`, `management_vlan`, `management_ip_mode`, and
`recovery_case`. Use lowercase 64-digit target hashes. Use VLAN `0` for an observed
untagged management interface or its tagged VLAN ID from 1 through 4094.
Use `dhcp` or `static` for the observed IP mode. Qualify `pre_update_reboot`
and `firmware_flash` separately. A reboot proof covers recovery before flashing
that reported source build. Unknown or missing values stay held.
Release age and confirmed-working firmware affect only the separate Firmware Hold.

The shared job runner checks saved inventory before connecting, then reads
live model, firmware version, and management scope before the
pre-update reboot and before each flash. It hashes the selected target file
at each check, so files with the same numeric build have separate qualifications.
A held member uses `deferred` with an address-change reason in job history.
The existing scheduler keeps that wave pending for a later window. Qualification
is read again at each boundary and survives restart as reviewed source data.
Keep a member held through all later passes when its pre-reboot scope is held.

Read the source version from live Tachyon device information. Read management
scope independently from inventory fields `management_vlan` and
`management_ip_mode`, and from the same keys in live `VendorDeviceInfo.extra`.
Unknown or missing fields keep devices held, including named lab exceptions.
The Manager hashes the selected stored target file; a registry checksum for a
different file does not qualify it.

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
An absent, expired, or malformed plan keeps the device held. A plan that still
contains `source_sha256` is rejected with a clear address-change hold error;
remove that field from every device entry. Scheduled jobs use qualification
proof. Each accepted bench exception records its name,
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
