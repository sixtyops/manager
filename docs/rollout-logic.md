# Scheduled Rollout Logic

> **Status:** design spec for the rebuilt core. `docs/rearchitecture.md`
> rules 1–24 are the summary. This document is the full definition.
> Lines marked *(today: …)* describe what the current code does instead.

## Summary

**Key points:** The rollout rules below describe the target contract. They do
not claim that every gate is shipped.

**Detail:** Shipped differences are marked *(today: …)* or linked to
[gradual-rollout.md](gradual-rollout.md).

- **A device is an identity, not an IP address.** `device_id` never changes.
  We identify a device by a serial claim that we verify under a named trust
  policy. A MAC is a mutable alias. IP is just an attribute.
- **An address change holds the device.** The system never adopts the newest
  reported IP. The device waits until a trusted check reads its expected
  serial (§1.2).
- **We never delete a device because it went offline.** Offline devices stay
  in the inventory with their history. Only an operator can archive one, and
  the device comes back automatically if we see it again.
- **There are no fixed canary devices.** Firmware is proven by *any* device
  that is healthy on it right now. If that device disappears, the proof
  disappears too.
- **A rollout is a saved list of devices with a state machine.** It is not a
  query that runs again every minute.
- **Waves are 10% → 50% → 100% of that saved list.** Run one wave per
  maintenance window. Any failed gate or device check stops new writes for
  the whole window. Do not resume in that window.
- **A pre-start gate block is a hold, not an outage.** Keep the same wave
  pending for a later window. A named Hold exception uses the same gates and
  cannot bypass a failed gate.
- **Expose one device write and recovery sequence at a time.** Do not start
  another until the current device passes every recovery check.
- **Unknown models are never auto-updated.**

---

## 1. Device identity

### Key points

- `device_id` never changes. Every rollout, confirmation, and history record
  points at a `device_id`, never at an IP.
- Identity is a **serial claim, verified under a named trust policy**. It is
  not absolute hardware proof.
- A MAC is a mutable alias, not identity. A device has **more than one MAC**
  (Ethernet, radio, …). We keep all of them as aliases.
- A row never merges into another row on serial or MAC alone.
- IP, name, model, and the AP a CPE is on are attributes. They can change.

### Detail

