# Expected Tachyon CPE and AP behavior

**Key points:** Use these read-only signals to check that a customer router
remains connected through a Tachyon CPE. The values below come from one idle
lab capture on 2026-10-30. The AP ran firmware 1.15.1.55177; its attached
TNA-303X CPE ran firmware 1.15.0.55159. The capture does not establish CPE
negotiation behavior on firmware 1.15.1.55177. Treat the rates as one
observation, not as thresholds or recovery timing. No fault was injected.

**Detail:** The router's MAC is shown only by suffix `B0:49:A0`. The CPE radio
MAC suffix was `D0:A0:BE`; its `eth0` MAC suffix was `D0:A0:BC`. Keep the
radio and Ethernet MACs distinct. The captured values remain observations only. The shipped A5 recovery checks do not use an unknown `eth0` baseline as evidence; the CPE `eth0` link check runs only when the pre-reboot report showed link-up.

## Single-bank firmware check

**Key points:** Manager uses one bank by default. Verify the active bank after
each update and record the other bank's firmware version.

**Detail:** In the lab upgrade and restore check, verify the active bank before
the upgrade. After restore, verify that the active bank runs the original
firmware. Record the other bank version as observed.

## Signals

| Signal | Read and fields | Normal values seen | Fault indication |
|---|---|---|---|
| CPE reconnect by MAC | AP `GET /cgi.lua/status?type=wireless,zones`; `wireless.peers[].mac`, `model`, `fw`, `linkUptime`, `rxRate`, `txRate`. The client method is `get_connected_cpes()`. | One peer remained listed. Radio MAC suffix `D0:A0:BE`, model `TNA-303X`, firmware `1.15.0.r55159`, `rxRate=2502`, `txRate=2502`, `linkUptime=4290` seconds at capture. | A hard down removes the expected peer. After it returns, match the same radio MAC and check that `linkUptime` starts again and rises. A replacement MAC does not prove the original CPE returned. Rate units need vendor confirmation. |
| CPE `eth0` link and negotiation | CPE `GET /cgi.lua/status?type=interfaces`; `interfaces.eth0.disabled`, `type`, `zone`, and `mtu`. `get_device_info()` and `get_ap_info()` already read this endpoint but parse interface MACs only. | `disabled=false`, `type=wired`, `zone=wan`, `mtu=1500`. The captured response did not return link state, negotiated speed, or duplex fields. Later supported reports can expose `link_up` and byte counters. | A lower negotiated speed or duplex change cannot be detected from this capture. `disabled=false` does not prove carrier. Recovery checks use reported link state only when the pre-reboot report showed link-up. |
| Customer MAC learned on CPE `eth0` | CPE `GET /cgi.lua/bridge_table`; response `data` rows contain the MAC, `dev`, port, and bridge text. `get_bridge_table()` returns `{mac, port}`. | Eight parsed entries; router suffix `B0:49:A0` appeared once on `eth0` under `br-wan`. The row had no VLAN, age, or dynamic/learning flag. | Missing router MAC or a different port fails the check. A hard Ethernet down should stop the counters and remove the customer row after aging. The current response cannot prove when the row was learned. A present row alone does not prove DHCP or end-to-end forwarding. |
| Two-way traffic | CPE `GET /cgi.lua/status?type=interfaces`; `interfaces.eth0.rx_bytes`, `tx_bytes`, `rx_packets`, `tx_packets`. AP per-CPE data uses the same peer endpoint as above with `rxBytes`, `txBytes`, `rxPackets`, `txPackets`. The client does not parse these counters today. | Six CPE samples, about 10 seconds apart, had nonzero deltas in both directions: `rx_bytes` 0.16–1.29 kbps and `tx_bytes` 1.86–3.51 kbps per interval. Across 51 seconds: 0.70 kbps RX and 2.56 kbps TX. AP per-CPE counters also rose both ways: `rxBytes` 3.03–11.01 kbps and `txBytes` 0.70–2.62 kbps per interval; across 50 seconds: 5.31 kbps RX and 1.59 kbps TX. | If one direction rises and the other does not, traffic is one-way. If both stop, the link may be down or idle; compare with the saved baseline and bridge evidence. For this router, idle traffic was nonzero both ways. Compute kbps as `8 × byte_delta / (1000 × elapsed_seconds)`. |
| Customer MAC upstream | AP and TNS-100 `GET /cgi.lua/bridge_table`; response `data` rows contain the MAC, `dev`, port, optional `vlan`, and bridge text. | AP learned router suffix `B0:49:A0` on `prs0` under `br-wan`. TNS-100 learned it on `eth1`, VLAN 101, under `br-wan`. | Missing upstream entry, an unexpected port, or a VLAN other than the expected customer VLAN is a fault signal. An entry can remain while DHCP or forwarding is blocked, so also check two-way counters. The AP row had no VLAN field. |

The API v1.0.8 Swagger page lists `GET /stats` and `GET /bridge_table`.
The client uses the status and bridge paths above. A read of
`/cgi.lua/apiv1/stats` returned HTTP 401 with the existing client session, so
this capture did not establish whether that API route reports Ethernet
negotiation or counters. Do not infer unsupported fields from the API route
name. See the [TNA-30x API v1.0.8](https://tachyon-networks.github.io/api_docs/tna_30x/v1.0.8/).

## Client coverage

**Key points:** Shipped A5 recovery checks require the saved CPE peer to return by radio MAC. They check CPE `eth0` link-up only when the pre-reboot report showed it up.

**Detail:** `get_connected_cpes()` returns the radio MAC used to confirm peer return. `get_eth0_recovery_state()` reads reported link state and counters. Recovery uses the link result only when the pre-reboot report showed `eth0` up. Unknown pre-reboot link state skips this check; it does not create a baseline or fail recovery. AP recovery also requires the saved CPE peers to return, then reboots each reachable saved CPE and checks peer return again.

The capture above documents observable fields. It does not define recovery thresholds. Bridge learning, byte counters, negotiated rate, DHCP state, and customer application traffic are not A5 recovery gates. The current client does not use those signals to qualify a customer path.

## AP update CPE rejoin

**Key points:** After an AP firmware update recovers, Manager reboots each
reachable CPE saved in the pre-update peer list before it checks peer return.

**Detail:** The repository documents `POST /reboot` and has no observed or
documented wireless-only restart call. Manager uses the existing reboot call
for this step. It skips and logs CPEs that it cannot reach.
