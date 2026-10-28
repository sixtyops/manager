"""Unit tests for the ordered wave-start gates in updater/core/gates.py.

Each gate has a pass and a fail case on a fixture context. Order tests make
two gates fail at once and check that the earlier gate wins. Fail-closed
tests give unknown or missing data and check that the wave is blocked.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from updater import database as db, rollout_gate
from updater.core import gates
from updater.core.gates import (
    GATES,
    Decision,
    MaintenanceSchedule,
    WaveStartContext,
    evaluate_wave_start,
    window_instance_key,
)

CHI = ZoneInfo("America/Chicago")
ALL_DAYS = frozenset(range(7))

# Wednesday 2026-06-10, 03:10 Chicago. Inside a 03:00-05:00 window.
NOW = datetime(2026, 6, 10, 3, 10, tzinfo=CHI)
SCHEDULE = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=5)
KEY = window_instance_key(SCHEDULE, NOW)

ORDER = [
    "status",
    "job_in_flight",
    "already_ran_this_window",
    "frozen",
    "clock_invalid",
    "outside_window",
    "window_ending",
    "blocked_weather",
    "artifact_missing",
    "nothing_schedulable",
]


def _ctx(**overrides) -> WaveStartContext:
    """A context where every gate passes."""
    base = WaveStartContext(
        rollout={"status": "active", "phase": "pct10", "last_phase_window": None},
        now=NOW,
        schedule=SCHEDULE,
        freeze_windows=[],
        ntp_drift_seconds=2.0,
        weather=True,
        artifacts_ok=True,
        pending_units=3,
        job_in_flight=False,
        first_wave_held=False,
    )
    return replace(base, **overrides)


# One failing override per gate, keyed by gate name.
FAIL = {
    "status": dict(rollout={"status": "paused", "phase": "pct10", "last_phase_window": None}),
    "job_in_flight": dict(job_in_flight=True),
    "already_ran_this_window": dict(
        rollout={"status": "active", "phase": "pct50", "last_phase_window": KEY}
    ),
    "frozen": dict(freeze_windows=[{"start_date": "2026-06-09", "end_date": "2026-06-11", "enabled": 1}]),
    "clock_invalid": dict(ntp_drift_seconds=301.0),
    "outside_window": dict(now=datetime(2026, 6, 10, 12, 0, tzinfo=CHI)),
    "window_ending": dict(now=datetime(2026, 6, 10, 4, 45, tzinfo=CHI)),
    "blocked_weather": dict(weather=False),
    "artifact_missing": dict(artifacts_ok=False),
    "nothing_schedulable": dict(pending_units=0),
}


def test_gate_order_is_the_contract():
    assert [name for name, _ in GATES] == ORDER


def test_all_gates_pass_on_fixture():
    assert evaluate_wave_start(_ctx()) == Decision(True, None)


@pytest.mark.parametrize("name", ORDER)
def test_each_gate_blocks_with_its_reason(name):
    decision = evaluate_wave_start(_ctx(**FAIL[name]))
    assert decision.ok is False
    expected = "status_paused" if name == "status" else name
    assert decision.reason == expected


@pytest.mark.parametrize("name", ORDER)
def test_each_gate_function_passes_on_fixture(name):
    gate = dict(GATES)[name]
    assert gate(_ctx()).ok is True


@pytest.mark.parametrize("i", range(len(ORDER) - 1))
def test_earlier_gate_wins_over_every_later_gate(i):
    """Fail gate i and every later gate. The result is gate i."""
    first = ORDER[i]
    for later in ORDER[i + 1:]:
        if first == "already_ran_this_window" and later == "outside_window":
            # No window is open, so there is no window instance to compare.
            # See test_already_ran_needs_an_open_window.
            continue
        # When both set `now`, the earlier gate's value wins.
        overrides = {**FAIL[later], **FAIL[first]}
        decision = evaluate_wave_start(_ctx(**overrides))
        expected = "status_paused" if first == "status" else first
        assert decision.reason == expected, (first, later, decision)


# ── Firmware Hold (inside already_ran_this_window, via phase_run_decision) ──

def test_already_ran_needs_an_open_window():
    """Outside a window there is no instance key. The wave is still blocked,
    by outside_window."""
    rollout = {"status": "active", "phase": "pct50", "last_phase_window": KEY}
    later = datetime(2026, 6, 10, 12, 0, tzinfo=CHI)
    assert evaluate_wave_start(_ctx(rollout=rollout, now=later)).reason == "outside_window"


def test_hold_blocks_first_wave():
    decision = evaluate_wave_start(_ctx(first_wave_held=True))
    assert decision == Decision(False, "firmware_hold")


def test_hold_ignored_after_first_wave():
    for phase in ("pct50", "pct100"):
        rollout = {"status": "active", "phase": phase, "last_phase_window": None}
        assert evaluate_wave_start(_ctx(rollout=rollout, first_wave_held=True)).ok is True


def test_already_ran_wins_over_cleared_hold():
    """A cleared hold never lets a second wave run in the same window."""
    rollout = {"status": "active", "phase": "pct10", "last_phase_window": KEY}
    decision = evaluate_wave_start(_ctx(rollout=rollout, first_wave_held=False))
    assert decision.reason == "already_ran_this_window"


def test_hold_wins_over_later_gates():
    decision = evaluate_wave_start(_ctx(first_wave_held=True, weather=False, pending_units=0))
    assert decision.reason == "firmware_hold"


def test_previous_window_key_allows_next_window():
    yesterday = window_instance_key(SCHEDULE, NOW - timedelta(days=1))
    rollout = {"status": "active", "phase": "pct50", "last_phase_window": yesterday}
    assert evaluate_wave_start(_ctx(rollout=rollout)).ok is True


def test_legacy_date_only_marker_allows_active_rollout_in_next_window():
    rollout = {
        "status": "active",
        "phase": "pct50",
        "last_phase_window": "2026-06-09",
    }
    assert evaluate_wave_start(_ctx(rollout=rollout)).ok is True


def test_legacy_date_only_marker_blocks_second_wave_in_same_window():
    rollout = {
        "status": "active",
        "phase": "pct50",
        "last_phase_window": "2026-06-10",
    }
    assert evaluate_wave_start(_ctx(rollout=rollout)) == Decision(
        False, "already_ran_this_window"
    )


def test_legacy_date_only_marker_blocks_overnight_window_after_midnight():
    sched = MaintenanceSchedule(schedule_id=2, days=frozenset({2}), start_hour=22, end_hour=2)
    rollout = {
        "status": "active",
        "phase": "pct50",
        "last_phase_window": "2026-06-10",
    }
    for now in (
        datetime(2026, 6, 10, 23, 30, tzinfo=CHI),
        datetime(2026, 6, 11, 1, 30, tzinfo=CHI),
    ):
        assert evaluate_wave_start(_ctx(rollout=rollout, schedule=sched, now=now)) == Decision(
            False, "already_ran_this_window"
        )


def test_new_rollout_window_writes_use_schedule_and_date_key(mock_db):
    rollout_id = db.create_rollout("firmware.bin")
    db.set_rollout_phase_window(rollout_id, KEY)
    assert db.get_active_rollout()["last_phase_window"] == json.dumps(
        list(KEY), separators=(",", ":")
    )


def test_stored_key_as_list_matches():
    """A key read back from JSON is a list. It still matches this window."""
    rollout = {"status": "active", "phase": "pct50", "last_phase_window": json.loads(json.dumps(KEY))}
    assert evaluate_wave_start(_ctx(rollout=rollout)).reason == "already_ran_this_window"


def test_rollout_gate_reexports_the_same_function():
    assert rollout_gate.phase_run_decision is gates.phase_run_decision


# ── Fail-closed on unknown or missing data ──

@pytest.mark.parametrize(
    "overrides, reason",
    [
        (dict(rollout=None), "status_None"),
        (dict(rollout={}), "status_None"),
        (dict(rollout={"status": "bogus"}), "status_bogus"),
        (dict(rollout={"status": "active", "last_phase_window": None}), "phase_None"),
        (dict(rollout={"status": "active", "phase": "bogus", "last_phase_window": None}),
         "phase_bogus"),
        (dict(rollout={"status": "active", "phase": "canary", "last_phase_window": None}),
         "phase_canary"),
        (dict(job_in_flight=None), "job_in_flight"),
        (dict(first_wave_held=None), "firmware_hold"),
        (dict(rollout={"status": "active", "phase": "pct50", "last_phase_window": "2026-06-10"}),
         "already_ran_this_window"),
        (dict(rollout={"status": "active", "phase": "pct50", "last_phase_window": (1, "x")}),
         "already_ran_this_window"),
        (dict(freeze_windows=None), "frozen"),
        (dict(freeze_windows=[{"start_date": "garbage", "end_date": "2026-06-11"}]), "frozen"),
        (dict(freeze_windows=[{"end_date": "2026-06-11"}]), "frozen"),
        (dict(freeze_windows=["not-a-row"]), "frozen"),
        (dict(now=None), "frozen"),
        (dict(now=datetime(2026, 6, 10, 3, 10)), "frozen"),  # naive
        (dict(ntp_drift_seconds="5"), "clock_invalid"),
        (dict(ntp_drift_seconds=float("nan")), "clock_invalid"),
        (dict(ntp_drift_seconds=-301.0), "clock_invalid"),
        (dict(schedule=None), "outside_window"),
        (dict(schedule=replace(SCHEDULE, days=frozenset())), "outside_window"),
        (dict(schedule=replace(SCHEDULE, start_hour=24)), "outside_window"),
        (dict(schedule=replace(SCHEDULE, schedule_id=None)), "outside_window"),
        (dict(schedule=replace(SCHEDULE, tz="Not/AZone")), "outside_window"),
        (dict(weather=None), "blocked_weather"),
        (dict(weather="ok"), "blocked_weather"),
        (dict(artifacts_ok=None), "artifact_missing"),
        (dict(artifacts_ok=1), "artifact_missing"),
        (dict(pending_units=None), "nothing_schedulable"),
        (dict(pending_units=-1), "nothing_schedulable"),
        (dict(pending_units=True), "nothing_schedulable"),
    ],
)
def test_unknown_or_missing_data_blocks(overrides, reason):
    decision = evaluate_wave_start(_ctx(**overrides))
    assert decision == Decision(False, reason)


def test_ntp_unreachable_allows():
    """The one documented exception: unreachable NTP falls back to the system clock."""
    assert evaluate_wave_start(_ctx(ntp_drift_seconds=None)).ok is True


def test_disabled_freeze_row_is_ignored():
    rows = [{"start_date": "2026-06-09", "end_date": "2026-06-11", "enabled": 0}]
    assert evaluate_wave_start(_ctx(freeze_windows=rows)).ok is True


def test_freeze_end_date_covers_whole_day():
    rows = [{"start_date": "2026-06-01", "end_date": "2026-06-10", "enabled": 1}]
    late = datetime(2026, 6, 10, 23, 30, tzinfo=CHI)
    sched = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=23, end_hour=2)
    assert evaluate_wave_start(_ctx(freeze_windows=rows, now=late, schedule=sched)).reason == "frozen"


def test_gate_that_raises_blocks_with_its_name(monkeypatch):
    def boom(ctx):
        raise RuntimeError("bad data")

    gates_list = list(GATES)
    gates_list[ORDER.index("blocked_weather")] = ("blocked_weather", boom)
    monkeypatch.setattr(gates, "GATES", tuple(gates_list))
    assert gates.evaluate_wave_start(_ctx()) == Decision(False, "blocked_weather")


def test_non_context_input_blocks():
    assert evaluate_wave_start(None).ok is False
    assert evaluate_wave_start({"rollout": {}}).ok is False


def test_context_requires_every_field():
    with pytest.raises(TypeError):
        WaveStartContext(rollout={}, now=NOW)  # type: ignore[call-arg]


# ── Window end buffer ──

def test_window_ending_boundary():
    # 16 minutes left: runs. 15 minutes left: blocked.
    assert evaluate_wave_start(_ctx(now=datetime(2026, 6, 10, 4, 44, tzinfo=CHI))).ok is True
    assert evaluate_wave_start(
        _ctx(now=datetime(2026, 6, 10, 4, 45, tzinfo=CHI))
    ).reason == "window_ending"


def test_window_end_is_exclusive():
    assert evaluate_wave_start(_ctx(now=datetime(2026, 6, 10, 5, 0, tzinfo=CHI))).reason == "outside_window"


# ── Window instance key ──

def test_two_windows_on_one_day_give_distinct_keys():
    morning = MaintenanceSchedule(schedule_id="am", days=ALL_DAYS, start_hour=2, end_hour=4)
    evening = MaintenanceSchedule(schedule_id="pm", days=ALL_DAYS, start_hour=20, end_hour=22)
    k1 = window_instance_key(morning, datetime(2026, 6, 10, 3, 0, tzinfo=CHI))
    k2 = window_instance_key(evening, datetime(2026, 6, 10, 21, 0, tzinfo=CHI))
    assert k1 is not None and k2 is not None
    assert k1 != k2
    assert k1 == ("am", "2026-06-10")
    assert k2 == ("pm", "2026-06-10")


def test_same_schedule_twice_on_one_day_gives_distinct_keys():
    """A 24-hour window starting at 06:00 on two days: same date, two keys."""
    sched = MaintenanceSchedule(schedule_id=7, days=ALL_DAYS, start_hour=6, end_hour=6)
    k_early = window_instance_key(sched, datetime(2026, 6, 10, 5, 0, tzinfo=CHI))
    k_late = window_instance_key(sched, datetime(2026, 6, 10, 7, 0, tzinfo=CHI))
    assert k_early != k_late


def test_window_crossing_midnight_gives_one_key():
    sched = MaintenanceSchedule(schedule_id=2, days=frozenset({2}), start_hour=22, end_hour=2)  # Wed
    before = window_instance_key(sched, datetime(2026, 6, 10, 23, 30, tzinfo=CHI))
    after = window_instance_key(sched, datetime(2026, 6, 11, 1, 30, tzinfo=CHI))
    assert before is not None
    assert before == after == (2, "2026-06-10")


def test_crossing_midnight_already_ran_blocks_after_midnight():
    sched = MaintenanceSchedule(schedule_id=2, days=frozenset({2}), start_hour=22, end_hour=2)
    ran_key = window_instance_key(sched, datetime(2026, 6, 10, 22, 5, tzinfo=CHI))
    rollout = {"status": "active", "phase": "pct50", "last_phase_window": ran_key}
    ctx = _ctx(rollout=rollout, schedule=sched, now=datetime(2026, 6, 11, 0, 30, tzinfo=CHI))
    assert evaluate_wave_start(ctx).reason == "already_ran_this_window"


def test_window_belongs_to_its_start_day():
    """Thursday 01:00 is in Wednesday's overnight window, not Thursday's."""
    sched = MaintenanceSchedule(schedule_id=2, days=frozenset({3}), start_hour=22, end_hour=2)  # Thu
    assert window_instance_key(sched, datetime(2026, 6, 11, 1, 0, tzinfo=CHI)) is None


def test_key_is_same_for_any_input_timezone():
    sched = MaintenanceSchedule(schedule_id=1, days=ALL_DAYS, start_hour=3, end_hour=5, tz="America/Chicago")
    utc_now = NOW.astimezone(timezone.utc)
    assert window_instance_key(sched, utc_now) == window_instance_key(sched, NOW)


def test_key_outside_window_is_none():
    assert window_instance_key(SCHEDULE, datetime(2026, 6, 10, 12, 0, tzinfo=CHI)) is None


def test_key_invalid_input_is_none():
    assert window_instance_key(None, NOW) is None
    assert window_instance_key(SCHEDULE, None) is None
    assert window_instance_key(SCHEDULE, datetime(2026, 6, 10, 3, 10)) is None


@pytest.mark.parametrize("zone", [None, "America/Chicago"])
@pytest.mark.parametrize("before, after, start_date", [
    ("2026-11-01T01:15:00-05:00", "2026-11-01T01:15:00-06:00", "2026-11-01"),
    ("2026-03-08T01:15:00-06:00", "2026-03-08T03:15:00-05:00", "2026-03-08"),
])
def test_dst_offset_change_cannot_start_a_second_wave(zone, before, after, start_date):
    sched = MaintenanceSchedule(
        schedule_id=1, days=ALL_DAYS, start_hour=0, end_hour=4, tz=zone,
    )
    first = datetime.fromisoformat(before)
    later = datetime.fromisoformat(after)
    assert evaluate_wave_start(_ctx(schedule=sched, now=first)).ok is True
    key = window_instance_key(sched, first)
    assert key == window_instance_key(sched, later) == (1, start_date)
    stored = json.loads(json.dumps(key))
    rollout = {"status": "active", "phase": "pct50", "last_phase_window": stored}
    assert evaluate_wave_start(_ctx(
        schedule=sched, now=later, rollout=rollout,
    )) == Decision(False, "already_ran_this_window")


@pytest.mark.parametrize("schedule_id", [None, "", "  ", [], {}, True, False, 1.0, 0, -1])
def test_malformed_stored_schedule_id_blocks(schedule_id):
    rollout = {
        "status": "active", "phase": "pct50",
        "last_phase_window": (schedule_id, KEY[1]),
    }
    assert evaluate_wave_start(_ctx(rollout=rollout)) == Decision(False, "already_ran_this_window")


@pytest.mark.parametrize("schedule_id", [None, "", "  ", [], {}, True, False, 1.0, 0, -1])
def test_malformed_current_schedule_id_blocks(schedule_id):
    sched = replace(SCHEDULE, schedule_id=schedule_id)
    assert evaluate_wave_start(_ctx(schedule=sched)) == Decision(False, "outside_window")


@pytest.mark.parametrize("start_date", [
    None, "", "20260610", "2026-02-30", "2026-06-10T00:00:00+00:00", NOW,
])
def test_malformed_stored_start_date_blocks(start_date):
    rollout = {
        "status": "active", "phase": "pct50",
        "last_phase_window": (1, start_date),
    }
    assert evaluate_wave_start(_ctx(rollout=rollout)) == Decision(False, "already_ran_this_window")


@pytest.mark.parametrize("start, end", [
    ("2026-06-11", "2026-06-09"),
    ("2026-06-10T04:00:00-05:00", "2026-06-10T03:00:00-05:00"),
])
def test_reversed_enabled_freeze_blocks(start, end):
    rows = [{"start_date": start, "end_date": end, "enabled": 1}]
    assert evaluate_wave_start(_ctx(freeze_windows=rows)) == Decision(False, "frozen")


@pytest.mark.parametrize("start,end,clock", [
    (8, 17, "2026-06-10T12:30:00"),
    (0, 4, "2026-06-10T01:30:00"),
    (22, 4, "2026-06-10T23:30:00"),
    (22, 4, "2026-06-11T01:30:00"),
    (22, 0, "2026-06-10T23:30:00"),
    (4, 4, "2026-06-10T12:30:00"),
    (4, 4, "2026-06-11T01:30:00"),
    (0, 0, "2026-06-10T01:30:00"),
])
@pytest.mark.parametrize("marker,reason", [
    ("2026-06-09", None),
    ("2026-06-10", "already_ran_this_window"),
    ("2026-06-11", "legacy_window_ambiguous"),
])
@pytest.mark.parametrize("encoded", [False, True])
def test_legacy_marker_requires_later_window_start(start, end, clock, marker, reason, encoded):
    schedule = MaintenanceSchedule(
        schedule_id=2, days=frozenset({2}), start_hour=start, end_hour=end,
    )
    now = datetime.fromisoformat(clock).replace(tzinfo=CHI)
    key = window_instance_key(schedule, now)
    assert key == (2, "2026-06-10")
    rollout = {
        "status": "active", "phase": "pct50",
        "last_phase_window": json.dumps(marker) if encoded else marker,
    }
    expected = (reason is None, reason)
    assert rollout_gate.phase_run_decision(rollout, key) == expected
    assert evaluate_wave_start(_ctx(rollout=rollout, schedule=schedule, now=now)) == Decision(*expected)
