# Re-IP Recovery

**Bottom line:** When a device comes back on a **new DHCP IP** after an update, the
Manager finds it again by its immutable **serial number** and updates its record —
so a firmware rollout keeps going instead of falsely halting, and the fleet stays
accurate. If it can't be found automatically, an operator can move it by hand with
a right-click. A device that is genuinely dead still halts the job.

## Why this exists

The Manager reaches every device by IP address. Tachyon v1.5.0 firmware sources its
DHCP request from a different interface, so the DHCP server hands the device a new
IP after it reboots. Without recovery, the device looks unreachable at its old
address: mid-rollout the Manager would report "did not come back online" and halt
the whole job — even though the device updated fine. The physical **eth0 MAC does
not change**, but the new IP binds to a *different* interface's MAC, so the serial
(which never changes) is the only reliable anchor.

## How it works

On every poll the Manager records each device's **serial** and its **full set of
interface MACs** (`device_identity` table). When a device is unreachable at its
known IP, the shared locator (`updater/device_locator.py`) finds it — cheapest step
first, and **every candidate is confirmed by reading its serial** before adoption,
so it can never bind a record to a different unit:

1. **Wireless peer directory** — the AP peer list (cached in `cpe_cache`) already
   maps every wirelessly-connected device's MAC to its current IP. Free; covers
   CPEs and any AP on a wireless backhaul.
2. **/24 scan** — for a wired AP the switch bridge table proves the eth0 MAC is
   still forwarding (alive, just moved) but not the IP, so the locator scans the
   device's own /24, reachability-filters, and probes each responder's serial.
   Controlled by the `rediscover_scan_enabled` setting (default on).

**During a rollout**, a device reported as "did not come back online" is run through
the locator before the job is cancelled. If found and verified on the target
firmware, its record is moved (all history follows) and the wave continues — the
job shows "moved to `<new IP>`". If not found, the job halts as before: the
fail-closed guarantee is preserved, recovery only decides *where* a device is, never
whether a wave may advance.

**Manually**, right-click a device's name and choose **Change IP address…**. The
Manager logs into the new address with the device's saved credentials and requires
the serial to match before moving it (`POST /api/aps/{ip}/change-ip`). This replaces
the old delete-and-re-add and keeps the device's history.

## Notes

- All record moves reuse `change_device_ip()`, which repoints every IP-keyed table
  (CPE cache, config snapshots, audit history, bridge entries, identity) so nothing
  is orphaned at the stale address.
- Operator-side mitigation, where possible: DHCP reservations keyed on the DHCP
  client-identifier rather than MAC, or static management IPs, avoid the re-IP
  entirely. The Manager self-heals regardless.
