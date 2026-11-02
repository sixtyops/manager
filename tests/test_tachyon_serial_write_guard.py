"""Check the expected serial at every firmware write boundary."""

from unittest.mock import AsyncMock, call

import pytest

from updater.vendors.tachyon import client as tachyon

IP = "192.0.2.20"
FW = "/tmp/tna-30x-1.15.0-r55142-test.bin"


@pytest.mark.asyncio
async def test_upload_retry_rechecks_serial_before_each_attempt(monkeypatch):
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    serial_check = AsyncMock(side_effect=[True, False])
    request = AsyncMock(return_value=(7, b""))
    monkeypatch.setattr(tachyon, "_run_curl", request)
    monkeypatch.setattr(tachyon.asyncio, "sleep", AsyncMock())

    uploaded = await client.upload_firmware(FW, serial_check=serial_check)

    assert not uploaded
    assert serial_check.await_args_list == [call("upload"), call("upload")]
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_serial_mismatch_before_install_blocks_trigger():
    client = tachyon.TachyonClient(IP, "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(return_value=tachyon.DeviceInfo(
        ip=IP, model="TNA-301", current_version="1.12.3.54970",
    ))
    client.upload_firmware = AsyncMock(return_value=True)
    client.trigger_update = AsyncMock(return_value=True)
    serial_check = AsyncMock(return_value=False)

    result = await client.update_firmware(FW, serial_check=serial_check)

    assert not result.success
    assert result.error == "Expected serial did not match before install"
    serial_check.assert_awaited_once_with("install")
    client.trigger_update.assert_not_awaited()
