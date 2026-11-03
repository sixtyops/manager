# Scheduled firmware queue

**Key points:** Scheduled work uses the serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715). It derives pending devices from current Manager inventory and does not save wave membership. Scheduled admission stays closed until required safety proof passes.

**Detail:** The queue runs CPEs, then APs, then switches. It runs one device write and recovery sequence at a time. New and returning devices join when they need the selected firmware. Update Mode defaults to **Single bank (recommended)**. The operator can select **Both banks**. Manual per-device updates bypass Firmware Hold and do not advance scheduled timing.

The first used maintenance windows for a new firmware family or artifact use `firmware_window_ramp_minutes`, configured from 30 to 120 minutes. Later windows use the normal schedule. The persisted cutoff stops new starts and lets active recovery finish. Remaining devices wait for a later eligible window. A failed check stops the queue. After resume, Manager checks the failed device first. It does not flash again when that device already runs the target version; it verifies recovery first.

Firmware Hold remains in force for scheduled queue work. The scheduler checks readiness and other admission gates before it starts work. The serial queue does not make scheduled admission ready by itself.

## Recovery

Manager stores the expected serial and current address before a write. After reboot, it checks the current address. If a CPE is missing, Manager uses fresh successful AP peer reports to find candidates by the saved radio MAC, then reads only reported unicast addresses. The authenticated serial confirms the candidate. Manager uses the credentials selected for the update. It does not scan networks or identify a device by name.

Manager checks the expected serial before each write. It saves a moved address only after serial confirmation. Missing, conflicting, or ambiguous identity evidence, failed persistence, or timeout stops the job and requires operator resolution.

After an AP update recovers, Manager reboots each reachable CPE from the saved pre-update peer list, then checks for peer return. It skips and logs CPEs it cannot reach. The client has no documented wireless-only restart call; Manager uses the documented `POST /reboot` endpoint.

## Scheduled readiness

`scheduled_rollout_safety_ready` defaults to `false`, including on existing installs. Scheduled admission requires this flag even when `schedule_enabled=true`. The flag records the operator decision. It does not check issue status or prove bench results. Set it to `false` to stop new scheduled admission. The schedule toggle remains separate.

The live single-bank firmware test passed in dev host run 26 on 2026-11-03. Epic [#604](https://github.com/sixtyops/manager/issues/604) tracks the remaining qualification, release, and go-live steps.
