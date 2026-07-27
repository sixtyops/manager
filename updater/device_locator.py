"""Locate a managed device that got a new DHCP IP (v1.5.0 re-IP recovery).

v1.5.0 firmware sources DHCP from a non-eth0 interface, so devices come back on a
new IP after an update. SixtyOps keys everything on IP, so a re-IP'd device looks
dead at its old address. This module finds it again by its immutable **serial**
(the only reliable anchor — the new IP binds to a MAC we may not have keyed on).

The locator is a ladder, cheapest first, and **every candidate IP is confirmed by
connecting and matching the serial** before it is returned:

1. Wireless peer directory — the AP peer list (cached in ``cpe_cache``) already
   maps every wirelessly-connected device's MAC to its current IP. Free.
2. Bounded /24 scan — for a wired AP the switch bridge table proves the device is
   still forwarding (alive, just moved) but not its IP, so we scan the device's own
   /24, reachability-filter, and probe each responder's serial. Gated by a setting.

Used by the rollout recovery hook, the manual "Change IP" endpoint, and (peer step
only) the poller's drift reconciliation, so re-IP handling lives in one place.
"""

import asyncio
import ipaddress
import logging
import os
from typing import Optional

from . import database as db
from .vendors import get_driver

logger = logging.getLogger(__name__)

VERIFY_SSL = os.environ.get("TACHYON_VERIFY_SSL", "").lower() in ("1", "true", "yes")

# Concurrency caps for the /24 scan. Reachability is cheap (one short curl);
# serial probing logs in, so it is kept tighter.
_REACH_CONCURRENCY = 64
_PROBE_CONCURRENCY = 16
_REACH_TIMEOUT_S = 2


def _norm(mac: Optional[str]) -> Optional[str]:
    return mac.upper() if mac else None


async def probe_identity(
    ip: str, username: str, password: str, vendor: str = "tachyon", timeout: int = 12
) -> Optional[dict]:
    """Log into a device and read its identity. Returns {serial, mac, macs,
    system_name, model} or None if unreachable / auth fails / no serial."""
    try:
        client = get_driver(vendor)(ip, username, password, timeout=timeout)
        connected = await client.connect()
        if connected is not True:
            return None
        info = await client.get_ap_info()
    except Exception as e:
        logger.debug(f"probe_identity({ip}) failed: {e}")
        return None
    serial = info.get("serial")
    if not serial:
        return None
    return {
        "ip": ip,
        "serial": serial,
        "mac": _norm(info.get("mac")),
        "macs": info.get("macs") or ([_norm(info.get("mac"))] if info.get("mac") else []),
        "system_name": info.get("system_name"),
        "model": info.get("model"),
    }


async def confirm_serial_at_ip(
    ip: str, expected_serial: str, username: str, password: str, vendor: str = "tachyon"
) -> Optional[dict]:
    """Return the probed identity iff the device at ``ip`` reports ``expected_serial``."""
    ident = await probe_identity(ip, username, password, vendor=vendor)
    if ident and expected_serial and ident["serial"] == expected_serial:
        return ident
    return None


def _peer_directory_candidates(macs: list[str], exclude_ip: str) -> list[str]:
    """IPs from the cached wireless peer directory whose MAC matches any of ``macs``."""
    wanted = {_norm(m) for m in macs if m}
    if not wanted:
        return []
    seen, out = set(), []
    for cpe in db.get_all_cpes():
        cmac = _norm(cpe.get("mac"))
        cip = cpe.get("ip")
        if cmac in wanted and cip and cip != exclude_ip and cip not in seen:
            seen.add(cip)
            out.append(cip)
    return out


def _is_alive_on_switch(macs: list[str]) -> bool:
    """True if any of the device's MACs is still forwarding on a managed switch —
    proof the unit is up, just re-IP'd."""
    for m in macs:
        if m and db.get_ap_switch_port(m):
            return True
    return False


def _subnet_hosts(hint_ip: str, exclude: set[str]) -> list[str]:
    """Host IPs of hint_ip's /24, minus network/broadcast and excluded addresses."""
    try:
        net = ipaddress.ip_interface(f"{hint_ip}/24").network
    except ValueError:
        return []
    return [str(h) for h in net.hosts() if str(h) not in exclude]


async def _reachable(ip: str) -> bool:
    """Quick TLS liveness check so we only try to log into hosts that answer."""
    cmd = ["curl", "-s", "-m", str(_REACH_TIMEOUT_S)]
    if not VERIFY_SSL:
        cmd.append("-k")
    cmd.extend(["-o", "/dev/null", "-w", "%{http_code}", f"https://{ip}/"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await proc.communicate()
    except Exception:
        return False
    code = stdout.decode().strip()
    return code.isdigit() and int(code) > 0


async def locate_device(
    serial: str,
    macs: list[str],
    hint_ip: str,
    username: str,
    password: str,
    *,
    vendor: str = "tachyon",
    allow_scan: bool = True,
) -> Optional[str]:
    """Find the current IP of a device identified by ``serial``, or None.

    ``hint_ip`` is the last-known (now-stale) IP; ``macs`` are the device's known
    interface MACs used as search hints. The returned IP is always serial-confirmed.
    """
    if not serial:
        return None
    macs = [_norm(m) for m in (macs or []) if m]

    # 1) Wireless peer directory — confirm each candidate by serial.
    for cand in _peer_directory_candidates(macs, hint_ip):
        ident = await confirm_serial_at_ip(cand, serial, username, password, vendor=vendor)
        if ident:
            logger.info(f"Located {serial} at {cand} via wireless peer directory")
            return cand

    # 2) Bounded /24 scan (wired APs the peer directory can't see).
    if not allow_scan:
        return None
    alive_hint = " (still forwarding on switch)" if _is_alive_on_switch(macs) else ""
    hosts = _subnet_hosts(hint_ip, exclude={hint_ip})
    if not hosts:
        return None
    logger.info(f"Scanning {len(hosts)} hosts on {hint_ip}/24 for {serial}{alive_hint}")

    reach_sem = asyncio.Semaphore(_REACH_CONCURRENCY)

    async def _check(ip):
        async with reach_sem:
            return ip if await _reachable(ip) else None

    responders = [r for r in await asyncio.gather(*[_check(h) for h in hosts]) if r]

    probe_sem = asyncio.Semaphore(_PROBE_CONCURRENCY)
    found: dict = {"ip": None}

    async def _probe(ip):
        async with probe_sem:
            if found["ip"]:
                return
            ident = await confirm_serial_at_ip(ip, serial, username, password, vendor=vendor)
            if ident and not found["ip"]:
                found["ip"] = ip

    # Probe responders concurrently; stop as soon as one matches the serial.
    tasks = [asyncio.ensure_future(_probe(ip)) for ip in responders]
    pending = set(tasks)
    while pending and not found["ip"]:
        _done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if found["ip"]:
        logger.info(f"Located {serial} at {found['ip']} via /24 scan")
        return found["ip"]
    return None
