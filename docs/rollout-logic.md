# Scheduled Rollout Logic

> **Status:** design spec for the rebuilt core. `docs/rearchitecture.md`
> rules 1–24 are the summary. This document is the full definition.
> Lines marked *(today: …)* describe what the current code does instead.

## Summary

**Key points:** The rollout rules below describe the target contract. They do
not claim that every gate is shipped.

**Detail:** Shipped differences are marked *(today: …)* or linked to
[gradual-rollout.md](gradual-rollout.md).

- **A device is an identity, not an IP address.** We identify devices by
  serial number, or by MAC when we cannot log in. IP is just an attribute.
- **We never delete a device because it went offline.** Offline devices stay
  in the inventory with their history. Only an operator can archive one, and
  the device comes back automatically if we see it again.
- **There are no fixed canary devices.** Firmware is proven by *any* device
  that is healthy on it right now. If that device disappears, the proof
  disappears too.
- **A rollout is a saved list of devices with a state machine.** It is not a
  query that runs again every minute.
- **Waves are 10% → 50% → 100% of that saved list.** One wave per
  maintenance window. A failure after the firmware upload starts pauses the
  rollout. A problem before the upload starts only defers that device.
- **A failed check stops the whole maintenance window.** A pre-start block is
  a hold, not an outage. The operator cannot resume a failed rollout in the
  same window. A named Hold exception uses the same gates and cannot bypass a
  failed gate.
- **Unknown models are never auto-updated.**

---

## 1. Device identity

### Key points

- Serial number is the identity. MAC is the fallback.
- A device has **more than one MAC** (Ethernet, radio, …). Any of them can
  identify it. We keep all of them.
- IP, name, model, and the AP a CPE is on are attributes. They can change.
- Every rollout, confirmation, and history record points at a `device_id`,
  never at an IP.

### Detail

**Identity key.**

| Priority | Source | Works for | Notes |
|---|---|---|---|
| 1 | Serial number from the device's `/system` info | APs, switches, and CPEs we can log into | Survives factory reset, new IP, new AP. |
| 2 | Any of the device's MAC addresses | Any CPE listed by its AP | Used when we cannot sign into the CPE. |
| 3 | `legacy-ip:<ip>` (`uid_source = ip_legacy`) | Rows imported from a bridge archive with no serial or MAC | Import only. Never a rollout candidate. Merged into the real identity on first serial/MAC observation. |

`devices` gets two new columns: `uid` (the identity string) and
`uid_source` (`serial` or `mac`). `uid` is unique. Everything else
references the row's `id`.

**Multiple MACs.** A device has several MACs: the Ethernet port, the radio,
and sometimes more. The AP's peer list shows the radio MAC. Direct login
shows the `eth0` MAC *(today: only `eth0` is read, `client.py:394`)*. So one
device can be reported under different MACs by different sources.

- All known MACs are stored in `device_macs (device_id, mac, interface,
  first_seen, last_seen)`. `mac` is unique across the table.
- An observation matches a device if **any** of its MACs match.
- When we sign into a device we read every interface's MAC and add the ones
  we did not know.
- If a MAC we see already belongs to a *different* device row, and the
  serials differ, that is a conflict. We log it and do not merge. If one
  side has no serial (MAC-only row), the rows merge as described below.
- A MAC-only `uid` is the first MAC we saw. It is only a label; matching
  always goes through `device_macs`.

