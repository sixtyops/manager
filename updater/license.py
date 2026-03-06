"""License management and feature gating for SixtyOps."""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

import httpx

from . import database as db
from . import __version__

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature definitions
# ---------------------------------------------------------------------------

class Feature(str, Enum):
    """Gated features that require a PRO license.

    Any feature NOT listed here is free by default.
    """
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
    RADIUS_SERVER = "radius_server"
    SNMP_TRAPS = "snmp_traps"


class LicenseTier(str, Enum):
    FREE = "free"
    PRO = "pro"


class LicenseStatus(str, Enum):
    FREE = "free"
    ACTIVE = "active"
    EXPIRED = "expired"
    GRACE = "grace"
    INVALID = "invalid"


# All Feature members require PRO
PRO_FEATURES = set(Feature)

# Soft threshold for free-tier nag banner
FREE_DEVICE_NAG_THRESHOLD = 10

# Grace period when license server is unreachable (days)
GRACE_PERIOD_DAYS = 7

# Re-validation interval (seconds) — every 24 hours
VALIDATION_INTERVAL = int(os.environ.get("LICENSE_CHECK_INTERVAL", 86400))

# License server URL (configurable via env for dev/testing)
LICENSE_SERVER_URL = os.environ.get(
    "LICENSE_SERVER_URL",
    "https://license.sixtyops.net/api/v1",
)

# Dev override: TACHYON_FORCE_PRO=1 bypasses all gating (disabled in appliance mode)
_APPLIANCE_MODE = os.environ.get("TACHYON_APPLIANCE", "") == "1"
_FORCE_PRO = (
    not _APPLIANCE_MODE
    and os.environ.get("TACHYON_FORCE_PRO", "").lower() in ("1", "true", "yes")
)


# ---------------------------------------------------------------------------
# License state
# ---------------------------------------------------------------------------

@dataclass
class LicenseState:
    """Cached license state. Persisted in settings for offline resilience."""
    tier: LicenseTier = LicenseTier.FREE
    status: LicenseStatus = LicenseStatus.FREE
    license_key: str = ""
    customer_name: str = ""
    expires_at: Optional[str] = None
    last_validated: Optional[str] = None
    grace_until: Optional[str] = None
    device_limit: Optional[int] = None
    error: str = ""

    def is_pro(self) -> bool:
        return self.status in (LicenseStatus.ACTIVE, LicenseStatus.GRACE)

    def is_feature_enabled(self, feature: Feature) -> bool:
        if feature not in PRO_FEATURES:
            return True
        return self.is_pro()

    def to_dict(self) -> dict:
        """Safe dict for API responses (license key redacted)."""
        return {
            "tier": self.tier.value,
            "status": self.status.value,
            "has_key": bool(self.license_key),
            "customer_name": self.customer_name,
            "expires_at": self.expires_at,
            "last_validated": self.last_validated,
            "grace_until": self.grace_until,
            "device_limit": self.device_limit,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Global singleton + DB persistence
# ---------------------------------------------------------------------------

_license_state: Optional[LicenseState] = None


def _load_from_db() -> LicenseState:
    """Reconstruct license state from settings table."""
    # Run one-time migration for existing deployments
    _check_migration()

    key = db.get_setting("license_key", "")
    if not key:
        return LicenseState()

    status_str = db.get_setting("license_status", "free")
    try:
        status = LicenseStatus(status_str)
    except ValueError:
        status = LicenseStatus.FREE

    # Check if grace period has expired
    grace_until = db.get_setting("license_grace_until", "")
    if status == LicenseStatus.GRACE and grace_until:
        if datetime.now().isoformat() > grace_until:
            status = LicenseStatus.EXPIRED

    tier = LicenseTier.PRO if status in (LicenseStatus.ACTIVE, LicenseStatus.GRACE) else LicenseTier.FREE
    device_limit_str = db.get_setting("license_device_limit", "0")

    return LicenseState(
        tier=tier,
        status=status,
        license_key=key,
        customer_name=db.get_setting("license_customer_name", ""),
        expires_at=db.get_setting("license_expires_at", "") or None,
        last_validated=db.get_setting("license_last_validated", "") or None,
        grace_until=grace_until or None,
        device_limit=int(device_limit_str) if device_limit_str and device_limit_str != "0" else None,
        error=db.get_setting("license_error", ""),
    )


def _save_to_db(state: LicenseState):
    """Persist license state to settings."""
    with db.get_db() as conn:
        pairs = {
            "license_key": state.license_key,
            "license_status": state.status.value,
            "license_customer_name": state.customer_name,
            "license_expires_at": state.expires_at or "",
            "license_last_validated": state.last_validated or "",
            "license_grace_until": state.grace_until or "",
            "license_device_limit": str(state.device_limit or 0),
            "license_error": state.error,
        }
        for k, v in pairs.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, datetime.now().isoformat()),
            )


