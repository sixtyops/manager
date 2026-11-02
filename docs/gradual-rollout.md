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
**Both banks** to update both banks. Update recovery confirms the device after
a reboot before it continues with another write.

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

## Update recovery

**Key points:** Recovery confirms a Tachyon device with an authenticated serial
read. Treat an AP peer radio MAC as a candidate selector only.

**Detail:** Before a write, save the expected serial and current locator in the
active job. Keep the original IP as the job and history key. After a reboot,
check the current address first. For a missing CPE, use fresh successful peer
reports from enabled APs to find rows with the saved radio MAC, then read only
their reported unicast IPs. Use the credentials already selected for the update.
Confirm the expected serial and compatible model before accepting one current
address and parent. Do not scan networks, try other credentials, or infer
identity from a name.

Keep one deadline for recovery. Persist a moved address only after serial
confirmation, refuse cache collisions, and invalidate sessions for the old and
new addresses. Recheck the expected serial before every reboot, upload attempt,
install trigger, reconnect, and bank pass. Missing evidence, ambiguity, failed
persistence, or timeout stops the whole job and requires operator resolution in
a later window. Never reflash an unfinished update after restart.

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
"on hold" glyph. The release-date hold permits a manual update.

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

## Update recovery identity check

**Key points:** Recovery authenticates with the same credential pair selected
for the update. It confirms identity with a serial read before continuing.

**Detail:** Manager uses its existing device management authentication
model. It does not save or check device certificate pins. Recovery for a CPE
first tries its current address. If the device is missing, Manager checks
fresh successful peer reports for the saved radio MAC and reads only reported
unicast addresses. The radio MAC selects a candidate; the authenticated serial
confirms identity.

Recovery stops when the serial is missing or different, when candidates are
ambiguous, or when an identity conflict exists. It does not use another AP's
credentials, global credentials, subnet scans, or device names. Manager saves
a confirmed current address before continuing and checks the expected serial
before each later write. A recovery failure stops the update and the rollout
window under the failure rules above.

Certificate pinning, separate credential profiles, general address
promotion, and broader identity migration are not planned. This recovery
scope does not make scheduled updates ready. Keep the existing admission and
exact-head proof gates in place.
