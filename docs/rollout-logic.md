# Scheduled Rollout Logic

> **Status:** The serial queue is shipped. This document separates shipped queue behavior from target identity and safety rules. See [gradual-rollout.md](gradual-rollout.md) for operator-facing behavior and [issue #604](https://github.com/sixtyops/manager/issues/604) for proof status.

## Summary

**Key points:** This document defines the serial firmware queue shipped in
[PR #715](https://github.com/sixtyops/manager/pull/715). Scheduled admission
stays closed until required safety proof passes.

**Detail:** The shipped queue uses Manager inventory and the device APIs it calls. It runs one ordered list of devices that need the selected firmware. It does not persist membership or calculate percentages.

- Keep device identity, checked address changes, and offline history as
  described below.
- Keep Firmware Hold by firmware family. Operators set `firmware_window_ramp_minutes` (30-120) in settings for the first used maintenance windows for each new firmware family or artifact; later windows use the normal schedule. Scheduler status shows the persisted cutoff and ramp window ordinal.
- Run CPEs, then APs, then switches. Process one device write and recovery
  sequence at a time.
- Persist the first failure and the maintenance-window cutoff. Stop new starts
  at the cutoff, finish active recovery, and continue remaining devices in a
  later window.
- New and returning devices join the derived queue when they need the target.
- Default Update Mode to one bank; the operator can select both banks. Manual per-device updates retain their
  deliberate Firmware Hold bypass and do not advance scheduled timing.
- Stop the queue after a failed check. Resolve the failure before work resumes
  in a later window. Never reopen the failed window.

The target remains closed to scheduled admission until its required proof
passes. See [issue #695](https://github.com/sixtyops/manager/issues/695) for the
ordered PR plan.

The shipped queue ignores legacy wave rollouts during admission. Legacy wave
rows remain in history and are hidden from current scheduler status. Startup
migration resolves duplicate legacy CPE rows by mapping newer observations to
the existing migrated CPE; AP and switch IP conflicts remain errors.

## 1. Device identity

### Key points

- `device_id` never changes. Every rollout, confirmation, and history record
  points at a `device_id`, never at an IP.
- Recovery confirms identity with an authenticated serial read. This is a
  device claim, not cryptographic hardware proof.
- A MAC is a mutable alias, not identity. A device has **more than one MAC**
  (Ethernet, radio, …). We keep all of them as aliases.
- A row never merges into another row on serial or MAC alone.
- IP, name, model, and the AP a CPE is on are attributes. They can change.

### Detail

*(Target. This section describes the target contract for address recovery
([epic #530](https://github.com/sixtyops/manager/issues/530)). It is not
shipped. Today the poller matches by IP, and jobs and history are keyed by IP.)*

**What identity means.** `device_id` is the row `id`. It never changes.
Identity is a serial claim. The device reports a serial after Manager
authenticates with its existing management credentials. A matching serial
confirms the device for update recovery. It does not provide cryptographic
hardware proof.

**Identity key.**

| Priority | Source | Works for | Notes |
|---|---|---|---|
| 1 | Serial number from `/cgi.lua/status?type=system` | APs, switches, and CPEs we can log into | A claim read after management authentication. Survives factory reset, new IP, new AP. |
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

- Update recovery accepts a candidate only after management authentication,
  expected-serial confirmation, and conflict checks.
- Serial alone or MAC alone never merges two rows.
- A destructive merge needs its own tested migration and a safe history
  plan. A conflicting row holds.

**Management authentication.** Manager uses the same credential pair already
selected for the update. It uses the existing device management
authentication model. Manager does not pin device certificates. The broader
credential-profile and certificate-pinning proposals are not planned.

### 1.2 Address changes

### Key points

- **Shipped in [PR #706](https://github.com/sixtyops/manager/pull/706):** CPE
  recovery checks AP peer reports after an address change. The saved radio
  MAC selects a candidate; authenticated serial confirms it.
- **Shipped in [PR #792](https://github.com/sixtyops/manager/pull/792):** polling
  records authenticated serial identity.
- **Shipped in [PR #788](https://github.com/sixtyops/manager/pull/788):** a
  checked transaction moves an AP record to its confirmed address.
- **Shipped in [PR #787](https://github.com/sixtyops/manager/pull/787):** a CPE
  reads its connected APs, and an AP reports its radio MAC.
- **Shipped in [PR #822](https://github.com/sixtyops/manager/pull/822):** update
  recovery uses saved CPE peer reads to re-find an AP after its address changes.
- Tachyon confirmed that a modem firmware update can change the AP management
  bridge MAC. Since DHCP assigns a lease per MAC, the AP management address
  can change. Tachyon targets a fix in firmware 1.15.2 ([issue #777](https://github.com/sixtyops/manager/issues/777)).
- A MAC only selects a candidate; it does not prove identity. Recovery
  confirms identity with an authenticated serial read. It does not scan
  networks or promote addresses outside the active update operation.

### Detail

*(Shipped status: polling records authenticated serial identity in
[PR #792](https://github.com/sixtyops/manager/pull/792). `devices.ip` remains
`TEXT NOT NULL UNIQUE`, so one IP maps to one row.)*

For CPE recovery, save the expected serial, model, peer radio MAC, and current
location before reboot. Try the current address first. For a missing CPE,
inspect fresh successful peer-list reports from APs Manager polls. Read only
unicast addresses reported for the saved radio MAC. Use the same credential
pair selected for the update. Do not use another AP's credentials, global
credentials, a subnet scan, a device name, or certificate pinning. This CPE
recovery shipped in [PR #706](https://github.com/sixtyops/manager/pull/706).

**AP recovery is shipped in [PR #822](https://github.com/sixtyops/manager/pull/822).**
Before reboot or install, Manager saves AP identity, both MACs, management
settings, selected CPE identities, and credential-source references. The
locator reads saved CPE peer reports and authenticates AP candidates with the
selected credentials. Serial, model, management settings, MAC conflicts,
inventory identity, and address ownership must pass before the checked record
move. A changed AP address is accepted only when the before and after settings
show DHCP on the same VLAN. Static changes, changed VLAN or IP mode, missing
settings, identity conflicts, failed persistence, or the deadline stop
recovery.

If the saved address verifies, Manager keeps it. If it is unavailable, it
checks candidates and selects the first verified address in sorted order.
Multiple addresses for the same AP are not an identity conflict. Conflicting
identities or unresolved alternatives still stop recovery. The confirmed
address is saved before the job continues, and Manager checks the expected
serial before each later write. The bounded locator is read-only; it does not
repeat an upload or install.

After a Manager restart during armed AP recovery, startup may record a
verified address, but leaves the interrupted job failed for operator action.
It does not move inventory, write to a device, replay an update, or continue
the queue. See [PR #831](https://github.com/sixtyops/manager/pull/831).

The config-snapshot code already re-links history by MAC when an IP changes
(`poller.py:1109-1140`). With identity in place, that special case goes away.

### 1.3 Offline devices

### Key points

- The poller never deletes a device row.
- Offline devices keep their site, customer label, config history, update
  history, and confirmations. There is no time limit.
- Archiving is a manual, reversible operator action.
- If an archived device is seen again and its serial is confirmed after
  management authentication, it is un-archived.

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

### 1.4 Device certificate pinning

### Key points

- Manager does not save or check managed-device certificate pins.
- Manager uses its existing device management authentication model.
- Certificate pinning and certificate-change holds are not planned.

### Detail

During update recovery, Manager sends the selected management credentials
using the existing transport. It then confirms the expected serial through
an authenticated read. A mismatch or ambiguity stops recovery before another
write.

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
- Scheduled and manual updates default to `bank_mode=one`. Use `bank_mode=both`
  only when an operator selects both banks. Missing bank mode uses `one`; an
  invalid value blocks the job before a device write.
- Firmware files are **artifacts** with a checksum. Rollouts point at an
  artifact id, not a filename.
- A family with no selected artifact has no target. Its devices are left
  out of rollouts. They are not "on hold".

### Detail

Before an explicit per-device update returns "already current," Manager reads
the live bank state with a fresh driver and resolved credentials. Only a
complete read with no warning and a matching active version permits the
no-op. Both-bank mode requires both bank versions to match. A failed read or
mismatch follows normal job admission; stored data does not fill missing live
fields. See [#835](https://github.com/sixtyops/manager/pull/835) and
[#840](https://github.com/sixtyops/manager/pull/840).

The single-device endpoint uses the inventory role. CPE updates use the CPE
recovery path and parent AP credentials. The parent must have role `ap`. See
[#837](https://github.com/sixtyops/manager/pull/837).

*(Older target note: an unknown model maps to `tna-30x`, which could flash the
wrong image.)*

`firmware_artifacts (id, family, version, sha256, release_date, path,
verified_at)`. Settings selects one artifact per family. A rollout stores
the artifact id for each family it is rolling. Filenames are for display.

---

## 3. Serial queue contract

### Key points

- The queue is derived from current inventory and the selected firmware.
- Order device roles as CPEs, APs, then switches.
- Run one device write and recovery sequence at a time.
- Firmware Hold, eligibility, schedule, freeze, weather, clock, artifact, and
  readiness checks remain admission gates.
- Persist the cutoff and first failure for each maintenance window.
- Stop starting devices at cutoff. Finish active recovery and continue later.
- A failed check stops the queue for that window. Resolve it before a later
  window. Never reopen the failed window.

### Detail

This is the queue contract shipped in [PR #715](https://github.com/sixtyops/manager/pull/715).
It derives each pending queue from current Manager inventory instead of saved
rollout membership and percentage selection.

Before admitting a device, check current readiness, Firmware Hold, schedule,
freeze, clock, weather, artifact integrity, device identity, and eligibility.
Keep each write and its recovery under one device sequence. Persist the device
result before admitting another device. If the process restarts during a
sequence, reconcile that device with read-only checks before more work begins.

At the maintenance-window cutoff, do not start another device. Let an active
sequence finish recovery. Derive the remaining queue in the next eligible
window. New and returning devices enter the queue when they need the selected
firmware and pass the admission checks.

The first failed gate or device check records a durable stop for the window.
Do not start another device in that window. In a later window, check the failed
device first. If it already runs the target firmware, verify recovery instead
of flashing again. Otherwise require an explicit operator resolution before
continuing. Keep the failed window closed.

### Firmware Hold

Hold each firmware family until its configured release-date period has elapsed
or a device in that family is confirmed working. A confirmation in one family
does not clear another family. Manual per-device updates keep their deliberate
Hold bypass. Scheduled queue work does not bypass Hold.

### Recovery evidence

Require the expected serial, target version, and role-specific smoke checks.
For AP recovery, use bounded reported peer-return checks, existing RSSI smoke
checks, and reported CPE `eth0` link-up when it was up before reboot. Do not
infer customer traffic from bridge learning or byte-counter changes. Missing
required recovery evidence stops the queue.

## 4. Changes while the queue is open

Firmware window identity uses the timezone resolved by the scheduler. Explicit
zones take precedence. With automatic timezone, the scheduler caches only a
successfully selected and validated zone. An unavailable zone blocks scheduled
admission. Stop resolution uses schedule-local time and keeps the stopped
window closed, including its overnight tail. Manual-day stop keys also use the
scheduler timezone across UTC midnight. See
[#828](https://github.com/sixtyops/manager/pull/828),
[#830](https://github.com/sixtyops/manager/pull/830), and
[#841](https://github.com/sixtyops/manager/pull/841).

The queue is derived again from current inventory at each admission. Devices
that become eligible or return online join when they pass the admission gates.
Re-derive pending devices from current inventory. Do not save membership for future queue runs.

## 5. Target examples

A held firmware family waits until Hold clears. Once eligible, devices run in
CPE, AP, then switch order. If a device fails, no later device starts during
that window. The operator resolves the failure, and a later window verifies
the failed device before continuing. At cutoff, an active device completes
recovery; remaining devices wait for the next window.

## 6. Target state and tests

The target stores window timing and in-flight device recovery using existing
Manager job and history data. It does not add saved rollout membership,
percentage selection or a second rollout engine. Keep existing
history and stop records.

Pin these target invariants in `tests/test_rollout_invariants.py`:

1. Only one device write and recovery sequence can run at a time.
2. A failed device stops later queue entries for the whole window.
3. The cutoff survives restart, stops new starts, and lets active recovery
   finish.
4. Firmware Hold remains family-specific and gates scheduled work.
5. New and returning devices enter the derived queue only when eligible.
6. Recovery uses expected identity, target version, and required smoke checks.
7. Manual per-device updates keep their Hold bypass and do not advance
   scheduled timing.

Keep scheduled queue admission closed until exact-head proof passes.