def get_license_state() -> LicenseState:
    """Get the current license state (from cache, never blocks)."""
    global _license_state
    if _license_state is None:
        _license_state = _load_from_db()
    return _license_state


def is_feature_enabled(feature: Feature) -> bool:
    """Quick check — no DB call if cache is warm."""
    if _FORCE_PRO:
        return True
    state = get_license_state()
    # Runtime grace expiry check: if grace period has passed, expire it now
    if state.status == LicenseStatus.GRACE and state.grace_until:
        if datetime.now().isoformat() > state.grace_until:
            global _license_state
            state.status = LicenseStatus.EXPIRED
            state.tier = LicenseTier.FREE
            state.error = "Grace period expired. Please restore internet connectivity."
            _license_state = state
            _save_to_db(state)
    return state.is_feature_enabled(feature)


# ---------------------------------------------------------------------------
# Device counting
# ---------------------------------------------------------------------------

def get_billable_device_count() -> int:
    """Count APs + switches (enabled). CPEs are free."""
    with db.get_db() as conn:
        ap_count = conn.execute(
            "SELECT COUNT(*) FROM access_points WHERE enabled = 1"
        ).fetchone()[0]
        sw_count = conn.execute(
            "SELECT COUNT(*) FROM switches WHERE enabled = 1"
        ).fetchone()[0]
    return ap_count + sw_count


def get_nag_info() -> dict:
    """Get device threshold nag info for free-tier users."""
    state = get_license_state()
    count = get_billable_device_count()
    return {
        "billable_count": count,
        "threshold": FREE_DEVICE_NAG_THRESHOLD,
        "should_nag": not state.is_pro() and count > FREE_DEVICE_NAG_THRESHOLD,
        "is_pro": state.is_pro(),
    }


# ---------------------------------------------------------------------------
# License validation (remote server)
# ---------------------------------------------------------------------------

async def validate_license(license_key: str = None) -> LicenseState:
    """Validate license key with the remote server.

    If license_key is provided, it's a new activation.
    If None, re-validates the existing stored key.
    """
    global _license_state

    key = license_key or db.get_setting("license_key", "")
    if not key:
        _license_state = LicenseState()
        _save_to_db(_license_state)
        return _license_state

    # Normalize: uppercase, strip whitespace (server enforces XXXX-XXXX-XXXX-XXXX)
    key = key.strip().upper()

    device_count = get_billable_device_count()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{LICENSE_SERVER_URL}/validate",
                json={
                    "license_key": key,
                    "device_count": device_count,
                    "app_version": __version__,
                },
            )
            # 422 = malformed key (validation error), treat as invalid not server outage
            if resp.status_code == 422:
                _license_state = LicenseState(
                    tier=LicenseTier.FREE,
                    status=LicenseStatus.INVALID,
                    license_key=key,
                    error="Invalid license key format. Expected XXXX-XXXX-XXXX-XXXX.",
                )
                _save_to_db(_license_state)
                return _license_state
            resp.raise_for_status()
            data = resp.json()

        is_valid = data.get("valid", False)
        _license_state = LicenseState(
            tier=LicenseTier.PRO if is_valid else LicenseTier.FREE,
            status=LicenseStatus.ACTIVE if is_valid else LicenseStatus.INVALID,
            license_key=key,
            customer_name=data.get("customer_name", ""),
            expires_at=data.get("expires_at"),
            last_validated=datetime.now().isoformat(),
            device_limit=data.get("device_limit"),
            error="" if is_valid else data.get("error", "License invalid"),
        )

    except (httpx.HTTPError, httpx.TimeoutException, Exception) as e:
        logger.warning(f"License server unreachable: {e}")
        old_state = get_license_state()

        if old_state.status == LicenseStatus.ACTIVE:
            # Was active, entering grace period
            grace_deadline = (datetime.now() + timedelta(days=GRACE_PERIOD_DAYS)).isoformat()
            _license_state = LicenseState(
                tier=LicenseTier.PRO,
                status=LicenseStatus.GRACE,
                license_key=key,
                customer_name=old_state.customer_name,
                expires_at=old_state.expires_at,
                last_validated=old_state.last_validated,
                grace_until=grace_deadline,
                device_limit=old_state.device_limit,
                error=f"License server unreachable. Grace period until {grace_deadline[:10]}.",
            )
        elif old_state.status == LicenseStatus.GRACE:
            # Already in grace — check if expired
            if old_state.grace_until and datetime.now().isoformat() > old_state.grace_until:
                _license_state = LicenseState(
                    tier=LicenseTier.FREE,
                    status=LicenseStatus.EXPIRED,
                    license_key=key,
                    customer_name=old_state.customer_name,
                    error="Grace period expired. Please restore internet connectivity.",
                )
            else:
                _license_state = old_state  # Keep existing grace state
        else:
            # No prior valid state
            _license_state = LicenseState(
                tier=LicenseTier.FREE,
                status=LicenseStatus.INVALID,
                license_key=key,
                error=f"Could not validate license: {e}",
            )

    _save_to_db(_license_state)
    return _license_state


