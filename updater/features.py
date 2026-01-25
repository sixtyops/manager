"""Feature definitions and stability classification for SixtyOps.

All features are unlocked. Some are marked 'dangerous' (untested /
high-impact) so the UI can show a warning badge.
"""

import logging
import uuid
from enum import Enum

from . import database as db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature enum (kept identical to the old license.py for import compat)
# ---------------------------------------------------------------------------

class Feature(str, Enum):
    """All available features."""
    UPDATE_SINGLE_DEVICE = "update_single_device"
    SSO_OIDC = "sso_oidc"
    RADIUS_AUTH = "radius_auth"
    CONFIG_BACKUP = "config_backup"
    CONFIG_TEMPLATES = "config_templates"
    CONFIG_COMPLIANCE = "config_compliance"
    CONFIG_PUSH = "config_push"
    SLACK_NOTIFICATIONS = "slack_notifications"
    DEVICE_PORTAL = "device_portal"
    DEVICE_HISTORY = "device_history"
    TOWER_SITES = "tower_sites"
    BETA_FIRMWARE = "beta_firmware"
    FIRMWARE_HOLD_CUSTOM = "firmware_hold_custom"
    SNMP_TRAPS = "snmp_traps"
    WEBHOOKS = "webhooks"


# Features classified as dangerous (untested / high-impact operations)
DANGEROUS_FEATURES: set[Feature] = {
    Feature.CONFIG_BACKUP,
    Feature.CONFIG_TEMPLATES,
    Feature.CONFIG_COMPLIANCE,
    Feature.CONFIG_PUSH,
    Feature.RADIUS_AUTH,
    Feature.SSO_OIDC,
}

_FEATURE_DISPLAY_NAMES: dict[str, str] = {
    "update_single_device": "Manual updates",
    "config_backup": "Config backup",
    "config_templates": "Config templates",
    "config_compliance": "Config compliance",
    "config_push": "Config push",
    "device_history": "Update history",
    "beta_firmware": "Beta firmware",
    "firmware_hold_custom": "Custom firmware hold",
    "slack_notifications": "Slack notifications",
    "sso_oidc": "SSO / OIDC",
    "device_portal": "Device portal",
    "tower_sites": "Tower sites",
    "radius_auth": "RADIUS authentication",
    "snmp_traps": "SNMP traps",
    "webhooks": "Webhooks",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_feature_enabled(feature: Feature) -> bool:
    """All features are always enabled."""
    return True


def is_dangerous(feature: Feature) -> bool:
    """Return True if the feature is classified as dangerous."""
    return feature in DANGEROUS_FEATURES


def get_feature_map() -> dict:
    """Return feature info for the frontend."""
    return {
        f.value: {
            "enabled": True,
            "dangerous": f in DANGEROUS_FEATURES,
            "name": _FEATURE_DISPLAY_NAMES.get(f.value, f.value),
        }
        for f in Feature
    }


def get_instance_id() -> str:
    """Get or create a persistent instance ID (UUID4) for this installation."""
    iid = db.get_setting("instance_id", "")
    if iid:
        return iid
    iid = str(uuid.uuid4())
    db.set_setting("instance_id", iid)
    return iid


# ---------------------------------------------------------------------------
# No-op FastAPI dependencies (backward compat for 60+ endpoint signatures)
# ---------------------------------------------------------------------------

async def require_pro():
    """No-op. Kept for backward compatibility."""
    pass


def require_feature(feature: Feature):
    """No-op dependency factory. All features are unlocked."""
    async def _noop():
        pass
    return _noop


# ---------------------------------------------------------------------------
# Compat stubs so code that imports old names doesn't break
# ---------------------------------------------------------------------------

def get_license_state():
    """Compat stub — returns a dict that looks like an always-active license."""
    return _CompatLicenseState()


def get_nag_info() -> dict:
    return {"billable_count": 0, "threshold": 0, "should_nag": False, "is_pro": True}


def get_billable_device_count() -> int:
    return 0


async def validate_license(license_key: str = None):
    return get_license_state()


def clear_license():
    pass


def init_license_validator(broadcast_func=None):
    return _NoopValidator()


class _CompatLicenseState:
    """Minimal stand-in for old LicenseState dataclass."""
    tier = "pro"
    status = "active"
    license_key = ""
    customer_name = ""
    expires_at = None
    last_validated = None
    grace_until = None
    device_limit = None
    error = ""

    def is_pro(self):
        return True

    def is_feature_enabled(self, feature):
        return True

    def to_dict(self):
        return {
            "tier": "pro",
            "status": "active",
            "has_key": False,
            "customer_name": "",
            "expires_at": None,
            "last_validated": None,
            "grace_until": None,
            "device_limit": None,
            "error": "",
            "is_pro": True,
        }


class _NoopValidator:
    async def start(self):
        pass

    async def stop(self):
        pass
