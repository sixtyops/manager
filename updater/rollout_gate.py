"""Single source of truth for rollout phase gating (fail-closed).

A fleet rollout widens in waves (pct10 -> pct50 -> pct100). There is no separate
canary phase: the first wave (pct10) is the de-facto canary, and any device that
fails to come back online halts the whole job (halt-on-first-failure). Two timing
rules protect uptime:

  1. **One wave per maintenance window.** After a rollout runs a wave's job in a
     window, the next wave waits for the *next* window. Without this, all waves
     cascade through a single window and the whole fleet updates at once (the
     "all devices in one night" incident).

  2. **Firmware hold on the first wave.** The first wave (pct10) of newly-released
     firmware waits out the **Firmware Hold** — N days after the firmware's Tachyon
     release date — before any fleet device updates. The hold clears early, per
     model family, once a device of that family is confirmed working on the new
     firmware (the operator's manual canary). The caller computes that and passes
     `first_wave_held`; this function only enforces it. The hold gates pct10 only —
     pct50/pct100 never re-consult it, so a rollout that has begun always finishes
     on schedule.

Both rules live in this one function so they are defined and tested exactly once
and cannot drift between the execution path and any future caller (e.g. the UI's
"next attempt" prediction). The function is **fail-closed**: any unexpected
rollout state returns "do not run", so a future refactor that introduces a new
phase or status holds instead of cascading.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Optional


SCHEDULED_ROLLOUT_SAFETY_REASON = (
    "Scheduled firmware updates are paused for safety qualification. "
    "Complete the rollout safety fixes and bench tests, then have the operator record readiness."
)


def scheduled_admission_decision(settings: dict) -> tuple[bool, Optional[str]]:
    """Admit scheduled work after the operator records safety readiness."""
    if settings.get("scheduled_rollout_safety_ready") != "true":
        return False, SCHEDULED_ROLLOUT_SAFETY_REASON
    return True, None


def current_scheduled_admission_decision() -> tuple[bool, Optional[str]]:
    """Read current readiness at each scheduled admission boundary."""
    from . import database as db

    try:
        readiness = db.get_setting("scheduled_rollout_safety_ready")
    except Exception:
        return False, SCHEDULED_ROLLOUT_SAFETY_REASON
    return scheduled_admission_decision({"scheduled_rollout_safety_ready": readiness})
from zoneinfo import ZoneInfo

from .core.gates import MaintenanceSchedule, phase_run_decision, window_instance_key


def firmware_window_key(settings: dict, now: datetime | None = None):
    """Return the shared key for the configured firmware maintenance window."""
    try:
        days = frozenset(
            {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}[day.strip().lower()[:3]]
            for day in str(settings.get("schedule_days", "")).split(",") if day.strip()
        )
        start = int(settings["schedule_start_hour"])
        end = int(settings["schedule_end_hour"])
        timezone_name = settings.get("schedule_timezone") or settings.get("timezone")
        if not timezone_name or timezone_name == "auto":
            now = now or datetime.now().astimezone()
            timezone_name = None
        else:
            schedule_tz = ZoneInfo(timezone_name)
            now = now or datetime.now(schedule_tz)
            if now.tzinfo is None or now.utcoffset() is None:
                # A naive injected scheduler time is in the configured schedule
                # zone. Letting astimezone() infer the host zone makes the
                # window key depend on where the scheduler process runs.
                now = now.replace(tzinfo=schedule_tz)
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.astimezone()
        schedule = MaintenanceSchedule(
            schedule_id="firmware",
            days=days,
            start_hour=start,
            end_hour=end,
            tz=timezone_name,
        )
        return window_instance_key(schedule, now)
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AddressTransition:
    """Bind proof to exact artifacts, management scope, and a recovery case."""

    family: str
    role: str
    source: str
    target: str
    source_sha256: str = ""
    target_sha256: str = ""
    management_vlan: Optional[int] = None
    management_ip_mode: str = ""
    recovery_case: str = ""


@dataclass(frozen=True)
class AddressBenchPlan:
    """Limit an approved bench plan to named addresses and transitions."""

    approval: str
    devices: frozenset[tuple[str, AddressTransition]]


# Add entries with a recorded bench-proof reference after independent review.
ADDRESS_CHANGE_QUALIFICATIONS: dict[AddressTransition, str] = {}


def _known_address_scope(transition: AddressTransition) -> bool:
    """Require complete artifact and management evidence with an exact case."""
    return (
        isinstance(transition.source_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", transition.source_sha256) is not None
        and isinstance(transition.target_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", transition.target_sha256) is not None
        and type(transition.management_vlan) is int and 0 <= transition.management_vlan <= 4094
        and isinstance(transition.management_ip_mode, str)
        and transition.management_ip_mode in {"dhcp", "static"}
        and isinstance(transition.recovery_case, str)
        and transition.recovery_case in {"pre_update_reboot", "firmware_flash"}
    )


def _address_bench_plan(name: str) -> Optional[AddressBenchPlan]:
    """Read an operator-provided lab plan with a bounded approval period."""
    path = os.environ.get("SIXTYOPS_ADDRESS_BENCH_PLAN_FILE", "")
    if not path or not name:
        return None
    try:
        with Path(path).open() as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            return None
        plan = json.loads(raw)
        if plan["name"] != name or not isinstance(plan["approval"], str) or not plan["approval"].strip():
            return None
        expires = datetime.fromisoformat(plan["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            return None
        devices = frozenset(
            (d["ip"], AddressTransition(
                d["family"], d["role"], d["source"], d["target"],
                d["source_sha256"], d["target_sha256"], d["management_vlan"],
                d["management_ip_mode"], d["recovery_case"],
            ))
            for d in plan["devices"]
        )
        if any(not _known_address_scope(transition) for _, transition in devices):
            return None
        return AddressBenchPlan(plan["approval"], devices)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def exact_address_build(version: Optional[str]) -> str:
    """Accept a full release and revision; preserve every build component."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)[.\s-]+(?:r|rev\s+)?(\d+)", version or "")
    return f"{match[1]}.{match[2]}" if match else ""