def clear_license():
    """Remove license key and revert to free tier."""
    global _license_state
    _license_state = LicenseState()
    _save_to_db(_license_state)


# ---------------------------------------------------------------------------
# Migration for existing deployments
# ---------------------------------------------------------------------------

_migration_checked = False  # In-memory guard prevents re-trigger even if DB flag deleted


def _check_migration():
    """One-time migration: grant existing deployments a 30-day PRO trial."""
    global _migration_checked
    if _migration_checked:
        return

    migrated = db.get_setting("license_migration_v1", "")
    if migrated:
        _migration_checked = True
        return

    _migration_checked = True  # Set BEFORE any logic to prevent re-entry

    setup_done = db.get_setting("setup_completed", "false") == "true"
    if not setup_done:
        db.set_setting("license_migration_v1", "done")
        return

    # If a license key already exists, no migration needed
    existing_key = db.get_setting("license_key", "")
    if existing_key:
        db.set_setting("license_migration_v1", "done")
        return

    device_count = get_billable_device_count()
    has_oidc = db.get_setting("oidc_enabled", "") == "true"
    has_slack = bool(db.get_setting("slack_webhook_url", ""))

    if device_count > 0 or has_oidc or has_slack:
        # Existing deployment with features in use — grant 30-day trial
        grace_until = (datetime.now() + timedelta(days=30)).isoformat()
        state = LicenseState(
            tier=LicenseTier.PRO,
            status=LicenseStatus.GRACE,
            grace_until=grace_until,
            customer_name="Migration trial",
        )
        _save_to_db(state)
        logger.info(f"Existing deployment detected. Granted 30-day PRO trial until {grace_until[:10]}")

    db.set_setting("license_migration_v1", "done")


# ---------------------------------------------------------------------------
# Background validator
# ---------------------------------------------------------------------------

_validator: Optional["LicenseValidator"] = None


class LicenseValidator:
    """Background service that periodically re-validates the license."""

    def __init__(self, broadcast_func: Optional[Callable] = None):
        self.broadcast_func = broadcast_func
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._validate_loop())
        logger.info("License validator started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("License validator stopped")

    async def _validate_loop(self):
        # Initial validation on startup
        state = get_license_state()
        if state.license_key:
            await validate_license()
            await self._broadcast_state()

        while self._running:
            await asyncio.sleep(VALIDATION_INTERVAL)
            state = get_license_state()
            if state.license_key:
                try:
                    await validate_license()
                    await self._broadcast_state()
                except Exception as e:
                    logger.exception(f"License validation error: {e}")

    async def _broadcast_state(self):
        if self.broadcast_func:
            await self.broadcast_func({
                "type": "license_state",
                **get_license_state().to_dict(),
                **get_nag_info(),
            })


def init_license_validator(broadcast_func: Optional[Callable] = None) -> LicenseValidator:
    global _validator
    _validator = LicenseValidator(broadcast_func)
    return _validator


def get_license_validator() -> Optional[LicenseValidator]:
    return _validator


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

from fastapi import HTTPException

_FEATURE_DISPLAY_NAMES = {
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
    "radius_server": "RADIUS server",
    "snmp_traps": "SNMP traps",
}


async def require_pro():
    """FastAPI dependency: require any active PRO license."""
    if _FORCE_PRO:
        return
    state = get_license_state()
    if not state.is_pro():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "pro_required",
                "message": "This feature requires a Pro license.",
                "upgrade_url": "https://sixtyops.net/#pricing",
            },
        )


def require_feature(feature: Feature):
    """Factory: returns a FastAPI dependency for a specific feature."""
    async def _check():
        if _FORCE_PRO:
            return
        if not is_feature_enabled(feature):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "feature_locked",
                    "feature": feature.value,
                    "message": f"The '{feature.value}' feature requires a Pro license.",
                    "upgrade_url": "https://sixtyops.net/#pricing",
                },
            )
    return _check
