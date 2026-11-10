# Address-change recovery for Tachyon APs

This document records the work on epic [#782](https://github.com/sixtyops/manager/issues/782): the cause, what shipped, what is open, the next steps, and the lab evidence. Update the status and next-step sections when a slice merges.

## Problem

**Key points**

- A Tachyon AP can come back from a firmware update with a different DHCP management address.
- Manager tracks an AP by its address. It waits at the old address, times out with "Recovery callback timed out", and stops the queue, although the AP is healthy.
- First seen on the dev host on 2026-11-07, in a scheduled 1.12.3 to 1.15.1 update of the lab TNA-301 ([#777](https://github.com/sixtyops/manager/issues/777)).

**Detail**

The lab AP uses DHCP on its management VLAN with the WAN zone address (`management.use_zone_ip = true`). It never had a static management address. The CPE behind it kept its address, and its wireless peer report showed the AP at the new address.

## Root cause

**Key points**

- The AP's management bridge (`br-wan` and its VLAN interface) uses the Ethernet MAC on some boots and the radio MAC on others.
- The DHCP server gives each MAC its own lease. When the MAC changes, the address changes.
- Tachyon support confirmed a known defect: an upgrade that also updates the 60 GHz modem firmware (for example 1.12.3 to 1.15.x) changes the order in which interfaces join the bridge. The intended MAC is the Ethernet MAC. Tachyon targets a fix in 1.15.2, planned before the end of 2026.
- Tachyon does not support changes to internal config keys, so `custom_mac` is not a workaround.

**Detail**

Lab evidence, 2026-11-09 and 2026-11-10, about 40 boots on 1.12.3, 1.15.0, and 1.15.1:

| Boot method | Boots | MAC changes |
|---|---|---|
| Firmware update or downgrade | 18 | 3 |
| Cold boot (switch PoE off, then on) | 13 | 1 |
| Warm reboot (`POST /cgi.lua/reboot`) | 9 | 0 |

- The router DHCP log showed requests from both AP MACs for the same host name, each with its own address.
- In one state, the IPv6 link-local address of the management interface came from the Ethernet MAC while the interface carried the radio MAC. So the MAC changed after the interface came up and before the DHCP request.
- The MAC usually stays the same over many boots. Two Manager runs of the 1.12.3 to 1.15.1 jump (2026-11-07 and 2026-11-10) both changed the address.
- An AP on the radio MAC can change address again when it returns to the Ethernet MAC, for example on a cold boot or on the 1.15.2 upgrade.

## Related defect: the reboot call does not reboot

`TachyonClient.reboot()` sends `POST /reboot`. On 1.15.1 this returns HTTP 302 and does not reboot the device. The device web UI reboots with `POST /cgi.lua/reboot`. So the pre-update reboot and the CPE reboot after an AP update do not reboot anything today. Tracked in [#790](https://github.com/sixtyops/manager/issues/790). It merges after [#786](https://github.com/sixtyops/manager/issues/786), because a real pre-update reboot can also change the AP address.

## Status

**Key points**

- Shipped: the building blocks (client reads, checked address move, serial identity). They add no runtime change to update recovery yet.
- Open: the management VLAN and IP mode check, and the AP re-find itself.

**Detail**

| Slice | Issue | PR | State |
|---|---|---|---|
| CPE re-find through AP peer reports | [#702](https://github.com/sixtyops/manager/issues/702) | [#706](https://github.com/sixtyops/manager/pull/706) | Shipped before this epic |
| Read the AP peer from a CPE, and the AP radio MAC | [#783](https://github.com/sixtyops/manager/issues/783) | [#787](https://github.com/sixtyops/manager/pull/787) | Merged, `a7848a6` |
| Move an AP record to a new address in one checked transaction | [#784](https://github.com/sixtyops/manager/issues/784) | [#788](https://github.com/sixtyops/manager/pull/788) | Merged, `2112c5e` |
| Record the serial as device identity during polling | [#791](https://github.com/sixtyops/manager/issues/791) | [#792](https://github.com/sixtyops/manager/pull/792) | Merged, `70f3947` |
| Lab PoE power-cycle procedure | none | [#789](https://github.com/sixtyops/manager/pull/789) | Merged, `31e618f` |
| Management VLAN and IP mode check before and after an update | [#777](https://github.com/sixtyops/manager/issues/777) | [#780](https://github.com/sixtyops/manager/pull/780) | Open; bench passed the fail-closed path |
| Docs: address-change status and reboot behavior | none | [#793](https://github.com/sixtyops/manager/pull/793) | Open; review clean |
| Re-find the AP through its CPEs during update recovery | [#786](https://github.com/sixtyops/manager/issues/786) | none | Design note posted; not started |
| Reboot with the real endpoint and prove the reboot | [#790](https://github.com/sixtyops/manager/issues/790) | none | Queued after #786 |

## Next steps

1. Merge [#780](https://github.com/sixtyops/manager/pull/780) after the review loop passes its head.
2. Merge [#793](https://github.com/sixtyops/manager/pull/793) after the review loop passes its head.
3. Build [#786](https://github.com/sixtyops/manager/issues/786) on `main` after #780 merges. Follow its [design note](https://github.com/sixtyops/manager/issues/786#issuecomment-6755631148) and the [lead corrections](https://github.com/sixtyops/manager/issues/786#issuecomment-6755651496). One PR, `updater/app.py` only, under about 300 changed engine lines. In DHCP mode, a new lease address passes after the serial is verified at that address; a static address change still stops the queue.
4. Bench #786 on the lab AP: a manual and a scheduled 1.12.3 to 1.15.1 update. Require "Address changed after update; identity verified" when the address changes, and a normal success when it does not.
5. Build [#790](https://github.com/sixtyops/manager/issues/790) after #786.
6. tw00 (4 APs on 1.12.3): hold the jump until #786 passes its bench, or until both MACs of each AP are reserved for one address at the router. A reservation for one MAC does not help.
7. After Phase 1, create Phase 2 issues: two devices that swap addresses, re-find outside an update by the poller, nullable `devices.ip` with address history, a per-write identity check on the driver session, and proof and history keyed by device ID.

## Rules for this work

- Use only Manager's data and the device APIs it already calls. No subnet scan, DHCP, or syslog integration ([#702](https://github.com/sixtyops/manager/issues/702)).
- A MAC, name, or reported address selects a candidate only. An authenticated serial and model match accepts it. Match an AP on both its Ethernet and radio MACs. A CPE reports the AP's radio MAC.
- Recovery reads only. It never repeats an upload or install.
- Keep the serial queue, Firmware Hold, cutoff, single-bank scheduled updates, the first-failure stop, and `updater/rollout_gate.py`. Do not weaken `tests/test_rollout_invariants.py`.

## Lab practice learned in this work

- Do not use the TNA-30x 1.15.0 image in the lab. It has a known web server hang that 1.15.1 fixes. Read the vendor release notes before any lab flash.
- A TNA-301 on 1.15.1 answers its API about 75 to 85 s after a reboot. Wait about 90 s before the first login, then probe every 30 s. Logins every 10 s during boot preceded web server hangs.
- After an AP reboot, the CPE peer list can be empty for up to about a minute. Keep asking until the deadline.
- To find the AP after a boot, read the CPE's wireless peer report, then confirm the AP serial at the reported address.
- A hung lab AP can be power-cycled through its TNS-100 PoE port. See [Power-cycle a lab AP through the switch](dev-hardware-validation.md#power-cycle-a-lab-ap-through-the-switch).
- Run a dev host bench on an image built from the exact PR head, with a temporary compose override, after an online backup of the database. Restore the original image and the backup afterward, and read both back.
