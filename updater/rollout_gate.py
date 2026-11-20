"""Shared firmware maintenance-window identity and admission gates."""

from datetime import datetime
from typing import Optional


FIRMWARE_CUTOFF_REASON = "cutoff_reached"
FIRMWARE_CUTOFF_MESSAGE = "Deferred: the firmware admission cutoff for this target has passed."
SCHEDULE_DISABLED_REASON = "Deferred: Auto-Update is off or unavailable"


def scheduled_admission_decision(settings: dict) -> tuple[bool, Optional[str]]:
    """Admit scheduled work only while Auto-Update is on."""
    if settings.get("schedule_enabled") != "true":
        return False, SCHEDULE_DISABLED_REASON
    return True, None


def current_scheduled_admission_decision() -> tuple[bool, Optional[str]]:
    """Read Auto-Update at each scheduled admission boundary."""
    from . import database as db

    try:
        enabled = db.get_setting("schedule_enabled")
    except Exception:
        return False, SCHEDULE_DISABLED_REASON
    return scheduled_admission_decision({"schedule_enabled": enabled})


from zoneinfo import ZoneInfo

from .core.gates import MaintenanceSchedule, window_instance_key
from . import services


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
        auto_zone = not timezone_name or timezone_name == "auto"
        if auto_zone:
            timezone_name = services.get_cached_timezone()
            if timezone_name is None:
                return None
        schedule_tz = ZoneInfo(timezone_name)
        if now is None:
            now = datetime.now(schedule_tz)
        elif now.tzinfo is None or now.utcoffset() is None:
            # Explicit naive times are schedule-local. Auto naive times come
            # from the host clock in scheduler status and represent host time.
            now = now.astimezone() if auto_zone else now.replace(tzinfo=schedule_tz)
        now = now.astimezone(schedule_tz)
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
