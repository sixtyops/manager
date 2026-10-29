"""Recheck address qualification at each Tachyon firmware write."""

from unittest.mock import AsyncMock

import pytest

from updater.vendors.tachyon import client as tachyon

IP = "192.0.2.20"
FW = "/tmp/tna-30x-1.15.0-r55142-test.bin"
SOURCE = "1.12.3.54970"


def info(version=SOURCE, vlan=100):
    return tachyon.DeviceInfo(
        ip=IP, model="TNA-301", current_version=version,
        management_vlan_id=vlan,
    )


def checker(expected_vlan=100):
    async def qualify(fresh):
        if fresh.current_version != SOURCE or fresh.management_vlan_id != expected_vlan:
            return False, "Address-change hold: live device scope changed"
        return True, ""
    return qualify


@pytest.mark.asyncio
async def test_changed_live_source_holds_before_upload(monkeypatch):
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(side_effect=[info(), info(version="1.12.3.54971")])
    request = AsyncMock(return_value=(0, b'{"status":"ok"}'))
    monkeypatch.setattr(tachyon, "_run_curl", request)

    result = await client.update_firmware(FW, qualification_check=checker())

    assert not result.success
    assert result.error == "Address-change hold: live device scope changed"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_management_scope_holds_before_trigger(monkeypatch):
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(side_effect=[info(), info(), info(vlan=200)])
    client.trigger_update = AsyncMock(return_value=True)
    request = AsyncMock(return_value=(0, b'{"status":"ok"}'))
    monkeypatch.setattr(tachyon, "_run_curl", request)

    result = await client.update_firmware(FW, qualification_check=checker())

    assert not result.success
    assert result.error == "Address-change hold: live device scope changed"
    request.assert_awaited_once()
    client.trigger_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_rechecks_qualification_before_second_upload(monkeypatch):
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    client.get_device_info = AsyncMock(side_effect=[info(), info(vlan=200)])
    request = AsyncMock(return_value=(7, b""))
    monkeypatch.setattr(tachyon, "_run_curl", request)
    monkeypatch.setattr(tachyon.asyncio, "sleep", AsyncMock())

    uploaded = await client.upload_firmware(
        FW, qualification_check=checker(),
    )

    assert not uploaded
    request.assert_awaited_once()
    assert client._qualification_denial == "Address-change hold: live device scope changed"


@pytest.mark.asyncio
async def test_live_info_error_denies_upload_with_unknown_state(monkeypatch):
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    client.get_device_info = AsyncMock(side_effect=OSError("offline"))
    request = AsyncMock(return_value=(0, b'{"status":"ok"}'))
    monkeypatch.setattr(tachyon, "_run_curl", request)
    observed = []

    async def qualification(fresh, error=""):
        observed.append((fresh, error))
        return False, "Deferred: Address-change qualification unavailable"

    uploaded = await client.upload_firmware(FW, qualification_check=qualification)

    assert not uploaded
    assert observed == [(None, "live device information could not be read")]
    request.assert_not_awaited()
