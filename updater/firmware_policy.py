"""Shared firmware target and device-state policy.

This module is the single place for decisions that must not drift between the
fetcher, fleet-status API, scheduler, and manual update routes: which firmware
files are deployable, how versions compare, and what a device's target state
means.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)

PLATFORM_SETTING_KEYS = {
    "tna-30x": "selected_firmware_30x",
    "tna-303l": "selected_firmware_303l",
    "tns-100": "selected_firmware_tns100",
}

FIRMWARE_EXTENSIONS = {".bin", ".img", ".npk", ".tar", ".gz"}
MIN_PLAUSIBLE_SIZE_FRACTION = 0.5


def pin_setting_key(setting_key: str) -> str:
    return f"{setting_key}_pinned"


def detect_platform(filename: str) -> str:
    lower = filename.lower()
    if "tna-303l" in lower or "tna303l" in lower:
        return "tna-303l"
    if "tna-30x" in lower or "tna30x" in lower:
        return "tna-30x"
    if "tns-100" in lower or "tns100" in lower:
        return "tns-100"
    return "unknown"


def extract_version_from_filename(filename: str) -> str:
    if not filename:
        return ""
    match = re.search(
        r"(?:tna-30x|tna30x|tna-303l|tna303l|tns-100|tns100|tns)-(\d+\.\d+\.\d+)-r(\d+)",
        filename,
        re.IGNORECASE,
    )
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"(\d+\.\d+\.\d+)(?:-r(\d+))?", filename)
    if match:
        if match.group(2):
            return f"{match.group(1)}.{match.group(2)}"
        return match.group(1)
    return ""


def parse_version(version: str) -> tuple[int, ...]:
    if not version:
        return (0,)
    normalized = str(version).replace("-", ".").replace(".r", ".").lower()
    parts: list[int] = []
    for seg in normalized.split("."):
        seg = seg.lstrip("r")
        try:
            parts.append(int(seg))
        except ValueError:
            continue
    return tuple(parts) if parts else (0,)


def compare_versions(a: str, b: str) -> int:
    pa = list(parse_version(a))
    pb = list(parse_version(b))
    while len(pa) < len(pb):
        pa.append(0)
    while len(pb) < len(pa):
        pb.append(0)
    for left, right in zip(pa, pb):
        if left != right:
            return left - right
    return 0


@dataclass(frozen=True)
class FirmwareFileHealth:
    filename: str
    platform: str
    version: str
    exists: bool
    incomplete: bool
    duplicate: bool
    verified: bool
    deployable: bool
    reason: str = ""


@dataclass(frozen=True)
class DeviceFirmwareState:
    status: str
    update_state: str
    needs_update: bool
    action: str
    label: str
    reason: str = ""


def _version_counts(paths: list[Path]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for path in paths:
        key = (detect_platform(path.name), extract_version_from_filename(path.name))
        counts[key] = counts.get(key, 0) + 1
    return counts


def firmware_file_health(firmware_dir: Path, filename: str) -> FirmwareFileHealth:
    path = firmware_dir / Path(filename).name
    platform = detect_platform(filename)
    version = extract_version_from_filename(filename)
    exists = path.exists() and path.is_file() and path.suffix in FIRMWARE_EXTENSIONS

    firmware_paths = [
        p for p in firmware_dir.iterdir()
        if p.is_file() and p.suffix in FIRMWARE_EXTENSIONS
    ] if firmware_dir.exists() else []
    sibling_max = 0
    for sibling in firmware_paths:
        if sibling.name == path.name:
            continue
        if detect_platform(sibling.name) == platform:
            sibling_max = max(sibling_max, sibling.stat().st_size)

    size = path.stat().st_size if exists else 0
    incomplete = bool(sibling_max and size < sibling_max * MIN_PLAUSIBLE_SIZE_FRACTION)
    counts = _version_counts(firmware_paths)
    duplicate = bool(version and counts.get((platform, version), 0) > 1)
    registry = {r["filename"]: r for r in db.get_firmware_registry()}
    reg = registry.get(path.name)
    # A NULL hash in an existing registry row means this file has not been
    # fingerprinted by the current integrity path. A file with no registry row is
    # allowed so legacy/manual-on-disk installs do not become unusable on upgrade.
    verified = bool(exists and (reg is None or reg.get("sha256")))

    reason = ""
    if not exists:
        reason = "file_missing"
    elif incomplete:
        reason = "file_incomplete"
    elif not verified:
        reason = "file_unverified"

    return FirmwareFileHealth(
        filename=path.name,
        platform=platform,
        version=version,
        exists=exists,
        incomplete=incomplete,
        duplicate=duplicate,
        verified=verified,
        deployable=exists and not incomplete and verified,
        reason=reason,
    )


def annotate_firmware_health(files: list[dict], firmware_dir: Path) -> None:
    platform_max: dict[str, int] = {}
    version_counts: dict[tuple[str, str], int] = {}
    for file_info in files:
        platform = detect_platform(file_info["name"])
        version = extract_version_from_filename(file_info["name"])
        platform_max[platform] = max(platform_max.get(platform, 0), file_info.get("size", 0))
        version_counts[(platform, version)] = version_counts.get((platform, version), 0) + 1

    for file_info in files:
        h = firmware_file_health(firmware_dir, file_info["name"])
        platform = detect_platform(file_info["name"])
        version = extract_version_from_filename(file_info["name"])
        sibling_max = platform_max.get(platform, 0)
        incomplete = bool(
            sibling_max
            and file_info.get("size", 0) < sibling_max * MIN_PLAUSIBLE_SIZE_FRACTION
        )
        duplicate = bool(version and version_counts.get((platform, version), 0) > 1)
        file_info["incomplete"] = incomplete
        file_info["duplicate"] = duplicate
        file_info["verified"] = h.verified
        file_info["deployable"] = h.exists and not incomplete and h.verified
        if not h.exists:
            reason = "file_missing"
        elif incomplete:
            reason = "file_incomplete"
        elif not h.verified:
            reason = "file_unverified"
        else:
            reason = ""
        file_info["health_reason"] = reason


def _get_channel_map() -> dict[str, str]:
    raw = db.get_setting("firmware_channels", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def select_auto_target(
    platform: str,
    firmware_dir: Path,
    beta_enabled: bool,
    releases: Optional[list] = None,
) -> Optional[str]:
    """Return the safest auto target filename for a platform.

    Beta-enabled tracks the highest deployable beta if one exists, otherwise the
    highest deployable stable. Beta-disabled tracks stable. Within a channel, the
    decision is version-based, not modified-time or discovery order.
    """
    candidates = []
    if releases is None:
        channel_map = _get_channel_map()
        for filename, channel in channel_map.items():
            if detect_platform(filename) == platform:
                candidates.append({"filename": filename, "channel": channel})
    else:
        for release in releases:
            filename = getattr(release, "filename", "")
            channel = getattr(release, "channel", "")
            if detect_platform(filename) == platform or getattr(release, "platform", "") == platform:
                candidates.append({"filename": filename, "channel": channel})

    def deployable_for_channel(channel: str) -> list[dict]:
        return [
            c for c in candidates
            if c["channel"] == channel
            and firmware_file_health(firmware_dir, c["filename"]).deployable
        ]

    allowed = deployable_for_channel("beta") if beta_enabled else []
    if not allowed:
        allowed = deployable_for_channel("stable")
    if not allowed:
        return None

    allowed.sort(
        key=lambda c: parse_version(extract_version_from_filename(c["filename"])),
        reverse=True,
    )
    return allowed[0]["filename"]


def auto_select_platform_target(
    platform: str,
    firmware_dir: Path,
    beta_enabled: bool,
    releases: Optional[list] = None,
) -> Optional[str]:
    """Persist a safe auto target for one platform.

    Missing pins are cleared. Auto-tracked targets never move backward while the
    current target is still deployable; this prevents a missing new download
    from silently pivoting the fleet to an older build.
    """
    setting_key = PLATFORM_SETTING_KEYS.get(platform)
    if not setting_key:
        return None

    pin_key = pin_setting_key(setting_key)
    pinned = db.get_setting(pin_key, "false") == "true"
    current = (db.get_setting(setting_key, "") or "").strip()
    if pinned:
        if current and firmware_file_health(firmware_dir, current).deployable:
            return current
        db.set_setting(pin_key, "false")
        pinned = False
        logger.warning(
            "Pinned firmware %r for %s is not deployable; reverting to auto-select",
            current,
            setting_key,
        )

    best = select_auto_target(platform, firmware_dir, beta_enabled, releases=releases)
    if not best:
        return current or None

    if current:
        current_health = firmware_file_health(firmware_dir, current)
        current_version = extract_version_from_filename(current)
        best_version = extract_version_from_filename(best)
        if (
            current_health.deployable
            and current_version
            and best_version
            and compare_versions(current_version, best_version) > 0
        ):
            logger.warning(
                "Holding %s at newer deployable target %s instead of auto-selecting older %s",
                setting_key,
                current,
                best,
            )
            return current

    if current != best:
        db.set_setting(setting_key, best)
        logger.info("Auto-selected %s = %s", setting_key, best)
    return best


def classify_device_version(
    current: Optional[str],
    target: str,
    allow_downgrade: bool = False,
) -> DeviceFirmwareState:
    if not target:
        return DeviceFirmwareState(
            status="unknown",
            update_state="missing_target",
            needs_update=False,
            action="none",
            label="No target",
            reason="missing_target",
        )
    if target == "__unknown__":
        return DeviceFirmwareState(
            status="unknown",
            update_state="needs_update",
            needs_update=True,
            action="update",
            label="Needs update",
            reason="unknown_target_version",
        )
    if not current:
        return DeviceFirmwareState(
            status="unknown",
            update_state="needs_update",
            needs_update=True,
            action="update",
            label="Needs update",
            reason="unknown_current_version",
        )

    cmp = compare_versions(str(current).replace(".r", "."), target)
    if cmp == 0:
        return DeviceFirmwareState("current", "up_to_date", False, "none", "Current")
    if cmp < 0:
        return DeviceFirmwareState("behind", "needs_update", True, "update", "Needs update")
    if allow_downgrade:
        return DeviceFirmwareState(
            "ahead",
            "downgrade_available",
            True,
            "downgrade",
            "On newer firmware",
            "downgrade_allowed",
        )
    return DeviceFirmwareState(
        "ahead",
        "up_to_date",
        False,
        "none",
        "On newer firmware",
        "newer_than_target",
    )
