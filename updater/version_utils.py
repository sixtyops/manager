"""Canonical firmware-version parsing and comparison.

Historically this logic was copy-pasted into `app.py`, `scheduler.py`, and the
Tachyon vendor client, with a comment in the scheduler asking callers to "keep
the two in sync". They drifted (the vendor copy silently drops the switch
revision). This module is the single source of truth; the duplicates import from
here.

Firmware versions arrive in a few shapes that must compare equal:
``1.12.4.7782`` (dotted), ``1.12.4.r7782`` (device-reported), and the build
number embedded in a filename as ``-r7782``. Comparison is purely numeric on the
version components — never on file dates or list order.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def extract_version_from_filename(filename: str) -> str:
    """Extract a normalized dotted version from a firmware filename.

    ``tna-30x-2.5.1-r54970.bin`` -> ``2.5.1.54970``
    ``tns-1.12.8-r54729-...-tns-100-...bin`` -> ``1.12.8.54729``

    Tachyon switch firmware uses a bare ``tns-`` prefix (no model number) where
    APs use ``tna-30x-`` / ``tna-303l-``. Without the ``tns`` alternate the
    primary regex misses the switch convention, the fallback drops the revision,
    and a device then looks "ahead" of target once ``allow_downgrade`` is on
    (the PR #92 bug).
    """
    if not filename:
        return ""
    name = Path(filename).name
    match = re.search(
        r"(?:tna-30x|tna30x|tna-303l|tna303l|tns-100|tns100|tns)-(\d+\.\d+\.\d+)-r(\d+)",
        name,
        re.IGNORECASE,
    )
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    # Fallback: capture the revision too when present, so a filename convention
    # we don't yet recognize doesn't silently lose the build number.
    match2 = re.search(r"(\d+\.\d+\.\d+)(?:-r(\d+))?", name)
    if match2:
        if match2.group(2):
            return f"{match2.group(1)}.{match2.group(2)}"
        return match2.group(1)
    return ""


def parse_version(version: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison.

    Handles ``1.12.3.54970`` and the device-reported ``1.12.3.r54970``.
    """
    if not version:
        return (0,)
    normalized = version.replace(".r", ".")
    parts = []
    for part in normalized.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def compare_versions(a: str, b: str) -> int:
    """Compare two version strings. Returns <0 if a<b, 0 if equal, >0 if a>b."""
    def parts(v):
        if not v:
            return [0]
        # Normalize: "1.12.2-r54885" -> "1.12.2.54885"
        v = v.replace("-", ".").lower()
        result = []
        for seg in v.split("."):
            seg = seg.lstrip("r")  # strip revision prefix
            try:
                result.append(int(seg))
            except ValueError:
                continue  # skip non-numeric segments
        return result or [0]
    pa, pb = parts(a), parts(b)
    while len(pa) < len(pb):
        pa.append(0)
    while len(pb) < len(pa):
        pb.append(0)
    for x, y in zip(pa, pb):
        if x != y:
            return x - y
    return 0


def normalize_version(value: Optional[str]) -> str:
    """Normalize firmware version strings for equality comparisons."""
    if not value:
        return ""
    return str(value).strip().replace(".r", ".")


# --- Device-vs-target status: the single source of truth ----------------------
#
# Historically six functions answered "is this device on target / does it need
# an update", spread across app.py and scheduler.py, disagreeing on compare
# basis, bank mode, cooldown, and missing-target handling. They are consolidated
# here as named *policies* that share these version primitives and one canonical
# bank derivation. The policies still differ — that divergence is load-bearing
# and deliberate, not accidental:
#
#   * PARSED compare for display/eligibility ("1.15.0.r8515" == "1.15.0.8515";
#     "newer than target" must be detectable).
#   * EXACT-STRING compare for skip/no-op gates (only skip a flash/reboot when
#     the device is *provably* on that exact build; when in doubt, push).
#
# tests/test_status_core.py pins the exact behavior of every policy. The old
# app.py/scheduler.py names remain as thin shims delegating here.


def version_state(current: Optional[str], target: str,
                  allow_downgrade: bool = False) -> str:
    """Parsed status of one version vs target: 'current' | 'behind' | 'unknown'.

    A device newer than target is 'behind' only when downgrades are allowed;
    otherwise 'current'. Missing either side -> 'unknown'.
    """
    if not current or not target:
        return "unknown"
    cmp = compare_versions(current, target)
    if cmp == 0:
        return "current"
    if cmp > 0:
        return "behind" if allow_downgrade else "current"
    return "behind"


def version_direction(current: Optional[str], target: str) -> Optional[str]:
    """'downgrade' if the device is newer than target, else 'upgrade'; None if
    either side is unknown. Only meaningful when the device is off-target."""
    if not current or not target:
        return None
    return "downgrade" if compare_versions(current, target) > 0 else "upgrade"


def active_inactive_versions(device: dict) -> tuple:
    """Canonical (active, inactive) bank versions, normalized. Active bank wins;
    falls back to the device's reported firmware_version when the bank-specific
    value is absent; unknown active_bank -> (firmware, "")."""
    bank1 = normalize_version(device.get("bank1_version"))
    bank2 = normalize_version(device.get("bank2_version"))
    firmware = normalize_version(device.get("firmware_version"))
    try:
        active_bank = int(device.get("active_bank")) if device.get("active_bank") is not None else None
    except (TypeError, ValueError):
        active_bank = None
    if active_bank == 1:
        return (bank1 or firmware, bank2)
    if active_bank == 2:
        return (bank2 or firmware, bank1)
    return (firmware, "")


def needs_scheduled_update(device: dict, target_version: str, bank_mode: str,
                           allow_downgrade: bool) -> bool:
    """Scheduled/manual-CPE enrollment policy: bank-aware, PARSED compare on the
    device's reported firmware_version. Missing target or unknown current ->
    push (cautious). In 'both' mode, an off-target inactive bank also pushes."""
    if not target_version:
        return True
    current = (device.get("firmware_version") or "").replace(".r", ".")
    if not current:
        return True
    if version_state(current, target_version, allow_downgrade) == "behind":
        return True
    if bank_mode == "both":
        b1 = (device.get("bank1_version") or "").replace(".r", ".")
        b2 = (device.get("bank2_version") or "").replace(".r", ".")
        active_bank = device.get("active_bank", 1)
        inactive = b2 if active_bank == 1 else b1
        if inactive:
            if version_state(inactive, target_version, allow_downgrade) == "behind":
                return True
        elif b1 or b2:
            return True
    return False


def needs_manual_bulk_update(device: dict, target_version: str, bank_mode: str,
                             allow_downgrade: bool = False) -> bool:
    """Manual bulk skip gate: EXACT-STRING active match (after .r-normalize) +
    bank mode. Skips only when provably satisfied; a strictly-newer device is
    skipped unless downgrades are allowed."""
    target = normalize_version(target_version)
    if not target:
        return True
    active_version, inactive_version = active_inactive_versions(device)
    if not active_version:
        return True
    target_parsed = parse_version(target)
    active_parsed = parse_version(active_version)
    if active_version == target:
        if bank_mode == "both":
            if not inactive_version:
                return False
            if inactive_version == target:
                return False
            if not allow_downgrade and parse_version(inactive_version) > target_parsed:
                return False
            return True
        return False
    if not allow_downgrade and active_parsed > target_parsed:
        return False
    return True


def is_provably_on_build(device: dict, target_version: str, bank_mode: str) -> bool:
    """Manual single-device no-op gate: True only when the device is provably on
    the exact target build (EXACT-STRING). Ignores downgrade — an explicit
    per-device update always pushes unless this proves a no-op."""
    target = normalize_version(target_version)
    if not target:
        return False
    active_version, inactive_version = active_inactive_versions(device)
    if active_version != target:
        return False
    if bank_mode == "both" and inactive_version != target:
        return False
    return True


def needs_rollout_update(current_version: str, target_version: str,
                         allow_downgrade: bool, last_update_iso: Optional[str] = None,
                         cooldown_days: int = 0) -> bool:
    """Rollout-enrollment policy (scheduler): HYBRID — exact-string equality is
    the skip gate, but the newer-than-target guard is PARSED. Adds a cooldown
    and the '__unknown__' marker (filename present but version unparseable ->
    enroll). Missing target -> do NOT enroll (rollout safety)."""
    if not target_version:
        return False
    if target_version == "__unknown__":
        return True
    if cooldown_days > 0 and last_update_iso:
        try:
            last_upd = datetime.fromisoformat(last_update_iso)
            if datetime.now() - last_upd < timedelta(days=cooldown_days):
                return False
        except (ValueError, TypeError):
            pass
    if current_version == target_version:
        return False
    if not allow_downgrade and parse_version(current_version) > parse_version(target_version):
        return False
    return True
