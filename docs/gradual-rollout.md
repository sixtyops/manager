# Scheduled firmware queue

**Key points:** Scheduled work uses the serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715). It derives pending devices from current Manager inventory and does not save wave membership. Scheduled admission stays closed until required safety proof passes.

**Detail:** The queue runs CPEs, then APs, then switches. It runs one device write and recovery sequence at a time. New and returning devices join when they need the selected firmware. Update Mode defaults to **Single bank (recommended)**. The operator can select **Both banks**. Manual per-device updates bypass Firmware Hold and do not advance scheduled timing.

The first used maintenance windows for a new firmware family or artifact use `firmware_window_ramp_minutes`, configured from 30 to 120 minutes in settings. Later windows use the normal schedule. Scheduler status shows the persisted cutoff and ramp window ordinal. The cutoff stops new starts and lets active recovery finish. Remaining devices wait for a later eligible window. A failed check stops the queue. After resume, Manager checks the failed device first. It does not flash again when that device already runs the target version; it verifies recovery first.

The serial queue ignores legacy wave rollouts during admission. Manager keeps legacy wave rows as history and excludes them from current scheduler status. Startup migration resolves duplicate legacy CPE rows by mapping a newer observation to the existing migrated CPE; AP and switch IP conflicts remain errors.

Firmware Hold remains in force for scheduled queue work. The scheduler checks readiness and other admission gates before it starts work. The serial queue does not make scheduled admission ready by itself.

## Recovery

Manager stores the expected serial and current address before a write. After reboot, it checks the current address. If a CPE is missing, Manager uses fresh successful AP peer reports to find candidates by the saved radio MAC, then reads only reported unicast addresses. The authenticated serial confirms the candidate. Manager uses the credentials selected for the update. It does not scan networks or identify a device by name.

Manager checks the expected serial before each write. It saves a moved address only after serial confirmation. Missing, conflicting, or ambiguous identity evidence, failed persistence, or timeout stops the job and requires operator resolution.

After an AP update recovers, Manager calls `POST /reboot` for each reachable CPE in the saved pre-update peer list, then checks for peer return. On Tachyon firmware 1.15.1, this endpoint returns HTTP 302 and does not reboot the device. The CPE reboot step therefore does not reboot the CPE. This behavior is tracked in [issue #790](https://github.com/sixtyops/manager/issues/790). AP re-find through its CPEs is planned in [issue #786](https://github.com/sixtyops/manager/issues/786).

## Scheduled readiness

`scheduled_rollout_safety_ready` defaults to `false`, including on existing installs. Scheduled admission requires this flag even when `schedule_enabled=true`. The flag records the operator decision. It does not check issue status or prove bench results. Set it to `false` to stop new scheduled admission. The schedule toggle remains separate.

The live single-bank firmware test passed in dev host run 26 on 2026-11-03.
Scheduled serial queue testing passed on the dev host in run 28c on 2026-11-04.
Epic [#604](https://github.com/sixtyops/manager/issues/604) records C3 as dropped
by the owner. C4 release preparation is complete in
[#732](https://github.com/sixtyops/manager/pull/732) for version 1.4.1-dev6;
the tag awaits the owner's signature after the UI review. C5 step 1 passed;
steps 2 and 3 await the owner.
