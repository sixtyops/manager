# Tachyon identity fixtures

These sanitized fixtures record the identity fields returned by the AP, its
peer list, and the switch. The values in angle brackets are placeholders.
They contain no device serial, MAC address, IP address, or customer label.

`*_system.json` stores `serial` and `model` from `/cgi.lua/status?type=system`.
`*_interfaces.json` stores each interface's `mac_address` from
`/cgi.lua/status?type=interfaces`. Repeated placeholders show MAC aliases
shared by multiple interfaces. `*_zones.json` stores
`management_interface` and `mgmt_vlan_id` from `/cgi.lua/status?type=zones`.
The `ap_peer_list.json` file stores the CPE model and its redacted peer MAC.
It has no serial or direct CPE interface data. `cpe_system.json`,
`cpe_interfaces.json`, and `cpe_bootbank.json` hold two direct CPE reads each.

The direct CPE status requests did not include a zone endpoint. The CPE
fixtures do not identify its management interface or VLAN.

The AP reports 63 interfaces with MAC fields and 52 distinct MAC aliases. The
switch reports nine interfaces and seven distinct aliases. On both devices,
`br-wan.12` is the management interface and uses VLAN 12. Its MAC value is the
management alias; it matches `eth0` and `br-wan`.

Status reads do not expose whether management addressing uses DHCP or a static
address.
