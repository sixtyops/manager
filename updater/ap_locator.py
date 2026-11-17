"""Find one AP through saved identities and bounded, read-only probes."""

import asyncio
import ipaddress
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .vendors.tachyon.client import HTTP_TIMEOUT_S, TachyonClient

MAX_ROUNDS = 20
ROUND_GAP_S = 30
FIRST_PROBE_DELAY_S = 90
MAX_ADDRESSES = 32


class APLocatorError(RuntimeError):
    """Stop AP recovery without an address move or device write."""


@dataclass(frozen=True)
class SavedCPE:
    ip: str
    serial: str
    model: str
    credential_ref: str


@dataclass
class SearchState:
    rounds: int = 0
    next_probe_at: float = 0
    addresses: set[str] = field(default_factory=set)


def _address(value: Any) -> str | None:
    try:
        address = ipaddress.IPv4Address(value) if isinstance(value, str) else None
        if address is not None and not (
            int(address) < 0x01000000 or address.is_multicast
            or address.is_loopback or address.is_reserved
        ):
            return str(address)
    except ipaddress.AddressValueError:
        pass
    return None


def _claim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity(info: Any, serial: str, model: str) -> str:
    actual_serial = _claim(getattr(info, "serial", None))
    actual_model = _claim(getattr(info, "model", None))
    if ((actual_serial and actual_serial != serial)
            or (actual_model and actual_model.casefold() != model.casefold())):
        return "wrong"
    return "match" if actual_serial and actual_model else "incomplete"


async def locate_ap(
    *, serial: str, model: str, ethernet_mac: str, radio_mac: str,
    last_ip: str, credential_ref: str, peers: list[SavedCPE],
    reboot_started: float, deadline: float, state: SearchState,
    client_factory: Callable[[str, str], TachyonClient],
    checkpoint: Callable[[SearchState], Awaitable[None]],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Return one verified address within the caller's existing reboot deadline.

    Supply D as the earlier saved reboot and driver deadline. The caller owns
    the reboot budget from get_reboot_timeout("ap"). The factory must resolve
    the exact credential reference into a separate client for each probe.
    """
    def remaining() -> float:
        value = deadline - clock()
        if not math.isfinite(value) or value <= 0:
            raise APLocatorError("Device not found after update")
        return value

    async def bounded(operation):
        timer = asyncio.timeout(remaining())
        try:
            async with timer:
                result = await operation()
        except TimeoutError:
            if timer.expired():
                raise APLocatorError("Device not found after update") from None
            raise
        remaining()
        return result

    async def save():
        try:
            result = await bounded(lambda: checkpoint(state))
            if result is False:
                raise APLocatorError("AP locator checkpoint failed")
        except APLocatorError:
            raise
        except Exception:
            raise APLocatorError("AP locator checkpoint failed") from None

    async def probe(ip, reference):
        remaining()
        client = client_factory(ip, reference)
        # Translate the injected monotonic clock to the transport's clock.
        client.read_deadline = asyncio.get_running_loop().time() + remaining()
        if await bounded(client.login) is not True:
            return client, None
        return client, await bounded(client.get_device_info)

    serial, model = _claim(serial), _claim(model)
    macs = {TachyonClient._normalize_mac(value) for value in (ethernet_mac, radio_mac)}
    valid_time = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if (not serial or not model or None in macs or not _claim(credential_ref)
            or _address(last_ip) is None or _address(last_ip) != last_ip
            or not all(valid_time(value) for value in (deadline, reboot_started, state.next_probe_at))
            or deadline <= reboot_started
            or type(state.rounds) is not int or not 0 <= state.rounds <= MAX_ROUNDS
            or not isinstance(state.addresses, set)
            or any(_address(ip) is None or _address(ip) != ip for ip in state.addresses)
            or (state.rounds and (last_ip not in state.addresses
                                 or state.next_probe_at < reboot_started + FIRST_PROBE_DELAY_S))):
        raise APLocatorError("Invalid AP locator state")
    usable = [peer for peer in peers if _address(peer.ip) is not None and _address(peer.ip) == peer.ip
              and _claim(peer.serial) and _claim(peer.model) and _claim(peer.credential_ref)]
    state.addresses.add(last_ip)
    if len(state.addresses) > MAX_ADDRESSES:
        raise APLocatorError("AP locator address limit reached")

    while True:
        remaining()
        if state.rounds >= MAX_ROUNDS:
            raise APLocatorError("AP locator round limit reached")
        wait = max(reboot_started + FIRST_PROBE_DELAY_S, state.next_probe_at) - clock()
        if wait > 0:
            await bounded(lambda: sleep(min(wait, remaining())))
            continue
        state.rounds += 1
        state.next_probe_at = clock() + ROUND_GAP_S
        await save()

        valid_reports = 0
        for peer in usable:
            try:
                client, info = await probe(peer.ip, peer.credential_ref)
                if _identity(info, _claim(peer.serial), _claim(peer.model)) != "match":
                    continue
                report = await bounded(client.get_connected_aps)
                if client.last_connected_aps_valid is not True or not isinstance(report, list):
                    continue
                valid_reports += 1
                for row in report:
                    if not isinstance(row, dict) or TachyonClient._normalize_mac(row.get("mac")) not in macs:
                        continue
                    ip = _address(row.get("ip"))
                    if ip is not None:
                        state.addresses.add(ip)
                        if len(state.addresses) > MAX_ADDRESSES:
                            raise APLocatorError("AP locator address limit reached")
            except APLocatorError:
                raise
            except Exception:
                continue
        await save()

        matches, unready = [], set()
        for ip in [last_ip, *sorted(state.addresses - {last_ip})]:
            try:
                _, info = await probe(ip, credential_ref)
                identity = _identity(info, serial, model)
                if identity == "wrong":
                    raise APLocatorError("AP locator identity mismatch")
                if identity == "match":
                    matches.append(ip)
                    if len(matches) > 1:
                        raise APLocatorError("AP locator address is ambiguous")
                elif ip != last_ip:
                    unready.add(ip)
            except APLocatorError:
                raise
            except Exception:
                if ip != last_ip:
                    unready.add(ip)
        remaining()
        if len(matches) == 1 and not unready and (not usable or valid_reports):
            return matches[0]
