"""CPE recovery behavior after an AP firmware update."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module
from updater.vendors.tachyon.client import TachyonClient


@pytest.mark.asyncio
async def test_reachable_saved_cpe_is_rebooted(monkeypatch):
    calls = []

    class CPE:
        def __init__(self, ip, *_credentials):
            self.ip = ip

        async def connect(self):
            calls.append((self.ip, "connect"))
            return True

        async def restart_wireless_or_reboot(self):
            calls.append((self.ip, "reboot"))
            return True

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: CPE)
    job = SimpleNamespace(credentials={"192.0.2.8": ("user", "password")})
    baseline = {"peers": {"02:00:00:00:00:01": {"ip": "192.0.2.8"}}}

    await app_module._restart_ap_cpes_after_recovery(job, "192.0.2.1", baseline)

    assert calls == [("192.0.2.8", "connect"), ("192.0.2.8", "reboot")]


@pytest.mark.asyncio
async def test_unreachable_saved_cpe_is_skipped_and_logged(monkeypatch, caplog):
    calls = []

    class CPE:
        def __init__(self, ip, *_credentials):
            self.ip = ip

        async def connect(self):
            calls.append((self.ip, "connect"))
            return False

        async def restart_wireless_or_reboot(self):
            calls.append((self.ip, "reboot"))
            return True

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: CPE)
    job = SimpleNamespace(credentials={"192.0.2.8": ("user", "password")})
    baseline = {"peers": {"02:00:00:00:00:01": {"ip": "192.0.2.8"}}}

    await app_module._restart_ap_cpes_after_recovery(job, "192.0.2.1", baseline)

    assert calls == [("192.0.2.8", "connect")]
    assert "Skipping unreachable CPE" in caplog.text


@pytest.mark.asyncio
async def test_saved_cpe_uses_ap_credentials_when_cpe_credentials_are_missing(monkeypatch):
    calls = []

    class CPE:
        def __init__(self, ip, username, password):
            calls.append((ip, username, password))

        async def connect(self):
            return True

        async def restart_wireless_or_reboot(self):
            return True

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: CPE)
    job = SimpleNamespace(credentials={"192.0.2.1": ("ap-user", "password")})
    baseline = {"peers": {"02:00:00:00:00:01": {"ip": "192.0.2.8"}}}

    await app_module._restart_ap_cpes_after_recovery(job, "192.0.2.1", baseline)

    assert calls == [("192.0.2.8", "ap-user", "password")]


@pytest.mark.asyncio
async def test_rejoin_check_runs_after_saved_cpe_restart(monkeypatch):
    calls = []

    async def restart(_job, _ap_ip, _baseline):
        calls.append("restart")

    async def check(_job, _ap_ip, _ap_client, _baseline, _deadline):
        calls.append("peer_check")
        return True, {"peers": {}}

    monkeypatch.setattr(app_module, "_restart_ap_cpes_after_recovery", restart)
    monkeypatch.setattr(app_module, "_check_ap_customer_recovery", check)

    result = await app_module._check_ap_recovery_after_cpe_rejoin(
        SimpleNamespace(), "192.0.2.1", object(), {"peers": {}}, 10.0
    )

    assert result[0] is True
    assert calls == ["restart", "peer_check"]


@pytest.mark.asyncio
async def test_wireless_restart_method_uses_documented_reboot_fallback():
    client = TachyonClient("192.0.2.8", "user", "password")
    client.reboot = AsyncMock(return_value=True)

    assert await client.restart_wireless_or_reboot(timeout=42) is True

    client.reboot.assert_awaited_once_with(timeout=42)