*(Target. This section describes the target contract for address recovery
([epic #530](https://github.com/sixtyops/manager/issues/530)). It is not
shipped. Today the poller matches by IP, and jobs and history are keyed by IP.)*

**What identity means.** `device_id` is the row `id`. It never changes.
Identity is a serial claim. The device reports a serial. We accept it only
after a connection that passes a named trust policy (see *Trust policies*
below). A serial read after login proves that the device claims the expected
serial. It does not prove who owned the endpoint before we sent credentials.
It is not absolute hardware proof.

**Identity key.**

| Priority | Source | Works for | Notes |
|---|---|---|---|
| 1 | Serial number from `/cgi.lua/status?type=system` | APs, switches, and CPEs we can log into | A claim, verified under a trust policy. Survives factory reset, new IP, new AP. |
| 2 | Any of the device's MAC addresses | Any CPE listed by its AP | An alias and a candidate only. Used to propose a match, never to prove one. |
| 3 | `legacy-ip:<ip>` (`uid_source = ip_legacy`) | Rows imported from a bridge archive with no serial or MAC | Import only. Never a rollout candidate. It joins a real identity only by the rules under *Provisional rows*. |

`devices` gets two new columns: `uid` (the identity string) and
`uid_source` (`serial` or `mac`). `uid` is unique. Everything else
references the row's `id`.

**Multiple MACs.** A device has several MACs: the Ethernet port, the radio,
and sometimes more. The AP's peer list shows the radio MAC. Direct login
shows the `eth0` MAC *(today: only `eth0` is read, `client.py:394`)*. So one
device can be reported under different MACs by different sources.

- All known MACs are stored in `device_macs (device_id, mac, interface,
  first_seen, last_seen)` as **observations**. MACs are mutable aliases.
  The table has no unique key on `mac`.
- A MAC observation proposes a candidate device. It does not prove identity.
- When we sign into a device we read every interface's MAC and add the ones
  we did not know.
- The same MAC stored under two devices is an **alias conflict**. We log
  it, and **both devices hold**. No write goes to either device while the conflict is open. A duplicate
  serial on two devices is also a conflict, and both devices hold.
- A MAC-only `uid` is the first MAC we saw. It is only a label.

*(today: `devices.ip` is the unique key and the foreign key everywhere.
CPEs are not in `devices`; they are in `cpe_cache`, keyed by
`(ap_ip, ip)`. There is no serial column — issue #254.)*

**Provisional rows.** A CPE is first seen in an AP's peer list, so we only
know its MAC. That row is a **provisional observation**. Later the poller
may sign in and read a serial.

- A provisional observation joins a device only after three checks pass:
  the trust check, the expected-serial check, and the conflict checks.
- Serial alone or MAC alone never merges two rows.
- A destructive merge needs its own tested migration and a safe history
  plan. A conflicting row holds.

**Trust policies.** A trust policy decides whether a connection may carry
credentials and a serial read. There are two. The system names the policy
it used in the event it writes.

- **Mode B (existing credential profile).** Reuse an existing management
  credential profile that the operator already authorized, with bounded
  targets. It needs no new account. Do not ask the operator again for
  access already given. Never fall back to global or AP credentials.
- **Mode A (TLS key pin).** A unique TLS key pin, enforced on the actual
  login and write connection, proves continuity under stated assumptions.
  It is not hardware proof. A shared factory certificate does not qualify.

### 1.2 Address changes

### Key points

- *(Target, not shipped.)* An address change holds the device. The system
  never adopts the newest IP.
- An observed IP is a **candidate**. It becomes the device address only after
  a trusted connection reads the expected serial.
- Direct login does not win. A reused IP does not go to the newest report.
- No blanket network scan. Recovery checks only named candidates.

### Detail

*(today: the poller matches by IP. `devices.ip` is `TEXT NOT NULL UNIQUE`,
so one IP maps to one row. A nullable `devices.ip` is future work.)*

Candidate sources are the AP peer list, switch neighbor data, and syslog.
They are **candidates only**. Inbound syslog does not exist. Switch
`/cgi.lua/discovery` was observed ([#421](https://github.com/sixtyops/manager/issues/421))
but is not implemented. None of these sources proves identity.

Each poll cycle, for each observation:

1. Propose a `device_id` from the serial or from any MAC alias. An
   observation that matches nothing creates a provisional row with
   `status = discovered`. It is never a rollout candidate.
2. Treat the observed IP as a candidate address. Do not change the device
   `ip` yet. The device holds with a visible reason.
3. Connect to the candidate under a trust policy (§1). Read the serial from
   `/cgi.lua/status?type=system`. A direct login does not skip this check.
4. Promote the address only if the serial equals the expected serial and no
   conflict exists. Promotion is one short `BEGIN IMMEDIATE` transaction. It
   uses compare-and-swap on `address_generation`, the expected serial, and
   one active owner per IP. It does no network work. It keeps `device_id`,
   history, credentials, and parent IDs. Record the address in
   `device_addresses (device_id, ip, first_seen, last_seen, source)` and
   close the old address's `last_seen`. Discard every cache built under an
   older generation.
5. Resolve conflicts by holding:
   - **Two devices, one IP (reused IP).** Both devices hold. The newest
     report does not win.
   - **Duplicate serial or duplicate MAC.** Both devices hold.
   - **Serial mismatch at the candidate.** The candidate is rejected. The
     device keeps holding.
   - **One device, two candidate IPs in one cycle.** Neither is promoted
     until a trusted check reads the expected serial on one of them.
6. A CPE that appears under a different AP gets a new `parent_device_id`
   and a `device_rehomed` event. It is still the same device.

A held device stays held until a trusted check passes and no conflict
remains. Automatic recovery turns on one case at a time, each
with its own bench proof. Until a case passes its gate, an address change in
that case holds the device and shows the reason.

**Rollback.** Rolling back any recovery slice disables recovery and holds
the device. It never restores the CPE prune or unsafe IP-keyed execution.

The config-snapshot code already re-links history by MAC when an IP changes
(`poller.py:1109-1140`). With identity in place, that special case goes away.

### 1.3 Offline devices

### Key points

- The poller never deletes a device row.
- Offline devices keep their site, customer label, config history, update
  history, and confirmations. There is no time limit.
- Archiving is a manual, reversible operator action.
- If an archived device is seen again and its serial is verified under a
  trust policy, it is un-archived.

### Detail

*(today: `prune_stale_cpes` deletes a CPE as soon as its AP stops listing
it.)*

- A device missed for one poll cycle becomes `status = offline`.
  `last_seen` is kept.
- The UI may *suggest* archiving devices offline for more than
  `offline_archive_hint_days` (default 90). It never archives by itself.
- **Archive** sets `archived_at`. Archived devices are left out of rollout
  scope and fleet counts. The row stays.
- **Reappearance.** If we see an archived identity again — a verified serial
  (§1) on any AP, any IP, any site, with no conflict — we un-archive it,
  record the new location, and raise a `device_reappeared` event. A MAC
  match alone does not un-archive a device. An unknown MAC does not block
  it. The operator fixes the
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
- A changed certificate puts the device on hold before login. Stop new writes
  for the window; the hold is not an outage.
- The operator accepts a new certificate in one click, per device or per
  site. The manager re-pins by itself when its own firmware update rotated
  the certificate and the serial still matches.

### Detail

Pins are keyed by `device_id`, so an IP change does not trigger a mismatch.
A mismatch sets `status = cert_mismatch`, writes a `cert_changed` event, and
stops all logins to that device. Un-archive needs a serial verified under a
trust policy (§1). A serial alone, read over an unverified connection, is
not proof. If the vendor's
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
- A failed check holds the current wave. The wave does not advance, and the
  same wave can resume only in a later maintenance window.

### 3.1 States

```
create ──► waiting_hold ──hold clears──► active ◄──── resume ────┐
                                           │                     │
           artifact changed / cancel ◄─────┼──────┐              │
                    │                      │      │              │
                    ▼               all members   │ a wave     paused
                cancelled           terminal      │ fails
                                           ▼      ▼
                                   completed
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

**Key points:** A failed gate stops new writes for the whole window. An
unknown, stale, or conflicting topology never makes a parent schedulable.

**Detail:**

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
| 10 | the wave has at least one safe, schedulable unit (§4) | `nothing_schedulable` |

The **window instance key** is `(schedule_id, window_start_utc)`. Two
windows on one day, or one window across midnight, are different keys.
*(today: the key is `YYYY-MM-DD`.)* The key, the batch assignment, and the
per-wave settings snapshot are written in **one transaction** when the wave
job starts. If any gate or device check fails, record a hold/failure for the
window and do not re-evaluate it to start writes again in that window. A
topology-blocked parent is never a safe, schedulable unit. It cannot trigger a
final-wave fallback or phase advance.

The UI's "next attempt" prediction calls the same function with a future
`now`. It has no rules of its own.

### 3.5 Advancing and completing

**Key points:** Advance only after required checks pass. A held unit keeps the
same wave pending for a later window.

**Detail:**

- A wave advances `phase` only after its required units pass every check.
- Any failed check holds the same wave for a later window. It does not advance
  past the held unit or start another unit in the failed window.
- The rollout completes when every member is terminal.
- A pre-start block keeps the member and wave pending. The rollout does not
  complete while a member is held. It can retry in a later maintenance
  window, after the failed check is clear.

---

## 4. Wave construction

### Key points

- We count **units**: an AP with its CPEs, a standalone CPE, or a switch.
- Wave sizes are cumulative against the saved member count: 10%, 50%, 100%.
- A held unit stays in its current wave and is retried first in a later window.
- A switch is never flashed in the same batch as an AP it powers.
- Any failed check stops new writes for the whole window. A pre-start block is
  a hold, not an outage.
- Only one device write and recovery sequence can be active at a time.

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

Select the next unit in this order:

1. held units from the current wave, oldest first;
2. remaining pending units, by `device_id`.

Run units in order as CPEs → APs → switches. Process each unit's full write
and recovery sequence before starting another. The target has no concurrent
device writes; a pacing setting cannot raise this limit.

**Switch rule.** A switch can run only if every AP it powers is either not
a member or already `succeeded` in an earlier wave. "Powers" comes from
the switch-port topology seen in the last 24 hours.

- Unknown, stale, or conflicting topology blocks the automatic switch write
  in every wave. It does not permit a final-wave fallback.
- A switch with known topology is eligible only after every AP it powers has
  passed all checks in an earlier wave. It cannot run in the same wave.

*(Today the scheduler uses the final-wave fallback for unknown topology. This
target rule is not shipped.)*

### 4.4 Checks before and during a device sequence

**Key points:** Commit the snapshot before the pre-update reboot and each
device write. Any failed check stops new writes for the window.

**Detail:**

For each device, the target sequence is: reach → backup and config snapshot →
pre-update reboot and recovery proof → **upload** → install → reboot → verify
→ smoke test. Commit the snapshot before the pre-update reboot and before any
device write.

**Any failed check stops new writes for the whole window.** A failed reach,
identity, artifact, backup, snapshot, pre-update reboot, install, recovery, or
smoke check cannot defer one unit while the rollout advances to another.
Record a pre-start block as `deferred` with a hold reason, or a post-start
failure as `failed`. Keep the rollout paused and its wave pending. Retry only
in a later maintenance window after the check passes.

**A block before a device write is a hold, not an outage.** Examples include
`unreachable`, `no_address`, `auth`, `cert_mismatch`, missing artifact, or a
snapshot that cannot be committed. Do not reboot or flash that device. Stop
new writes for this window.

If the pre-update reboot or a later device action causes a loss of service,
record a failure and stop the window. Let the current flash finish. Do not
start another device sequence until the current device passes every recovery
check. If it does not pass, no next write starts in that window.

*(Today a login failure before the reboot is recorded as `failed` and pauses
the rollout. `deferred` means the window ran out. Snapshot failure is
non-fatal. These shipped behaviors do not satisfy the target.)*

### 4.5 Halt

**Key points:** A failed check stops the maintenance window. Resume is only
available in a later window.

**Detail:**

On the first failed gate or device check:

1. Stop all new writes for the maintenance window.
2. Do not interrupt a flash already in progress. Finish its device sequence
   and recovery checks before any later write.
3. Set the rollout to `paused` with the hold/failure reason and affected
   device id.
4. Void this wave's confirmations (§3.3) if the wave failed.
5. Send Slack and email.

**Resume** is an operator action for a later maintenance window. It re-runs
the *same wave* after the failed check passes. The failed window stays
stopped. This target behavior is not a claim that the shipped engine enforces
the whole-window stop.

### 4.6 Window cutoff

**Key points:** Do not start a new device sequence after the cutoff. Finish
the active sequence and its recovery checks.

**Detail:**

A device not yet started when the per-device cutoff passes stays pending in
its current wave for a later window. A device already in a write sequence
runs to the end of its recovery checks, even after the window closes.

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
| Device changes IP (*target, not shipped*) | A mutating request that uses an old or stale address must carry the expected `device_id` and serial or `address_generation`. Before it admits a job, the engine verifies the device serial under the trust policy. It rejects a stale or mismatched binding. A serial mismatch holds the device and halts the window. A queued job binds `device_id`, serial, and `address_generation` at admission. An active job revalidates the serial before each write and writes only with that immutable ID and generation. A null or stale address holds the device and halts the window. The window never resumes automatically. A job never retargets silently. There is no second flash: it never repeats an upload or install. Restart recovery stays read-only until the normal engine gates allow the next step. A missed recovery deadline halts the window. *(today: the job reads the IP at flash time.)* |
| Device loses its IP (`address_unknown`) | Hold the device and stop new writes for the window. Keep the same wave pending. This is not an outage. |
| CPE moves to another AP | Nothing at the member level. Units follow the current AP. |
| Device goes offline | Hold the device and stop new writes for the window. Keep the same wave pending. This is not an outage. |
| Device updated some other way (device UI, or manual "update now") | At selection time it no longer needs the update → `skipped_current`. Terminal. Counts toward the target. |
| Device disabled | `disabled`. Terminal. Removed from `N`. |
| Device archived | `removed (archived)`. Terminal. Removed from `N`. Row kept. |
| Device leaves scope (site excluded) | `removed (scope)`. Terminal. |
| Device's model is corrected to a family with no target | `removed (no_target)`. |
| Selected artifact changes for a family **with members** | Rollout `cancelled (artifact_changed)`. The next rollout uses the new artifact and its Hold. It cannot start a write in a window already stopped by a failed check. |
| Selected artifact changes for a family with no members | Nothing. |
| Artifact file missing or checksum wrong | Gate returns `artifact_missing`. Hold the wave for the rest of the window. Do not retry to start a write in that window. Notify the operator. |
| Auto-update switched off | Rollout `paused (schedule_disabled)`. Switching it on does not start writes again in a stopped window. Resume in a later eligible window. The Firmware Hold is not re-checked. |
| Operator pauses or cancels | Pause stops new writes for the window; resume in a later window. Cancel keeps member rows for history. |
| Manual "update now" on a member | Target: same safety gates as scheduled work. A named Hold exception is recorded and cannot bypass a failed gate. A clean proof records the exception and result. Today manual routes bypass the Hold; see [gradual-rollout.md](gradual-rollout.md). |
| Manual "update now" on a non-member | Target: same gates and explicit Hold exception rules. A clean proof records the exception and result. Today manual routes bypass the Hold; see [gradual-rollout.md](gradual-rollout.md). |
| A unit was held by a failed check | Retry it first in the same wave during a later maintenance window, after the check passes. The failed window stays stopped. |
| Only topology-blocked switches remain | Keep the switches held. Unknown, stale, or conflicting topology never permits an automatic parent write or final-wave fallback. Do not advance past a held member. |

---

## 6. Worked examples

**Key points:** Stop at the first failed check. Retry the same wave only in a
later maintenance window.

**Detail:**

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

**First 10% wave runs. The third AP fails its smoke check after two APs
passed.** The window stops at that first failure. The two confirmations from
the incomplete wave are voided. Operator investigates and resumes in a later
window after the check passes. The same wave continues. The *next* rollout to
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
device_macs          device_id, mac, interface, first_seen, last_seen
                     (no unique key on mac; duplicates are alias conflicts)
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
3. Two waves never start in one window instance. A failed wave cannot resume
   in that window, including after restart.
4. A `waiting_hold` rollout never runs a wave while any member family is
   held. A family with no members never holds a rollout.
5. Proof is live. A confirmation whose device is offline, errored,
   archived, or on another version does not clear a hold.
6. Confirmations from a paused wave are voided.
7. A manual update of a CPE, or of an out-of-scope device, can clear its
   family's hold.
8. Wave sizes reach `ceil(0.1N)`, `ceil(0.5N)`, `N`. Held units do not shrink
   `N` or let the wave advance.
9. Any failed gate or device check stops new writes for the whole window.
   A pre-start block is a hold, not an outage. Resume is only in a later
   window. A snapshot passes before the pre-update reboot and every write.
10. A switch is never in the same batch as an AP it powers. Unknown, stale,
    or conflicting topology blocks an automatic switch update.
11. `unknown` family devices never appear in a rollout.
12. Changing the artifact for a family with members cancels the rollout.
    For a family with no members it does nothing.
13. Polling never deletes a device row. An archived identity whose serial
    is verified under a trust policy is un-archived.
15. A device reported under two different MACs (radio by the AP, Ethernet
    by direct login) keeps one `device_id`. Both MACs are aliases. The same
    MAC under two devices holds both.
16. An address change holds the device. No address is adopted from the
    newest report. A reused IP, a duplicate serial, or a duplicate MAC
    holds both devices.
17. A queued job binds device ID, serial, and address generation at
    admission. A mutating request to a stale or old address must carry the
    expected identity. A serial mismatch holds and halts the window. An
    active job revalidates the serial before each write. A
    null or stale address holds and halts the window. No upload or install
    repeats.
14. Rollout, confirmation, and history records survive an IP change and an
    AP move.
