# Scheduled firmware queue

**Key points:** Scheduled firmware updates use one serial queue. The queue derives pending devices from current Manager inventory. It runs CPEs, then APs, then switches. It starts one device at a time and completes that device's recovery before it starts the next.

**Detail:** Auto-Update (`schedule_enabled`) controls scheduled admission. Firmware Hold and the other scheduler gates apply. Scheduled updates default to Single bank. The operator can select Both banks. Manual per-device updates bypass Firmware Hold and do not advance scheduled timing.

## Window cutoff

Manager sets a cutoff for each selected firmware target. The cutoff is the earliest of:

- The maintenance window end minus 15 minutes.
- The scheduled start plus the ramp limit for the target's first or second used window. The `firmware_window_ramp_minutes` setting defaults to `[30, 120]`.
- A cutoff already stored for that target and schedule day.

The ramp starts at the scheduled start, even if Manager begins work later. A cutoff stored for the same target and day can only shorten the cutoff. Changing window hours on that day does not reset it. After cutoff, Manager starts no new device. An active device completes its recovery. Remaining devices wait for a later eligible window. See [#868](https://github.com/sixtyops/manager/issues/868).

## Deferred devices

When every device is deferred, scheduler status reports: `No devices updated. All N devices deferred: <reason>. Next scheduled window: <time>`. The reason identifies a reached cutoff, unavailable window timing, or a closed window. Manager stores this report and restores it after a restart.

The scheduler keeps checking, but it holds only the affected target until its next eligible window or until the target changes. It does not start repeat jobs for the same deferral. See [#870](https://github.com/sixtyops/manager/issues/870), [#873](https://github.com/sixtyops/manager/issues/873), and [#875](https://github.com/sixtyops/manager/issues/875).

## Preview the next window

Call `GET /api/scheduler/preview` to see the next scheduled window, devices in queue order, and why each device is held. The endpoint reads Manager settings and inventory. It does not contact devices or create jobs. See [Scheduler preview in the API reference](api.md#get-apischedulerpreview) for response fields and admission details.

## Recovery

Manager records the expected serial and address before a write. After reboot, it verifies device identity and recovery before it reports success. Unresolved identity, failed persistence, or timeout stops the job and requires operator resolution. See [recovery details](rearchitecture.md) and the [rollout model](rollout-logic.md).

## Related behavior

The queue stops after its first failure. After resume, Manager checks the failed device first and verifies recovery before any repeat write. See [the rollout model](rollout-logic.md) for failure handling and [the API reference](api.md#scheduler) for scheduler status.