def address_change_decision(
    family: Optional[str], role: str, source: Optional[str], target: Optional[str],
    *, ip: str = "", bench_exception: str = "", scheduled: bool = False,
    source_sha256: str = "", target_sha256: str = "",
    management_vlan: Optional[int] = None, management_ip_mode: str = "",
    recovery_case: str = "",
) -> tuple[bool, Optional[str]]:
    """Require exact transition proof or an explicitly selected lab plan."""
    transition = AddressTransition(
        family or "unknown", role, exact_address_build(source), exact_address_build(target),
        source_sha256, target_sha256, management_vlan, management_ip_mode, recovery_case,
    )
    if (transition.family in {"tna-30x", "tna-303l", "tns-100"}
            and role in {"ap", "cpe", "switch"}
            and transition.source and transition.target and _known_address_scope(transition)):
        proof = ADDRESS_CHANGE_QUALIFICATIONS.get(transition)
        if isinstance(proof, str) and proof.strip():
            return True, None
        plan = _address_bench_plan(bench_exception) if not scheduled else None
        if plan and plan.approval and (ip, transition) in plan.devices:
            return True, f"Bench plan {bench_exception}: {plan.approval}"
    return False, (
        f"Address-change hold: {transition.family} {role}, "
        f"{source or 'unknown'} to {target or 'unknown'}, {recovery_case or 'unknown recovery case'}. "
        "Record both artifact SHA-256 values and the management VLAN/IP mode, "
        "then qualify this scope or use an approved named lab plan."
    )


__all__ = ["firmware_window_key", "phase_run_decision", "window_instance_key", "address_change_decision"]
