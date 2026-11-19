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

Lab evidence, 2026-11-09 and 2026-11-10, about 40 boots on 1.12.3, 1.15.0, and 1.15.1. Source: the lead's lab runs 34 to 39 and the two Manager runs; per-boot results are in the [#777](https://github.com/sixtyops/manager/issues/777) comments and the vendor ticket evidence:

| Boot method | Boots | MAC changes |
|---|---|---|
| Firmware update or downgrade | 18 | 3 |
| Cold boot (switch PoE off, then on) | 13 | 1 |
| Warm reboot (`POST /cgi.lua/reboot`) | 9 | 0 |

- The router DHCP log showed requests from both AP MACs for the same host name, each with its own address.
- In one state, the IPv6 link-local address of the management interface came from the Ethernet MAC while the interface carried the radio MAC. So the MAC changed after the interface came up and before the DHCP request.
- The MAC usually stays the same over many boots. Two Manager runs of the 1.12.3 to 1.15.1 jump (2026-11-07 and 2026-11-10) both changed the address.
- An AP on the radio MAC can change address again when it returns to the Ethernet MAC, for example on a cold boot or on the 1.15.2 upgrade.

## Reboot request and proof

**Key points:** Tachyon reboot uses `POST /cgi.lua/reboot` and requires HTTP
200. Manager also requires proof that the device rebooted.

**Detail:** The request, probes, authentication, and recovery callback share
one deadline. Manager accepts a reachability drop followed by API return, or a
lower `system.uptime` value than the authenticated read immediately before the
request. A recovery callback alone does not prove a reboot. Exact-head lab
proof in [#842](https://github.com/sixtyops/manager/pull/842) covered AP
(TNA-301) firmware 1.12.3.55002 and 1.15.1.55177, and CPE (TNA-303X) firmware
1.15.0.55151 and 1.15.1.55177. No CPE reboot was tested on 1.12.3.

## Status

**Key points**

- Shipped: update recovery can re-find an AP through saved CPE peers, verify its serial, model, MAC conflicts, and management settings, then move the AP record in one checked transaction.
- Shipped: a changed address is accepted only when both management snapshots show DHCP on the same VLAN. Static changes, VLAN or IP-mode changes, missing settings, identity conflicts, and failed persistence stop recovery.
- Bench proof passed for manual and scheduled address changes, a dual-address AP, restart recovery, and real reboots with uptime proof.

**Detail**

| Slice | Issue | PR | State |
|---|---|---|---|
| CPE re-find through AP peer reports | [#702](https://github.com/sixtyops/manager/issues/702) | [#706](https://github.com/sixtyops/manager/pull/706) | Shipped before this epic |
| Read the AP peer from a CPE, and the AP radio MAC | [#783](https://github.com/sixtyops/manager/issues/783) | [#787](https://github.com/sixtyops/manager/pull/787) | Merged, `a7848a6` |
| Move an AP record to a new address in one checked transaction | [#784](https://github.com/sixtyops/manager/issues/784) | [#788](https://github.com/sixtyops/manager/pull/788) | Merged, `2112c5e` |
| Record the serial as device identity during polling | [#791](https://github.com/sixtyops/manager/issues/791) | [#792](https://github.com/sixtyops/manager/pull/792) | Merged, `70f3947` |
| Lab PoE power-cycle procedure | none | [#789](https://github.com/sixtyops/manager/pull/789) | Merged, `31e618f` |
| Management VLAN and IP mode check before and after an update | [#777](https://github.com/sixtyops/manager/issues/777) | [#780](https://github.com/sixtyops/manager/pull/780) | Merged, `69008d8` |
| Docs: address-change status and reboot behavior | none | [#793](https://github.com/sixtyops/manager/pull/793) | Merged, `9c9ef73` |
| Allow same-VLAN DHCP address changes | [#801](https://github.com/sixtyops/manager/issues/801) | [#802](https://github.com/sixtyops/manager/pull/802) | Merged; code gate, see bench in #822 |
| Bounded read-only AP locator | [#810](https://github.com/sixtyops/manager/issues/810) | [#811](https://github.com/sixtyops/manager/pull/811) | Merged; bench proof completed in #822 |
| Save AP recovery intent and arm reboot deadline | [#812](https://github.com/sixtyops/manager/issues/812) | [#815](https://github.com/sixtyops/manager/pull/815) | Merged; unchanged-address bench passed |
| Re-find a moved AP during update recovery | [#814](https://github.com/sixtyops/manager/issues/814) | [#822](https://github.com/sixtyops/manager/pull/822) | Merged; manual and scheduled DHCP address-change bench passed |
| Recover after Manager restart | [#826](https://github.com/sixtyops/manager/issues/826) | [#831](https://github.com/sixtyops/manager/pull/831) | Merged; read-only re-find, job remains failed for operator |
| Keep one AP identity across two addresses | [#832](https://github.com/sixtyops/manager/issues/832) | [#834](https://github.com/sixtyops/manager/pull/834) | Merged; dual-address bench passed |
| Reboot through UI endpoint and prove reboot | [#790](https://github.com/sixtyops/manager/issues/790) | [#842](https://github.com/sixtyops/manager/pull/842) | Merged; real reboot and uptime proof passed |

## Next steps

1. Phase 1 is shipped and its listed bench gates passed. The epic owner can close the remaining documentation slice after this audit is linked.
2. Keep the four tw00 APs on 1.12.3 until the operator approves and performs the fleet update; lab proof does not authorize a live fleet write.
3. Track broader identity work separately: devices that swap addresses, poller re-find outside an active update, address history, per-write identity checks on the driver session, and proof/history keyed by device ID.

## Rules for this work

- Use only Manager's data and the device APIs it already calls. No subnet scan, DHCP, or syslog integration ([#702](https://github.com/sixtyops/manager/issues/702)).
- A MAC, name, or reported address selects a candidate only. An authenticated serial and model match accepts it. Match an AP on both its Ethernet and radio MACs. A CPE reports the AP's radio MAC. If the saved address verifies, keep it; if it is unavailable, choose the first verified address in sorted order after checking candidates. Conflicts and unresolved alternatives stop recovery.
- Recovery reads only. It never repeats an upload or install.
- Keep the serial queue, Firmware Hold, cutoff, single-bank scheduled updates, the first-failure stop, and `updater/rollout_gate.py`. Do not weaken `tests/test_rollout_invariants.py`.

## Lab practice learned in this work

- Do not use the TNA-30x 1.15.0 image in the lab. It has a known web server hang that 1.15.1 fixes. Read the vendor release notes before any lab flash.
- A TNA-301 on 1.15.1 answers its API about 75 to 85 s after a reboot. Wait about 90 s before the first login, then probe every 30 s. In lab run 37 (2026-11-09), logins every 10 s during boot preceded a web server hang on 1.15.1; with 30 s probing the hang did not recur in 14 boots.
- After an AP reboot, the CPE peer list can be empty for up to about a minute. Keep asking until the deadline.
- To find the AP after a boot, read the CPE's wireless peer report, then confirm the AP serial at the reported address.
- A hung lab AP can be power-cycled through its TNS-100 PoE port. See [Power-cycle a lab AP through the switch](dev-hardware-validation.md#power-cycle-a-lab-ap-through-the-switch).
- Run a dev host bench on an image built from the exact PR head, with a temporary compose override, after an online backup of the database. Restore the original image and the backup afterward, and read both back.
