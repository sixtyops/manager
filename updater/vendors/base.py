"""Vendor driver abstract base class and shared data contracts."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class VendorDeviceInfo:
    """Vendor-neutral device information."""
    ip: str
    vendor: str = ""
    model: Optional[str] = None
    serial: Optional[str] = None
    mac: Optional[str] = None
    current_version: Optional[str] = None
    bank1_version: Optional[str] = None
    bank2_version: Optional[str] = None
    active_bank: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VendorUpdateResult:
    """Vendor-neutral firmware update result."""
    ip: str
    success: bool
    old_version: Optional[str] = None
    new_version: Optional[str] = None
    error: Optional[str] = None
    skipped: bool = False
    bank1_version: Optional[str] = None
    bank2_version: Optional[str] = None
    active_bank: Optional[int] = None
    model: Optional[str] = None


@dataclass
class VendorSmokeTestResult:
    """Vendor-neutral post-update smoke test result."""
    passed: bool = True
    warnings: list = field(default_factory=list)
    checks: list = field(default_factory=list)


class VendorDriver(ABC):
    """Abstract base class for vendor device drivers.

    Each vendor implements this interface to provide device communication,
    firmware updates, and health checks. The orchestration layer (app.py,
    poller.py) uses this interface without knowing vendor-specific details.
    """

    # Class-level metadata — subclasses must set these
    VENDOR_ID: str = ""
    VENDOR_NAME: str = ""
    SUPPORTED_PROTOCOLS: List[str] = []
    HAS_DUAL_BANK: bool = False
    HAS_CPE_DISCOVERY: bool = False

    def __init__(self, ip: str, username: str, password: str, timeout: int = 30):
        self.ip = ip
        self.username = username
        self.password = password
        self.timeout = timeout

    @abstractmethod
    async def connect(self):
        """Authenticate/connect to the device.

        Returns:
            True on success, or an error string on failure.
        """
        ...

    @abstractmethod
    async def get_device_info(self) -> VendorDeviceInfo:
        """Get device identity, model, and firmware version."""
        ...

    @abstractmethod
    async def upload_firmware(self, firmware_path: str, bandwidth_limit_kbps: int = 0) -> bool:
        """Upload firmware binary to the device."""
        ...

    @abstractmethod
    async def trigger_update(self) -> bool:
        """Tell the device to install uploaded firmware (may trigger reboot)."""
        ...

    @abstractmethod
    async def wait_for_reboot(self, timeout: int = 300) -> bool:
        """Wait for device to come back online after reboot."""
        ...

    async def reboot(self, timeout: int = 300) -> bool:
        """Reboot without firmware update. Override per vendor."""
        raise NotImplementedError(f"{self.VENDOR_NAME} driver does not support standalone reboot")

    @abstractmethod
    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None,
        pass_number: int = 1,
        reboot_timeout: int = 300,
        bandwidth_limit_kbps: int = 0,
    ) -> VendorUpdateResult:
        """Perform the full update cycle: connect, upload, install, verify."""
        ...

    @abstractmethod
    async def run_smoke_tests(
        self, role: str = "ap", pre_update_cpe_count: int = 0
    ) -> VendorSmokeTestResult:
        """Post-update health check."""
        ...

    # --- Optional methods with default implementations ---

    async def get_connected_cpes(self) -> list:
        """Get subscriber devices connected to this AP.

        Only meaningful for vendors with CPE topology (HAS_CPE_DISCOVERY=True).
        """
        return []

    async def get_ap_info(self) -> Dict[str, Any]:
        """Get extended AP information (location, zone, etc.).

        Default implementation delegates to get_device_info().
        """
        info = await self.get_device_info()
        return {
            "ip": info.ip,
            "mac": info.mac,
            "system_name": None,
            "model": info.model,
            "firmware_version": info.current_version,
            "location": info.extra.get("location"),
        }

    async def get_config(self) -> Optional[dict]:
        """Download device configuration. Returns None if unsupported."""
        return None

    async def apply_config(self, config: dict, dry_run: bool = False) -> dict:
        """Apply configuration to device."""
        return {"success": False, "error": f"Config push not supported by {self.VENDOR_NAME}"}

    # --- Firmware metadata ---

    @abstractmethod
    def get_firmware_types(self) -> List[Dict[str, str]]:
        """Return firmware type definitions for this vendor.

        Each dict: {"key": "tna-30x", "label": "TNA-30x", "pattern": r"tna-30x.*\\.bin"}
        """
        ...

    @abstractmethod
    def validate_firmware_for_model(self, firmware_path: str, model: str) -> tuple:
        """Check if firmware file is compatible with device model.

        Returns:
            (is_valid: bool, error_message: str)
        """
        ...

    @abstractmethod
    def extract_version_from_firmware(self, firmware_path: str) -> str:
        """Parse version string from firmware filename."""
        ...

    def get_reboot_timeout(self, role: str = "ap") -> int:
        """Vendor-specific reboot timeout in seconds."""
        return 300

    def get_hardware_id(self, model: str) -> str:
        """Get hardware ID for config downloads. Override per vendor."""
        return ""

    def select_firmware_for_model(self, model: Optional[str], firmware_files: Dict[str, str]) -> Optional[str]:
        """Select the correct firmware path for a device model.

        Default implementation returns the first available firmware file.
        Override per vendor for model-specific firmware selection.
        """
        if not firmware_files:
            return None
        return next(iter(firmware_files.values()), None)

    def get_firmware_type_for_model(self, model: Optional[str]) -> Optional[str]:
        """Get the firmware type key for a device model.

        Default implementation returns the first firmware type key.
        Override per vendor for model-specific type mapping.
        """
        types = self.get_firmware_types()
        return types[0]["key"] if types else None
