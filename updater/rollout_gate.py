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

from datetime import datetime
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


__all__ = ["firmware_window_key", "phase_run_decision", "window_instance_key"]
