"""Shared firmware maintenance-window identity and readiness gates."""

from datetime import datetime
from typing import Optional


SCHEDULED_ROLLOUT_SAFETY_REASON = (
    "Scheduled updates are off for this network until an admin turns them on. "
    "Manual updates still work."
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

from .core.gates import MaintenanceSchedule, window_instance_key


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


__all__ = ["firmware_window_key", "window_instance_key"]
