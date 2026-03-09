"""Vendor driver registry.

Provides registration and lookup for vendor device drivers.
Each vendor package registers itself at import time.
"""

import logging
from typing import Dict, List, Type

from .base import VendorDriver, VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult

logger = logging.getLogger(__name__)

_DRIVERS: Dict[str, Type[VendorDriver]] = {}


def register_driver(vendor_id: str, driver_class: Type[VendorDriver]):
    """Register a vendor driver class."""
    if vendor_id in _DRIVERS:
        logger.warning(f"Overwriting existing driver for vendor '{vendor_id}'")
    _DRIVERS[vendor_id] = driver_class
    logger.info(f"Registered vendor driver: {vendor_id} ({driver_class.VENDOR_NAME})")


def get_driver(vendor_id: str) -> Type[VendorDriver]:
    """Get a vendor driver class by ID.

    Raises:
        KeyError: If vendor_id is not registered.
    """
    if vendor_id not in _DRIVERS:
        available = ", ".join(_DRIVERS.keys()) or "(none)"
        raise KeyError(f"Unknown vendor '{vendor_id}'. Available: {available}")
    return _DRIVERS[vendor_id]


def list_vendors() -> List[dict]:
    """Return metadata for all registered vendors."""
    vendors = []
    for vendor_id, driver_class in _DRIVERS.items():
        # Instantiate temporarily to get firmware types
        try:
            firmware_types = driver_class.__dict__.get("_FIRMWARE_TYPES", [])
            if not firmware_types and hasattr(driver_class, "get_firmware_types"):
                # Try calling as classmethod-style if defined at class level
                instance = object.__new__(driver_class)
                firmware_types = instance.get_firmware_types()
        except Exception:
            firmware_types = []

        vendors.append({
            "id": vendor_id,
            "name": driver_class.VENDOR_NAME,
            "protocols": driver_class.SUPPORTED_PROTOCOLS,
            "has_dual_bank": driver_class.HAS_DUAL_BANK,
            "has_cpe_discovery": driver_class.HAS_CPE_DISCOVERY,
            "firmware_types": firmware_types,
        })
    return vendors


def init_vendors():
    """Import all vendor packages to trigger registration."""
    from . import tachyon  # noqa: F401
    from . import mikrotik  # noqa: F401
    from . import cambium  # noqa: F401


__all__ = [
    "VendorDriver",
    "VendorDeviceInfo",
    "VendorUpdateResult",
    "VendorSmokeTestResult",
    "register_driver",
    "get_driver",
    "list_vendors",
    "init_vendors",
]
