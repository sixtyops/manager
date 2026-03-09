"""Tachyon Networks vendor driver."""

from typing import Any, Callable, Dict, List, Optional

from ..base import VendorDriver, VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult
from .. import register_driver
from .client import TachyonClient, DeviceInfo, UpdateResult, SmokeTestResult


class TachyonDriver(VendorDriver):
    """Vendor driver for Tachyon Networks devices (TNA/TNS series)."""

    VENDOR_ID = "tachyon"
    VENDOR_NAME = "Tachyon Networks"
    SUPPORTED_PROTOCOLS = ["https"]
    HAS_DUAL_BANK = True
    HAS_CPE_DISCOVERY = True

    _FIRMWARE_TYPES = [
        {"key": "tna-30x", "label": "TNA-30x AP/CPE", "pattern": r"tna-30x.*\.bin"},
        {"key": "tna-303l", "label": "TNA-303L", "pattern": r"tna-303l.*\.bin"},
        {"key": "tns-100", "label": "TNS-100 Switch", "pattern": r"tns-100.*\.bin"},
    ]

    def __init__(self, ip: str, username: str, password: str, timeout: int = 30):
        super().__init__(ip, username, password, timeout)
        self._client = TachyonClient(ip, username, password, timeout)

    async def connect(self):
        return await self._client.login()

    async def get_device_info(self) -> VendorDeviceInfo:
        info = await self._client.get_device_info()
        return VendorDeviceInfo(
            ip=info.ip,
            vendor="tachyon",
            model=info.model,
            serial=info.serial,
            mac=info.mac,
            current_version=info.current_version,
            bank1_version=info.bank1_version,
            bank2_version=info.bank2_version,
            active_bank=info.active_bank,
        )

    async def upload_firmware(self, firmware_path: str, bandwidth_limit_kbps: int = 0) -> bool:
        return await self._client.upload_firmware(firmware_path, bandwidth_limit_kbps)

    async def trigger_update(self) -> bool:
        return await self._client.trigger_update()

    async def wait_for_reboot(self, timeout: int = 300) -> bool:
        return await self._client.wait_for_reboot(timeout)

    async def reboot(self, timeout: int = 300) -> bool:
        return await self._client.reboot(timeout)

    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None,
        pass_number: int = 1,
        reboot_timeout: int = 300,
        bandwidth_limit_kbps: int = 0,
    ) -> VendorUpdateResult:
        result = await self._client.update_firmware(
            firmware_path, progress_callback, pass_number, reboot_timeout, bandwidth_limit_kbps
        )
        return VendorUpdateResult(
            ip=result.ip,
            success=result.success,
            old_version=result.old_version,
            new_version=result.new_version,
            error=result.error,
            skipped=result.skipped,
            bank1_version=result.bank1_version,
            bank2_version=result.bank2_version,
            active_bank=result.active_bank,
            model=result.model,
        )

    async def run_smoke_tests(
        self, role: str = "ap", pre_update_cpe_count: int = 0
    ) -> VendorSmokeTestResult:
        result = await self._client.run_smoke_tests(role, pre_update_cpe_count)
        return VendorSmokeTestResult(
            passed=result.passed,
            warnings=result.warnings,
            checks=result.checks,
        )

    async def get_connected_cpes(self) -> list:
        return await self._client.get_connected_cpes()

    async def get_ap_info(self) -> Dict[str, Any]:
        return await self._client.get_ap_info()

    async def get_config(self) -> Optional[dict]:
        return await self._client.get_config()

    async def apply_config(self, config: dict, dry_run: bool = False) -> dict:
        return await self._client.apply_config(config, dry_run)

    def get_firmware_types(self) -> List[Dict[str, str]]:
        return self._FIRMWARE_TYPES

    def validate_firmware_for_model(self, firmware_path: str, model: str) -> tuple:
        return self._client.validate_firmware_for_model(firmware_path, model)

    def extract_version_from_firmware(self, firmware_path: str) -> str:
        from .client import _extract_version_from_firmware
        return _extract_version_from_firmware(firmware_path)

    def get_reboot_timeout(self, role: str = "ap") -> int:
        if role == "switch":
            return 600  # TNS-100 switches take longer
        return 300

    def get_hardware_id(self, model: str) -> str:
        return self._client.get_hardware_id(model)


# Register on import
register_driver("tachyon", TachyonDriver)
