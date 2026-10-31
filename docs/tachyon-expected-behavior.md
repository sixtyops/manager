# Expected Tachyon CPE and AP behavior

**Key points:** Use these read-only signals to check that a customer router
remains connected through a Tachyon CPE. The values below come from one idle
lab capture on 2026-10-30. The AP ran firmware 1.15.1.55177; its attached
TNA-303X CPE ran firmware 1.15.0.55159. The capture does not establish CPE
negotiation behavior on firmware 1.15.1.55177. Treat the rates as one
observation, not as thresholds or recovery timing. No fault was injected.

**Detail:** The router's MAC is shown only by suffix `B0:49:A0`. The CPE radio
MAC suffix was `D0:A0:BE`; its `eth0` MAC suffix was `D0:A0:BC`. Keep the
radio and Ethernet MACs distinct. Repeat this capture on the target CPE build
before treating its values as a qualified baseline.

## Signals

| Signal | Read and fields | Normal values seen | Fault indication |
|---|---|---|---|
| CPE reconnect by MAC | AP `GET /cgi.lua/status?type=wireless,zones`; `wireless.peers[].mac`, `model`, `fw`, `linkUptime`, `rxRate`, `txRate`. The client method is `get_connected_cpes()`. | One peer remained listed. Radio MAC suffix `D0:A0:BE`, model `TNA-303X`, firmware `1.15.0.r55159`, `rxRate=2502`, `txRate=2502`, `linkUptime=4290` seconds at capture. | A hard down removes the expected peer. After it returns, match the same radio MAC and check that `linkUptime` starts again and rises. A replacement MAC does not prove the original CPE returned. Rate units need vendor confirmation. |
| CPE `eth0` link and negotiation | CPE `GET /cgi.lua/status?type=interfaces`; `interfaces.eth0.disabled`, `type`, `zone`, and `mtu`. `get_device_info()` and `get_ap_info()` already read this endpoint but parse interface MACs only. | `disabled=false`, `type=wired`, `zone=wan`, `mtu=1500`. The endpoint did not return link state, negotiated speed, or duplex fields. | A lower negotiated speed or duplex change cannot be detected from these fields. If a supported field later reports speed below the saved baseline, fail the check. `disabled=false` does not prove carrier. The client needs a supported field source and parser before this check can pass. |
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

**Key points:** Existing client calls can identify the peer and find learned
MACs. They do not yet expose a complete Ethernet or traffic recovery check.

**Detail:** `get_connected_cpes()` returns the peer MAC and link fields needed
to match a reconnect. `get_bridge_table()` returns MAC and port pairs, but
discards VLAN and learning-age details and needs validation as a CPE/AP
contract. The interface methods already read `status?type=interfaces`, but
only keep interface MACs. On CPE `eth0`, `rx_bytes` counts bytes received
from the customer router and `tx_bytes` counts bytes sent to it. On the AP
peer row, `rxBytes` counts bytes received from that CPE and `txBytes` counts
bytes sent to it. Add parsing for these counters and a supported source for
link, speed, and duplex. `rxRate` and `txRate` are reported rates, not byte
counters. The existing methods do not read DHCP lease status.

For the four A5 checks:

1. **Reconnect by MAC:** `get_connected_cpes()` already returns `mac`. Compare
   the saved radio MAC before and after recovery.
2. **`eth0` negotiation:** `get_device_info()` and `get_ap_info()` already read
   `status?type=interfaces`. The current parser keeps MACs only, and the
   captured response has no carrier, speed, or duplex field. A supported
   field source and parser are still needed.
3. **Customer MAC on CPE `eth0`:** `get_bridge_table()` returns `mac` and
   `port`, which were enough to find the router on `eth0`. The parser drops
   VLAN and learning-age evidence, so it cannot prove fresh learning by
   itself.
4. **Two-way traffic:** no current method returns byte counters. Parse
   `interfaces.eth0.rx_bytes` and `tx_bytes`, and AP
   `wireless.peers[].rxBytes` and `txBytes`, from the existing reads. Keep the
   CPE interface counters and AP per-CPE counters separate.

Treat a learned customer MAC as forwarding-path evidence only. It does not
prove a DHCP lease or successful application traffic. Do not generate traffic
to qualify an idle customer link. Record a healthy two-way baseline and require
positive deltas in both directions after recovery when that baseline had
two-way traffic.
