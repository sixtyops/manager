"""Single source of truth for a firmware family's deployable target.

Answers "what firmware should family X get, what version is that, and is the file
actually deployable right now?" — historically resolved ~7 different ways, some
trusting the `selected_firmware_*` setting blind, one silently substituting 30x
firmware for a 303L when the file was missing.

This module answers WHAT firmware, never WHICH devices/waves — that stays in the
scheduler and the fail-closed `rollout_gate`. Per-family precedence preserves
today's behavior: an active rollout's pinned `firmware_file*` wins, else the
`selected_firmware_*` setting.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .version_utils import extract_version_from_filename
from .firmware_fetcher import PLATFORM_SETTING_KEYS  # leaf import; no cycle

FAMILIES = ("tna-30x", "tna-303l", "tns-100")

# Per-family rollout columns (a pinned firmware filename / version override).
_ROLLOUT_FILE_KEYS = {
    "tna-30x": "firmware_file",
    "tna-303l": "firmware_file_303l",
    "tns-100": "firmware_file_tns100",
}
_ROLLOUT_VERSION_KEYS = {
    "tna-30x": "target_version",
    "tna-303l": "target_version_303l",
    "tns-100": "target_version_tns100",
}

_firmware_dir: Optional[Path] = None


def set_firmware_dir(path) -> None:
    """Wire the firmware directory once at app startup, so the resolver can check
    file presence without importing `app` (which would create an import cycle)."""
    global _firmware_dir
    _firmware_dir = Path(path) if path else None


@dataclass(frozen=True)
class TargetInfo:
    family: str
    filename: str       # "" when nothing is selected/pinned
    version: str        # normalized dotted; "" or the "__unknown__" marker
    deployable: bool    # filename present AND the file is on disk
    health_reason: str  # "" | "no_selection" | "missing_file" | "unparseable_version"
    source: str         # "rollout" | "selection"


def target_versions(settings: dict, rollout: Optional[dict] = None) -> dict:
    """Verbatim replacement for the scheduler's per-family target-version map:
    `{family: version}` with the rollout `target_version*` fallback and the
    `__unknown__` marker (filename present but version unparseable -> still
    enroll). The Firmware Hold / rollout-invariant logic depends on this exact
    shape, so it stays version-only and unchanged."""
    rollout = rollout or {}
    file_names = {
        f: (rollout.get(_ROLLOUT_FILE_KEYS[f]) or settings.get(PLATFORM_SETTING_KEYS[f], ""))
        for f in FAMILIES
    }
    targets = {
        f: extract_version_from_filename(file_names[f]) or rollout.get(_ROLLOUT_VERSION_KEYS[f]) or ""
        for f in FAMILIES
    }
    for f, fn in file_names.items():
        if fn and not targets[f]:
            targets[f] = "__unknown__"
    return targets


def resolve_target(family: str, *, settings: dict, rollout: Optional[dict] = None,
                   firmware_dir=None) -> TargetInfo:
    """Resolve one family's target, including whether its file is deployable.

    Deployability is intentionally "filename present AND file on disk" — the
    honest signal for the failure mode where the selected newest build's file is
    briefly missing (it must report `deployable=False`, NOT silently fall back to
    an older build — that would undo the fetcher's never-move-backward guard).
    Truncated/duplicate files are already caught at flash time and surfaced by
    `_annotate_firmware_health` in the firmware list."""
    fdir = Path(firmware_dir) if firmware_dir else _firmware_dir
    rollout = rollout or {}
    pinned = rollout.get(_ROLLOUT_FILE_KEYS[family]) or ""
    if pinned:
        filename, source = pinned, "rollout"
    else:
        filename, source = (settings.get(PLATFORM_SETTING_KEYS[family], "") or ""), "selection"

    if not filename:
        return TargetInfo(family, "", "", False, "no_selection", source)
    version = extract_version_from_filename(filename)
    if not version:
        return TargetInfo(family, filename, "__unknown__", False, "unparseable_version", source)
    on_disk = bool(fdir) and (fdir / filename).exists()
    if not on_disk:
        return TargetInfo(family, filename, version, False, "missing_file", source)
    return TargetInfo(family, filename, version, True, "", source)


def resolve_fleet(*, settings: dict, rollout: Optional[dict] = None,
                  firmware_dir=None) -> dict:
    """`{family: TargetInfo}` for all three families."""
    return {
        f: resolve_target(f, settings=settings, rollout=rollout, firmware_dir=firmware_dir)
        for f in FAMILIES
    }
