"""Backward-compatibility shim for Tachyon client imports.

The actual implementation has moved to updater/vendors/tachyon/client.py.
This module re-exports the public API so existing imports continue to work.
"""

from .vendors.tachyon.client import (  # noqa: F401
    TachyonClient,
    DeviceInfo,
    UpdateResult,
    SmokeTestResult,
    _extract_version_from_firmware,
    _normalize_version,
    VERIFY_SSL,
)