*(today: `devices.ip` is the unique key and the foreign key everywhere.
CPEs are not in `devices`; they are in `cpe_cache`, keyed by
`(ap_ip, ip)`. There is no serial column — issue #254.)*

**Upgrading from MAC to serial.** A CPE is first seen in an AP's peer list,
so we only know its MAC. Later the poller signs in and reads its serial.
The row's `uid` becomes the serial. The MAC stays as an attribute.

If that serial, or any MAC read at login, already belongs to another row
(for example an archived device, or a MAC-only row made from the peer list),
the two rows are **merged**. The older `id` is kept. The newer row's history
and MACs move to it. We write a `device_merged` event.

### 1.2 Address changes

### Key points

- Two sources report addresses: the AP lists its SMs, and an SM tells us its
  own IP and its AP.
- We match each report to a device first, then update the IP.
- A device that changes IP during a rollout is not affected. The job reads
  the IP when it flashes the device.

### Detail

Each poll cycle:

1. Match every observation to a `device_id` by serial, then by any MAC in
   `device_macs`. An observation that matches nothing creates a new row with
   `status = discovered`.
2. Set the device's `ip` to the observed address. Record it in
   `device_addresses (device_id, ip, first_seen, last_seen, source)`. Close
   the old address's `last_seen`. Do not delete anything.
3. Resolve conflicts:
   - **One device, two IPs in one cycle.** The direct-login report wins.
   - **Two devices, one IP.** The newest report keeps the IP. The other
     device gets `ip = NULL` and `status = address_unknown`. The rollout
     treats it as unreachable (see §4.4).
4. A CPE that appears under a different AP gets a new `parent_device_id`
   and a `device_rehomed` event. It is still the same device.

The config-snapshot code already re-links history by MAC when an IP changes
(`poller.py:1109-1140`). With identity in place, that special case goes away.

### 1.3 Offline devices

### Key points

- The poller never deletes a device row.
- Offline devices keep their site, customer label, config history, update
  history, and confirmations. There is no time limit.
- Archiving is a manual, reversible operator action.
- If an archived device is seen again anywhere, it is un-archived.

### Detail

*(today: `prune_stale_cpes` deletes a CPE as soon as its AP stops listing
it.)*

- A device missed for one poll cycle becomes `status = offline`.
  `last_seen` is kept.
- The UI may *suggest* archiving devices offline for more than
  `offline_archive_hint_days` (default 90). It never archives by itself.
- **Archive** sets `archived_at`. Archived devices are left out of rollout
  scope and fleet counts. The row stays.
- **Reappearance.** If we see an archived identity again — same serial or
  MAC, on any AP, any IP, any site — we un-archive it, record the new
  location, and raise a `device_reappeared` event. The operator fixes the
  customer label. This is the "repurposed device" case: the identity follows
  the hardware, the labels follow the operator.
- **Replacement hardware** at the same IP and name has a new serial and MAC.
  It is a new device. The old one stays offline until archived. We never
  guess identity from an IP or a name.

### 1.4 TLS pinning

### Key points

- Devices use self-signed certificates. We cannot verify them against a CA.
- We record each device's certificate on first login and refuse to send the
  password if it changes.
- A changed certificate defers the device. It never fails it.
- The operator accepts a new certificate in one click, per device or per
  site. The manager re-pins by itself when its own firmware update rotated
  the certificate and the serial still matches.

### Detail

Pins are keyed by `device_id`, so an IP change does not trigger a mismatch.
A mismatch sets `status = cert_mismatch`, writes a `cert_changed` event, and
stops all logins to that device. Merge and un-archive need a serial **and**
a known MAC or a matching pin; a serial alone is not proof. If the vendor's
firmware regenerates certificates, an admin can turn pinning off; the UI
then shows a permanent warning.

### 1.5 What uses `device_id`

`rollout_members`, `firmware_confirmations`, `device_update_history`,
`device_configs`, `device_durations`, job device rows, and `switch_ports`.
*(today: all keyed by IP.)*

---

## 2. Firmware families and artifacts

### Key points

- A device's family comes from its model: `tna-30x`, `tna-303l`, `tns-100`.
- Any other model is family `unknown`. It is never auto-updated. A manual
  "update now" on it is allowed only if the device's own firmware validation
  accepts the file (`validate_firmware_for_model`).
- Firmware files are **artifacts** with a checksum. Rollouts point at an
  artifact id, not a filename.
- A family with no selected artifact has no target. Its devices are left
  out of rollouts. They are not "on hold".

### Detail

*(today: an unknown model maps to `tna-30x`, which could flash the wrong
image.)*

`firmware_artifacts (id, family, version, sha256, release_date, path,
verified_at)`. Settings selects one artifact per family. A rollout stores
the artifact id for each family it is rolling. Filenames are for display.

---

## 3. Rollout lifecycle

### Key points

- States: `waiting_hold` → `active` → `completed`. Side states: `paused`,
  `cancelled`.
- Only one rollout can be open at a time.
- Members are saved when the rollout is created. Offline devices that need
  the update are members too.
- The rollout goes `active` only when no member family is held.
- A rollout that ends with deferred devices completes anyway. The next
  rollout picks those devices up.

### 3.1 States

```
create ──► waiting_hold ──hold clears──► active ◄──── resume ────┐
                                           │                     │
           artifact changed / cancel ◄─────┼──────┐              │
                    │                      │      │              │
                    ▼               all members   │ a wave     paused
                cancelled           terminal      │ fails
                                           ▼      ▼
                                   completed (or completed_with_deferred)
```

A DB unique index on `status IN (waiting_hold, active, paused)` enforces
"one open rollout". `phase ∈ {pct10, pct50, pct100}` exists only while
`active` or `paused`.

### 3.2 Creation

Each scheduler tick, if no rollout is open and auto-update is on, build the
**candidate set**. A device is a candidate when it is:

- not archived, enabled, and in scope;
- in a known family that has a target artifact;
- **action-eligible**: installed version ≠ target version. If the installed
  version is newer, downgrades must be allowed.

If the set is not empty, create the rollout and save **every candidate** as
a member with `wave = NULL, status = pending`. Offline candidates are saved
too. They are behind, and we flash them when they come back.

*(today: `rollout_devices` rows are only written when a device is put in a
wave, so membership is whatever the query returns on that tick.)*

The rollout also stores: one artifact id per family that has members,
`member_count`, a settings snapshot for the log, and the schedule id.

### 3.3 The Firmware Hold — no fixed canaries

### Key points

- A family is **held** until its hold days pass, or until any device is
  healthy on that firmware right now.
- There is no chosen canary. Proof is whatever is healthy at this tick.
- If the only proving device goes away before the rollout starts, the family
  is held again.
- Once the rollout is `active`, the hold is not checked again.
- Proof from a wave that paused is thrown away.

### Detail

The rollout moves from `waiting_hold` to `active` when **no family with
members** is held. A family with a selected artifact but zero members is
ignored. *(today: every family with a selected file is checked, so a family
with nothing to update can block the wave.)*

A family is held while both are true:

- `now < artifact.release_date + firmware_canary_hold_days`, and
- there is no **live proof** for that artifact.

**Live proof** is a `firmware_confirmations` row `(device_id, artifact_id,
confirmed_at, source, voided_at IS NULL)` whose device is, at this tick, on
that version, has no `last_error`, is not archived, and was seen within
24 hours. Any device of the family counts: AP, switch, or CPE; in scope or
not; updated by a wave or by hand. *(today: only in-scope APs and switches
count. CPEs never count.)*

What this means in practice:

- **Proving device disconnects.** While the rollout is `waiting_hold`, the
  family is held again on the next tick. The operator confirms another
  device or waits out the hold days. Once `active`, it does not matter: the
  10% wave is now the canary, and halt-on-failure protects it.
- **A wave pauses.** Every confirmation from that wave gets `voided_at`.
  A half-failed wave cannot clear the hold for the next rollout.
- **Nobody confirms anything.** The hold days run out and the 10% wave runs
  as the canary. The minimum is 6 days.

The hold is checked every tick. A rollout can go `waiting_hold` → `active`
and run its first wave in the same window, if enough of the window is left.

### 3.4 Wave gate

`evaluate_wave_start(rollout, now)` runs these checks in order. It returns
the first failing reason, or `ok`.

| # | Check | Reason code |
|---|---|---|
| 1 | rollout is `active` | `status_<x>` |
| 2 | no other job is running | `job_in_flight` |
| 3 | no wave has run in this window instance | `already_ran_this_window` |
| 4 | not inside a freeze window | `frozen` |
| 5 | NTP drift is within tolerance (NTP unreachable → allow, log it) | `clock_invalid` |
| 6 | inside a maintenance window | `outside_window` |
| 7 | more than 15 minutes of window left | `window_ending` |
| 8 | weather guard passes (checked once per window) | `blocked_weather` |
| 9 | every artifact this wave needs is on disk with a matching sha256 | `artifact_missing` |
| 10 | the wave has at least one unit to run (§4) | `nothing_schedulable` |

The **window instance key** is `(schedule_id, window_start_utc)`. Two
windows on one day, or one window across midnight, are different keys.
*(today: the key is `YYYY-MM-DD`.)* The key, the batch assignment, and the
per-wave settings snapshot are written in **one transaction** when the wave
job starts. A `nothing_schedulable` result writes nothing, so the window is
not consumed.

The UI's "next attempt" prediction calls the same function with a future
`now`. It has no rules of its own.

### 3.5 Advancing and completing

- A wave that finishes with no failures advances `phase`.
- The rollout completes when every member is terminal.
- If members are still `deferred` after the `pct100` wave, the rollout
  completes as `completed_with_deferred`. Those devices are still behind, so
  the next tick creates a new rollout with them. The hold is already clear
  (by time or proof), so they retry one window later. A rollout never stays
  open because one customer is dark.

---

## 4. Wave construction

### Key points

- We count **units**: an AP with its CPEs, a standalone CPE, or a switch.
- Wave sizes are cumulative against the saved member count: 10%, 50%, 100%.
- Deferred units go first in the next wave.
- A switch is never flashed in the same batch as an AP it powers.
- Problems before the firmware upload defer the device. Problems after it
  pause the rollout.

### 4.1 Units

- **AP unit** = the AP plus every CPE member currently on it.
- **Standalone CPE unit** = a CPE member whose AP is not a member, or is
  already done.
- **Switch unit** = one switch.

Units are built when the wave is selected, from current associations. A CPE
that moved to another AP since creation is grouped with its new AP.

### 4.2 Sizing

`N` = the number of **units** (§4.1) built from members not in `removed` or
`disabled`. Stragglers count once added. One AP with nine CPE members is one
unit, not ten. Units are rebuilt each tick, so `N` can change when a CPE
re-homes; targets are recomputed from the current `N`.

- `pct10` target = `ceil(0.10 × N)`
- `pct50` target = `ceil(0.50 × N)`
- `pct100` target = `N`

A wave's batch is filled until *(units already succeeded + units in this
batch)* reaches the target. Minimum batch is 1 unit. Deferred units are not
"done", and they do not shrink `N`. They just retry.

