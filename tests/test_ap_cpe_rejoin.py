"""The standalone wireless restart API retains its documented fallback."""
from unittest.mock import AsyncMock
import pytest
from updater.vendors.tachyon.client import TachyonClient


@pytest.mark.asyncio
async def test_wireless_restart_method_uses_documented_reboot_fallback():
    client = TachyonClient("192.0.2.8", "user", "password")
    client.reboot = AsyncMock(return_value=True)

    assert await client.restart_wireless_or_reboot(timeout=42) is True

    client.reboot.assert_awaited_once_with(timeout=42)


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [True, False])
async def test_peer_recovery_is_read_only_and_keeps_original_deadline(monkeypatch, returned):
    from types import SimpleNamespace
    from updater import app
    clock = {"now": 10.0}
    monkeypatch.setattr(app, "monotonic", lambda: clock["now"])

    async def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(app.asyncio, "sleep", sleep)
    peer = SimpleNamespace(ip="192.0.2.8", mac="02:00:00:00:00:01")
    ap = SimpleNamespace(get_connected_cpes=AsyncMock(return_value=[peer] if returned else []),
                         last_connected_cpes_valid=True)
    monkeypatch.setattr(app, "get_driver", lambda vendor: pytest.fail("Recovery must not write to a CPE"))
    passed, _ = await app._check_ap_recovery(
        SimpleNamespace(), "192.0.2.1", ap,
        {"peers": {peer.mac: {"ip": peer.ip, "eth0_link_was_up": False}}}, 15.0,
    )
    assert passed is returned
    assert clock["now"] == (10 if returned else 15)
    assert not hasattr(app, "_restart_ap_cpes_after_recovery")
    assert not hasattr(app, "_check_ap_recovery_after_cpe_rejoin")
