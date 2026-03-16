"""MikroTik RouterOS vendor driver (placeholder)."""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..base import VendorDriver, VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult
from .. import register_driver


class MikroTikDriver(VendorDriver):
    """Vendor driver for MikroTik RouterOS devices (placeholder).

    MikroTik devices use SSH for management. Firmware files are .npk packages.
    This is a placeholder — methods raise NotImplementedError until implemented
    against real hardware.
    """

    VENDOR_ID = "mikrotik"
    VENDOR_NAME = "MikroTik"
    SUPPORTED_PROTOCOLS = ["ssh"]
    HAS_DUAL_BANK = False
    HAS_CPE_DISCOVERY = False

    _FIRMWARE_TYPES = [
        {"key": "routeros-arm", "label": "RouterOS ARM", "pattern": r"routeros.*arm.*\.npk"},
        {"key": "routeros-mipsbe", "label": "RouterOS MIPS-BE", "pattern": r"routeros.*mipsbe.*\.npk"},
    ]

    async def connect(self):
        raise NotImplementedError("MikroTik driver not yet implemented — SSH via asyncssh planned")

    async def get_device_info(self) -> VendorDeviceInfo:
        raise NotImplementedError("MikroTik driver not yet implemented")

    async def upload_firmware(self, firmware_path: str, bandwidth_limit_kbps: int = 0) -> bool:
        raise NotImplementedError("MikroTik driver not yet implemented — SFTP upload planned")

    async def trigger_update(self) -> bool:
        raise NotImplementedError("MikroTik driver not yet implemented")

    async def wait_for_reboot(self, timeout: int = 300) -> bool:
        raise NotImplementedError("MikroTik driver not yet implemented")

    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None,
        pass_number: int = 1,
        reboot_timeout: int = 300,
        bandwidth_limit_kbps: int = 0,
    ) -> VendorUpdateResult:
        raise NotImplementedError("MikroTik driver not yet implemented")

    async def run_smoke_tests(
        self, role: str = "ap", pre_update_cpe_count: int = 0
    ) -> VendorSmokeTestResult:
        raise NotImplementedError("MikroTik driver not yet implemented")

    def get_firmware_types(self) -> List[Dict[str, str]]:
        return self._FIRMWARE_TYPES

    def validate_firmware_for_model(self, firmware_path: str, model: str) -> tuple:
        filename = Path(firmware_path).name.lower()
        # MikroTik firmware is architecture-based, not model-based
        if filename.endswith(".npk"):
            return True, ""
        return False, f"MikroTik firmware must be a .npk file, got '{filename}'"

    def extract_version_from_firmware(self, firmware_path: str) -> str:
        filename = Path(firmware_path).name
        match = re.search(r"routeros.*?-(\d+\.\d+(?:\.\d+)?)", filename)
        return match.group(1) if match else ""

    def get_reboot_timeout(self, role: str = "ap") -> int:
        return 180  # MikroTik devices typically reboot faster


register_driver("mikrotik", MikroTikDriver)
