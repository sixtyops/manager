"""Firmware update results follow the device after an ambiguous reboot."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from updater.vendors.tachyon.client import DeviceInfo, TachyonClient


FIRMWARE = "/tmp/tna-30x-1.15.0-r55142.bin"
OLD_VERSION = "1.12.3.54970"
TARGET_VERSION = "1.15.0.55142"


def device_info(current, bank1, bank2, active):
    return DeviceInfo(
        ip="192.0.2.20",
        model="TNA-301",
        current_version=current,
        bank1_version=bank1,
        bank2_version=bank2,
        active_bank=active,
    )


def prepared_client(readback):
    client = TachyonClient("192.0.2.20", "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(side_effect=[
        device_info(OLD_VERSION, OLD_VERSION, "1.10.0.1", 2),
        readback,
    ])
    client.validate_firmware_for_model = MagicMock(return_value=(True, ""))
    client.upload_firmware = AsyncMock(return_value=True)
    client.trigger_update = AsyncMock(return_value=True)
    client.wait_for_reboot = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_late_reboot_readback_reports_landed_firmware_as_success():
    client = prepared_client(
        device_info(TARGET_VERSION, TARGET_VERSION, TARGET_VERSION, 1)
    )

    result = await client.update_firmware(FIRMWARE)

    assert result.success
    assert result.error is None
    assert result.new_version == TARGET_VERSION
    assert (result.active_bank, result.bank1_version, result.bank2_version) == (
        1, TARGET_VERSION, TARGET_VERSION,
    )
    assert client.get_device_info.await_count == 2


@pytest.mark.asyncio
async def test_readback_preserves_failure_when_target_did_not_land():
    client = prepared_client(
        device_info(OLD_VERSION, OLD_VERSION, OLD_VERSION, 2)
    )

    result = await client.update_firmware(FIRMWARE)

    assert not result.success
    assert result.error == "Device did not come back online"
    assert result.new_version == OLD_VERSION
    assert (result.active_bank, result.bank1_version, result.bank2_version) == (
        2, OLD_VERSION, OLD_VERSION,
    )
    assert client.get_device_info.await_count == 2
