"""Cambium ePMP vendor driver (placeholder)."""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..base import VendorDriver, VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult
from .. import register_driver


class CambiumDriver(VendorDriver):
    """Vendor driver for Cambium ePMP devices (placeholder).

    Cambium ePMP devices expose an HTTPS REST API for management.
    They support dual firmware banks and AP-SM topology (similar to Tachyon).
    This is a placeholder — methods raise NotImplementedError until implemented
    against real hardware.
    """

    VENDOR_ID = "cambium"
    VENDOR_NAME = "Cambium ePMP"
    SUPPORTED_PROTOCOLS = ["https"]
    HAS_DUAL_BANK = True
    HAS_CPE_DISCOVERY = True

    _FIRMWARE_TYPES = [
        {"key": "epmp-ap", "label": "ePMP AP", "pattern": r"ePMP.*AP.*\.img"},
        {"key": "epmp-sm", "label": "ePMP SM", "pattern": r"ePMP.*SM.*\.img"},
    ]

    async def connect(self):
        raise NotImplementedError("Cambium driver not yet implemented — HTTPS REST API planned")

    async def get_device_info(self) -> VendorDeviceInfo:
        raise NotImplementedError("Cambium driver not yet implemented")

    async def upload_firmware(self, firmware_path: str, bandwidth_limit_kbps: int = 0) -> bool:
        raise NotImplementedError("Cambium driver not yet implemented")

    async def trigger_update(self) -> bool:
        raise NotImplementedError("Cambium driver not yet implemented")

    async def wait_for_reboot(self, timeout: int = 300) -> bool:
        raise NotImplementedError("Cambium driver not yet implemented")

    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None,
        pass_number: int = 1,
        reboot_timeout: int = 300,
        bandwidth_limit_kbps: int = 0,
    ) -> VendorUpdateResult:
        raise NotImplementedError("Cambium driver not yet implemented")

    async def run_smoke_tests(
        self, role: str = "ap", pre_update_cpe_count: int = 0
    ) -> VendorSmokeTestResult:
        raise NotImplementedError("Cambium driver not yet implemented")

    def get_firmware_types(self) -> List[Dict[str, str]]:
        return self._FIRMWARE_TYPES

    def validate_firmware_for_model(self, firmware_path: str, model: str) -> tuple:
        filename = Path(firmware_path).name.lower()
        if filename.endswith(".img"):
            return True, ""
        return False, f"Cambium firmware must be a .img file, got '{filename}'"

    def extract_version_from_firmware(self, firmware_path: str) -> str:
        filename = Path(firmware_path).name
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", filename)
        return match.group(1) if match else ""

    def get_reboot_timeout(self, role: str = "ap") -> int:
        return 300


register_driver("cambium", CambiumDriver)