N = 3 units gives waves of 1, 1, 1. N = 100 units gives 10, 40, 50.
*(today: 10, 45, 45, because each percentage is taken of what is left.)*

### 4.3 Order and topology

**Key points:** Unknown, stale, or conflicting topology blocks an automatic
switch update. The final wave is not a fallback.

**Detail:**

Fill the batch in this order:

1. units that were `deferred` before, oldest first;
2. remaining pending units, by `device_id`.

Run the batch as CPEs → APs → switches. `parallel_updates` is a concurrency
cap. It never changes the batch size.

**Switch rule.** A switch can run only if every AP it powers is either not
a member or already `succeeded` in an earlier wave. "Powers" comes from
the switch-port topology seen in the last 24 hours.

- No topology, or stale topology → the switch waits for the `pct100` wave.
- In `pct100`, if any powered AP is still not `succeeded` → the switch is
  `deferred`.
- Unknown, stale, or conflicting topology blocks an automatic parent update.
  It does not permit a final-wave fallback.

*(Today the scheduler uses the final-wave fallback for unknown topology. This
target rule is not shipped.)*

### 4.4 Deferred vs failed — the point of no return

Per device, the job runs: reach → snapshot → pre-update reboot → **upload**
→ install → reboot → verify → smoke test.

**Before upload starts → `deferred`.** Reasons: `unreachable`,
`no_address` (ip is NULL), `auth` (cannot sign in), `cert_mismatch` (the
device's TLS certificate changed and no one has accepted it yet),
`snapshot_failed`, `prereboot_unreachable` (we never reached it to reboot). Deferred devices
keep their wave, do not pause the rollout, and go first next wave.

**Exception:** a device we **did** reboot and that did not come back is
`failed`. We caused that outage.

**From upload onward → `failed`** unless the smoke test passes.

*(today: a login failure before the reboot is recorded as `failed` and
pauses the rollout. `deferred` only means the window ran out.)*

### 4.5 Halt

**Key points:** A failed check stops the maintenance window. Resume is only
available in a later window.

**Detail:**

On the first `failed` device:

1. Stop launching new devices in this job.
2. Let devices already past upload finish. Stopping a flash midway is worse.
3. End the job. Set the rollout to `paused` with a reason and the failed
   device ids.
4. Void this wave's confirmations (§3.3).
5. Send Slack and email.

**Resume** is an operator action for a later maintenance window. It re-runs
the *same wave*: failed devices go back to `pending` and retry with anything
not yet launched. The failed window stays stopped. This target behavior is
not a claim that the shipped engine enforces the whole-window stop.

### 4.6 Window cutoff

A device not yet launched when the per-device cutoff passes is
`deferred (window_cutoff)`. A device already past upload runs to the end,
even after the window closes.

### 4.7 Restart recovery

If the manager restarts during a job, every device that was in flight is
re-checked at startup. On target version, reachable, smoke test passes →
`succeeded`. Anything else → `failed`, and the rollout pauses with reason
`recovered_after_restart`. Nothing is re-flashed without a human.

---

## 5. Changes while a rollout is open

**Key points:** Manual updates use the target safety gates. A named Hold
exception cannot bypass a failed gate.

**Detail:** Shipped manual routes still bypass the Hold, as described in
[gradual-rollout.md](gradual-rollout.md).

| Event | What happens |
|---|---|
| Device added (or un-archived) and it needs the update | Added as a **straggler**: `wave = pct100`, `status = pending`. `N` grows. **Exception:** if its family had no members at creation, the hold was never checked for that family, so it is *not* added. It waits for the next rollout. Nothing is added after the `pct100` wave has started. |
| Device changes IP | Nothing. The job reads the IP at flash time. |
| Device loses its IP (`address_unknown`) | Deferred when its turn comes. |
| CPE moves to another AP | Nothing at the member level. Units follow the current AP. |
| Device goes offline | Deferred when its turn comes. Still a member. |
| Device updated some other way (device UI, or manual "update now") | At selection time it no longer needs the update → `skipped_current`. Terminal. Counts toward the target. |
| Device disabled | `disabled`. Terminal. Removed from `N`. |
| Device archived | `removed (archived)`. Terminal. Removed from `N`. Row kept. |
| Device leaves scope (site excluded) | `removed (scope)`. Terminal. |
| Device's model is corrected to a family with no target | `removed (no_target)`. |
| Selected artifact changes for a family **with members** | Rollout `cancelled (artifact_changed)`. Next tick creates a new one. The hold is checked against the new artifact. |
| Selected artifact changes for a family with no members | Nothing. |
| Artifact file missing or checksum wrong | Gate returns `artifact_missing`. Rollout stays `active` and retries each tick. Operator is notified once. |
| Auto-update switched off | Rollout `paused (schedule_disabled)`. Switching it on resumes it. The hold is not re-checked. |
| Operator pauses or cancels | As named. Cancel keeps member rows for history. |
| Manual "update now" on a member | Target: same safety gates as scheduled work. A named Hold exception is recorded and cannot bypass a failed gate. A clean proof records the exception and result. Today manual routes bypass the Hold; see [gradual-rollout.md](gradual-rollout.md). |
| Manual "update now" on a non-member | Target: same gates and explicit Hold exception rules. A clean proof records the exception and result. Today manual routes bypass the Hold; see [gradual-rollout.md](gradual-rollout.md). |
| Every remaining unit was deferred last wave | They are attempted again. Deferred units go first. The engine never assumes a device is still unreachable. |
| No unit can be launched for a reason other than reachability (no pending member, or only topology-blocked switches) | Gate returns `nothing_schedulable`. The window is **not** used up. `phase` advances only if no safety gate failed. Unknown or conflicting topology blocks the affected switch. |

---

## 6. Worked examples

**Roll v1.9 to 200 TNA-30x APs and 30 TNS-100 switches.**
Operator selects both artifacts. Rollout is created with 230 members,
`waiting_hold` on both families. Operator clicks "update now" on one AP
and one switch. Both pass smoke → two confirmations. Next tick: both
families have proof → `active`. The first wave runs that night if a window
is open.

**Same, but the confirming AP loses power the next day, before the first
wave.** Its proof is gone. `tna-30x` is held again. `tns-100` is still
proven. The rollout stays `waiting_hold` because holds are all-or-nothing.
The operator confirms another AP or waits out the hold days.

**First 10% wave runs. 3 of 20 APs fail smoke.** Rollout pauses. The 17
good confirmations are voided. The window stops. Operator investigates and
resumes in a later window. The same wave retries the 3. The *next* rollout to
this artifact needs fresh proof or elapsed days.

**TNA-303L is CPE-only.** The operator updates one customer's SM by hand.
It passes smoke → proof for `tna-303l`. *(today: impossible; CPE proof is
ignored.)*

**Nobody confirms anything.** After `firmware_canary_hold_days` the hold
runs out. The 10% wave runs as the canary, under halt-on-failure.

---

## 7. Schema sketch

```
devices              id, uid, uid_source, role, model, family,
                     ip (nullable), parent_device_id, tower_site_id,
                     system_name, customer_label, notes, enabled,
                     status, first_seen, last_seen, archived_at,
                     firmware_version, bank1_version, bank2_version, active_bank,
                     last_error, username, password
device_addresses     device_id, ip, first_seen, last_seen, source
device_macs          device_id, mac (unique), interface, first_seen, last_seen
device_events        device_id, kind (discovered|offline|online|rehomed|
                     reappeared|merged|archived|unarchived), at, detail
firmware_artifacts   id, family, version, sha256, release_date, path, verified_at
rollouts             id, status, phase, pause_reason, member_count,
                     artifact_30x_id, artifact_303l_id, artifact_tns100_id,
                     schedule_id, last_window_key, created_at, completed_at
rollout_members      rollout_id, device_id, family, role, wave (nullable),
                     status (pending|deferred|in_flight|succeeded|failed|
                     skipped_current|disabled|removed), reason, is_straggler,
                     job_id, updated_at
firmware_confirmations
                     id, device_id, artifact_id, confirmed_at, source
                     (wave|manual), job_id, voided_at
switch_ports         switch_device_id, port, powered_device_id, observed_at
```

Dropped: `cpe_cache` (moves into `devices` plus a `link_metrics` table for
RSSI and telemetry), `access_points`, `switches`, `rollout_devices`,
`canary_completed_at`.

---

## 8. Invariants to pin in tests

1. Only one rollout is open at a time.
2. Members are saved at creation. A device that becomes eligible later is
   only added as a straggler with `wave = pct100`, and only if its family
   had members at creation.
3. Two waves never start in one window instance. This holds across a
   restart and across a resumed wave.
4. A `waiting_hold` rollout never runs a wave while any member family is
   held. A family with no members never holds a rollout.
5. Proof is live. A confirmation whose device is offline, errored,
   archived, or on another version does not clear a hold.
6. Confirmations from a paused wave are voided.
7. A manual update of a CPE, or of an out-of-scope device, can clear its
   family's hold.
8. Wave sizes reach `ceil(0.1N)`, `ceil(0.5N)`, `N`. Deferred units do not
   shrink `N`.
9. Failures before upload defer. Failures after upload pause. A failed
   pre-update reboot pauses.
10. A switch is never in the same batch as an AP it powers. Unknown, stale,
    or conflicting topology blocks an automatic switch update.
11. `unknown` family devices never appear in a rollout.
12. Changing the artifact for a family with members cancels the rollout.
    For a family with no members it does nothing.
13. Polling never deletes a device row. An archived identity that is seen
    again is un-archived.
15. A device reported under two different MACs (radio by the AP, Ethernet
    by direct login) is one device row, not two.
14. Rollout, confirmation, and history records survive an IP change and an
    AP move.
