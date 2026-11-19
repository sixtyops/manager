# Scheduled firmware queue

**Key points:** Scheduled work uses the serial queue shipped in [#715](https://github.com/sixtyops/manager/pull/715). It derives pending devices from current Manager inventory and does not save wave membership. Auto-Update controls scheduled admission.

**Detail:** The queue runs CPEs, then APs, then switches. It runs one device write and recovery sequence at a time. New and returning devices join when they need the selected firmware. Update Mode defaults to **Single bank (recommended)**. The operator can select **Both banks**. Manual per-device updates bypass Firmware Hold and do not advance scheduled timing.

The first used maintenance windows for a new firmware family or artifact use `firmware_window_ramp_minutes`, configured from 30 to 120 minutes in settings. Later windows use the normal schedule. Scheduler status shows the persisted cutoff and ramp window ordinal. The cutoff stops new starts and lets active recovery finish. Remaining devices wait for a later eligible window. A failed check stops the queue. After resume, Manager checks the failed device first. It does not flash again when that device already runs the target version; it verifies recovery first.

The serial queue ignores legacy wave rollouts during admission. Manager keeps legacy wave rows as history and excludes them from current scheduler status. Startup migration resolves duplicate legacy CPE rows by mapping a newer observation to the existing migrated CPE; AP and switch IP conflicts remain errors.

Firmware Hold remains in force for scheduled queue work. The scheduler checks Auto-Update and the other admission gates before it starts work.

## Recovery

Manager stores the expected serial and current address before a write. After reboot, it checks the current address. If a CPE is missing, Manager uses fresh successful AP peer reports to find candidates by the saved radio MAC, then reads only reported unicast addresses. The authenticated serial confirms the candidate. Manager uses the credentials selected for the update. It does not scan networks or identify a device by name.

Manager checks the expected serial before each write. It saves a moved address only after serial confirmation and a checked inventory move. For AP recovery, it keeps a verified saved address; if that address is unavailable, it checks the candidates and selects the first verified address in sorted order. Multiple addresses for one AP do not make its identity ambiguous. Conflicting identities, unresolved alternatives, failed persistence, or timeout stop the job and require operator resolution. Address changes pass only when both management snapshots show DHCP on the same VLAN.

After an AP update, Manager checks saved CPE peers and their required link state through reads. This recovery path does not reboot CPEs. The standalone Tachyon reboot method uses `POST /cgi.lua/reboot` and requires HTTP 200. It confirms a reachability drop and API return, or a lower `system.uptime` than the value read immediately before the request. Missing reboot evidence fails at the existing deadline. AP recovery can re-find a changed address through its saved CPE peers.

## Auto-Update

**Key points:** Auto-Update (`schedule_enabled`) is the one switch for scheduled updates.

**Detail:** Turn Auto-Update on to admit scheduled work within the maintenance window. Turn it off to stop new scheduled writes and let active recovery finish. Manager ignores an old stored `scheduled_rollout_safety_ready` value. Serial order, Firmware Hold, cutoff, first-failure stop, unresolved stops, and window identity still apply.

The live single-bank firmware test passed in dev host run 26 on 2026-11-03.
Scheduled serial queue testing passed on the dev host in run 28c on 2026-11-04.
Epic [#604](https://github.com/sixtyops/manager/issues/604) records C3 as dropped
by the operator. C4 release preparation is complete in
[#732](https://github.com/sixtyops/manager/pull/732) for version 1.4.1-dev6;
the tag awaits the operator's signature after the UI review. C5 step 1 passed;
steps 2 and 3 await the operator.

## Window identity and explicit updates

**Key points:** Firmware window and manual-day stop keys use the scheduler
timezone. An unavailable timezone blocks admission and stop resolution.

**Detail:** Explicit timezone settings take precedence. With `timezone=auto`,
Manager uses the successfully selected and validated scheduler timezone.
Stop resolution uses schedule-local time and keeps the stopped window closed
to firmware writes. Manual-day keys use that timezone across UTC midnight.
See [#828](https://github.com/sixtyops/manager/pull/828),
[#830](https://github.com/sixtyops/manager/pull/830), and
[#841](https://github.com/sixtyops/manager/pull/841).

The single-device endpoint uses the inventory role. A CPE update follows the
CPE path and uses parent AP credentials. Before an explicit update reports
already current, Manager reads live bank data with the resolved credentials.
Only a complete read with no warning and a matching active version permits
the no-op. Both-bank mode requires both versions to match. A failed or
mismatched read follows normal job admission. See
[#837](https://github.com/sixtyops/manager/pull/837),
[#835](https://github.com/sixtyops/manager/pull/835), and
[#840](https://github.com/sixtyops/manager/pull/840).
