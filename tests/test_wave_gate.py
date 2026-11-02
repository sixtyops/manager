"""Maintenance-window identity tests for updater/core/gates.py."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from updater.core.gates import MaintenanceSchedule, firmware_window_cutoff, window_instance_key

CHI = ZoneInfo("America/Chicago")
ALL_DAYS = frozenset(range(7))


def test_two_windows_on_one_day_give_distinct_keys():
    morning = MaintenanceSchedule(schedule_id="am", days=ALL_DAYS, start_hour=2, end_hour=4)
    evening = MaintenanceSchedule(schedule_id="pm", days=ALL_DAYS, start_hour=20, end_hour=22)
    k1 = window_instance_key(morning, datetime(2026, 6, 10, 3, 0, tzinfo=CHI))
    k2 = window_instance_key(evening, datetime(2026, 6, 10, 21, 0, tzinfo=CHI))
    assert k1 == ("am", "2026-06-10")
    assert k2 == ("pm", "2026-06-10")


def test_same_schedule_twice_on_one_day_gives_distinct_keys():
    schedule = MaintenanceSchedule(schedule_id=7, days=ALL_DAYS, start_hour=6, end_hour=6)
    early = window_instance_key(schedule, datetime(2026, 6, 10, 5, 0, tzinfo=CHI))
    late = window_instance_key(schedule, datetime(2026, 6, 10, 7, 0, tzinfo=CHI))
    assert early != late


def test_window_crossing_midnight_gives_one_key():
    schedule = MaintenanceSchedule(schedule_id=2, days=frozenset({2}), start_hour=22, end_hour=2)
    before = window_instance_key(schedule, datetime(2026, 6, 10, 23, 30, tzinfo=CHI))
    after = window_instance_key(schedule, datetime(2026, 6, 11, 1, 30, tzinfo=CHI))
    assert before == after == (2, "2026-06-10")


def test_window_belongs_to_its_start_day():
    schedule = MaintenanceSchedule(schedule_id=2, days=frozenset({3}), start_hour=22, end_hour=2)
    assert window_instance_key(schedule, datetime(2026, 6, 11, 1, 0, tzinfo=CHI)) is None


def test_key_is_same_for_any_input_timezone():
    schedule = MaintenanceSchedule(
        schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=5, tz="America/Chicago",
    )
    local_now = datetime(2026, 6, 10, 3, 10, tzinfo=CHI)
    assert window_instance_key(schedule, local_now.astimezone(timezone.utc)) == window_instance_key(
        schedule, local_now
    )


def test_key_outside_window_is_none():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=5)
    assert window_instance_key(schedule, datetime(2026, 6, 10, 12, 0, tzinfo=CHI)) is None


def test_key_invalid_input_is_none():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=5)
    assert window_instance_key(None, datetime(2026, 6, 10, 3, 10, tzinfo=CHI)) is None
    assert window_instance_key(schedule, None) is None
    assert window_instance_key(schedule, datetime(2026, 6, 10, 3, 10)) is None


@pytest.mark.parametrize("zone", [None, "America/Chicago"])
@pytest.mark.parametrize(
    "before,after,start_date",
    [
        ("2026-11-01T01:15:00-05:00", "2026-11-01T01:15:00-06:00", "2026-11-01"),
        ("2026-03-08T01:15:00-06:00", "2026-03-08T03:15:00-05:00", "2026-03-08"),
    ],
)
def test_dst_offset_change_keeps_window_key(zone, before, after, start_date):
    schedule = MaintenanceSchedule(
        schedule_id=1, days=ALL_DAYS, start_hour=0, end_hour=4, tz=zone,
    )
    first = datetime.fromisoformat(before)
    later = datetime.fromisoformat(after)
    assert window_instance_key(schedule, first) == window_instance_key(schedule, later)
    assert window_instance_key(schedule, first) == (1, start_date)


def test_firmware_cutoff_uses_first_ramp_from_scheduled_start():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=6, tz="America/Chicago")
    cutoff = firmware_window_cutoff(schedule, datetime(2026, 6, 10, 3, 20, tzinfo=CHI), 1, [30, 120])
    assert cutoff == datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)


def test_firmware_cutoff_uses_second_ramp_and_full_window_after_ramp_list():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=6, tz="America/Chicago")
    now = datetime(2026, 6, 10, 3, 20, tzinfo=CHI)
    assert firmware_window_cutoff(schedule, now, 2, [30, 120]) == datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)
    assert firmware_window_cutoff(schedule, now, 3, [30, 120]) == datetime(2026, 6, 10, 10, 45, tzinfo=timezone.utc)


def test_firmware_cutoff_late_tick_does_not_shift_ramp_anchor():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=6, tz="America/Chicago")
    cutoff = firmware_window_cutoff(schedule, datetime(2026, 6, 10, 3, 40, tzinfo=CHI), 1, [30, 120])
    assert cutoff == datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)


def test_firmware_cutoff_handles_overnight_window_and_dst():
    overnight = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=22, end_hour=2, tz="America/Chicago")
    cutoff = firmware_window_cutoff(overnight, datetime(2026, 6, 11, 1, 0, tzinfo=CHI), 2, [30, 120])
    assert cutoff == datetime(2026, 6, 11, 5, 0, tzinfo=timezone.utc)

    dst = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=0, end_hour=4, tz="America/Chicago")
    cutoff = firmware_window_cutoff(dst, datetime(2026, 3, 8, 3, 15, tzinfo=CHI), 2, [30, 120])
    assert cutoff == datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("setting", [[0, 120], [120, 30], [30, True], "30,120", None])
def test_firmware_cutoff_fails_closed_for_malformed_ramp(setting):
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=6, tz="America/Chicago")
    now = datetime(2026, 6, 10, 3, 20, tzinfo=CHI)
    assert firmware_window_cutoff(schedule, now, 1, setting) is None


def test_firmware_cutoff_never_extends_stored_cutoff():
    schedule = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=6, tz="America/Chicago")
    now = datetime(2026, 6, 10, 3, 20, tzinfo=CHI)
    stored = datetime(2026, 6, 10, 8, 10, tzinfo=timezone.utc)
    assert firmware_window_cutoff(schedule, now, 2, [30, 120], stored) == stored
    assert firmware_window_cutoff(schedule, now, 2, [30, 120], "bad cutoff") is None
