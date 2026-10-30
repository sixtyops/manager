"""Rollout timing invariants — the guarantees that keep a fleet rollout from
updating the whole fleet at once.

The canary phase was removed: the first fleet wave (pct10) is the de-facto canary
and is protected by halt-on-first-failure. Two timing rules remain (rollout_gate):

  1. One wave-job per maintenance window (no cascade).
  2. The Firmware Hold gates the first wave (pct10) of newly-released firmware
     until N days after the firmware's Tachyon release date — or earlier once a
     device of that model family is confirmed working on it.

These drive the scheduler across multiple ticks within one maintenance window and
across successive windows — the exact scenario that lets the "all phases in one
night" cascade slip through if Rule 1 ever regresses.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from updater import database as db
from updater.core import engine
from updater.core.gates import MaintenanceSchedule, window_instance_key
from tests.fixtures.fleet import make_fleet, set_version, set_health, confirm
from updater import rollout_gate
from updater.scheduler import AutoUpdateScheduler

# The release-date Firmware Hold is evaluated against the real wall clock (not the
# harness's simulated scheduler clock), so dated test firmware must be relative to
# the actual "today" to stay deterministic as time passes.
def _dated(prefix: str, days_ago: int) -> str:
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")
    return f"{prefix}-{d}-x.bin"


# Undated firmware (no release date in the name) — the hold falls back to the
# registry/legacy path and is treated as cleared, so these exercise the wave/window
# rules without a hold getting in the way.
FW = "tna-30x-1.12.3-r55002.bin"  # target version 1.12.3.55002
# Released yesterday — with hold_days=6 its hold has NOT elapsed (first wave held).
FW_HELD = _dated("tna-30x-1.12.3-r55002", 1)
# Released 30 days ago — its 6-day hold has objectively elapsed.
FW_AGED = _dated("tna-30x-1.12.3-r55002", 30)
# A 303L firmware also released yesterday (held).
FW_303L_HELD = _dated("tna-303l-1.12.4-r7782", 1)


def _discard_task(coro):
    """Swallow the broadcast task on_job_completed schedules (no running loop)."""
    coro.close()

    class _Dummy:
        def add_done_callback(self, _cb):
            return None

    return _Dummy()


def _seed_aps(n: int):
    make_fleet(sites=1, aps_per_site=n, switches_per_site=0,
               ap_models=("TNA-301",))


def _ap_customer_signals(rx=101, tx=202, *, peer=True, lan=True, upstream=True):
    radio = "02:00:00:00:00:01"
    customer = "02:00:00:00:00:02"
    return {
        "peers": ({radio: {
            "lan": ([{"mac": customer, "port": "eth0"}] if lan else []),
            "bytes": (rx, tx),
        }} if peer else {}),
        "upstream": ([{"mac": customer, "port": "prs0"}] if upstream else []),
    }


def test_ap_customer_recovery_requires_original_peer_and_bridge_paths():
    customer = "02:00:00:00:00:02"
    baseline = {"02:00:00:00:00:01": {
        "customer_macs": [customer], "rx_bytes": 100, "tx_bytes": 200,
    }}

    assert rollout_gate.ap_customer_recovery_decision(baseline, _ap_customer_signals()) == (True, "")
    assert "peer list" in rollout_gate.ap_customer_recovery_decision(
        baseline, _ap_customer_signals(peer=False)
    )[1]
    assert "eth0" in rollout_gate.ap_customer_recovery_decision(
        baseline, _ap_customer_signals(lan=False)
    )[1]
    assert "upstream" in rollout_gate.ap_customer_recovery_decision(
        baseline, _ap_customer_signals(upstream=False)
    )[1]


@pytest.mark.parametrize("rx,tx", [(101, 200), (100, 202), (100, 200), (99, 202)])
def test_ap_customer_recovery_requires_positive_two_way_deltas(rx, tx):
    customer = "02:00:00:00:00:02"
    baseline = {"02:00:00:00:00:01": {
        "customer_macs": [customer], "rx_bytes": 100, "tx_bytes": 200,
    }}

    passed, reason = rollout_gate.ap_customer_recovery_decision(
        baseline, _ap_customer_signals(rx, tx)
    )

    assert passed is (rx > 100 and tx > 200)
    if not passed:
        assert "both directions" in reason or "reset" in reason


def test_ap_customer_recovery_does_not_require_traffic_when_baseline_is_zero():
    customer = "02:00:00:00:00:02"
    baseline = {"02:00:00:00:00:01": {
        "customer_macs": [customer], "rx_bytes": 0, "tx_bytes": 0,
    }}

    assert rollout_gate.ap_customer_recovery_decision(
        baseline, _ap_customer_signals(0, 0)
    ) == (True, "")


def test_ap_customer_recovery_allows_a_valid_empty_peer_scope():
    assert rollout_gate.ap_customer_recovery_decision(
        {}, {"peers": {}, "upstream": []}
    ) == (True, "")


def _seed_confirmed(ip: str, version: str, *, model: str = None):
    """Seed an enabled, healthy AP confirmed working on `version` (on-version, no
    error, seen now, plus a recorded confirmation event)."""
    db.upsert_access_point(ip, "root", "pass", enabled=True,
                           firmware_version=version, model=model)
    db.update_ap_status(
        ip, last_seen=datetime.now(timezone.utc).isoformat(), last_error=None
    )
    db.mark_device_firmware_confirmed(ip, version)


def _settings(hold_days: int = 0, firmware: str = FW) -> dict:
    return {
        "schedule_enabled": "true",
        "scheduled_rollout_safety_ready": "true",
        "timezone": "America/Chicago",
        "schedule_start_hour": "3",
        "schedule_end_hour": "4",
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "selected_firmware_30x": firmware,
        "weather_check_enabled": "false",
        "firmware_canary_hold_days": str(hold_days),
    }


def _make_scheduler():
    start_update = AsyncMock(side_effect=lambda **kw: f"job-{start_update.call_count}")
    scheduler = AutoUpdateScheduler(AsyncMock(), start_update)
    return scheduler, start_update


def test_wave_reservation_rolls_back_marker_assignments_and_settings(mock_db):
    rollout_id = db.create_rollout(FW)
    db.set_rollout_settings_snapshot(rollout_id, {"old": "snapshot"})
    with db.get_db() as conn:
        conn.execute(
            """CREATE TRIGGER reject_wave_assignment BEFORE INSERT ON rollout_devices
               BEGIN SELECT RAISE(ABORT, 'injected assignment failure'); END"""
        )

    with pytest.raises(Exception, match="injected assignment failure"):
        engine.start_wave(
            db.get_rollout(rollout_id),
            {"ap_ips": ["10.0.0.1"], "switch_ips": []},
            {"bank_mode": "single"},
            (1, "2026-06-02"),
        )

    rollout = db.get_rollout(rollout_id)
    assert rollout["last_phase_window"] is None
    assert db.get_rollout_settings_snapshot(rollout_id) == {"old": "snapshot"}
    assert db.get_rollout_devices(rollout_id) == []


def test_create_rollout_persists_complete_membership_snapshot(mock_db):
    fixed_now = datetime(2026, 6, 2, 3, 15)
    members = [
        {"ip": "192.0.2.10", "device_type": "ap"},
        {"ip": "192.0.2.11", "device_type": "switch"},
    ]
    settings = {"bank_mode": "both", "allow_downgrade": "false"}

    rollout_id = db.create_rollout(
        FW, members=members, settings_snapshot=settings, now=fixed_now
    )

    rollout = db.get_rollout(rollout_id)
    persisted = db.get_rollout_devices(rollout_id)
    assert rollout["status"] == "waiting_hold"
    assert rollout["phase"] == "pct10"
    assert rollout["firmware_file"] == FW
    assert rollout["created_at"] == "2026-06-02 03:15:00"
    assert db.get_rollout_settings_snapshot(rollout_id) == settings
    assert {(row["ip"], row["device_type"], row["phase_assigned"], row["status"])
            for row in persisted} == {
                ("192.0.2.10", "ap", None, "pending"),
                ("192.0.2.11", "switch", None, "pending"),
            }


def test_create_rollout_rolls_back_when_any_member_insert_fails(mock_db):
    with db.get_db() as conn:
        conn.execute(
            """CREATE TRIGGER reject_rollout_member BEFORE INSERT ON rollout_devices
               WHEN NEW.ip = '192.0.2.11'
               BEGIN SELECT RAISE(ABORT, 'injected membership failure'); END"""
        )

    with pytest.raises(Exception, match="injected membership failure"):
        db.create_rollout(
            FW,
            members=[
                {"ip": "192.0.2.10", "device_type": "ap"},
                {"ip": "192.0.2.11", "device_type": "switch"},
            ],
            settings_snapshot={"bank_mode": "both"},
            now=datetime(2026, 6, 2, 3, 15),
        )

    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rollouts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rollout_devices").fetchone()[0] == 0


def test_create_rollout_duplicate_membership_requires_an_exact_snapshot(mock_db):
    fixed_now = datetime(2026, 6, 2, 3, 15)
    original_members = [{"ip": "192.0.2.10", "device_type": "ap"}]
    settings = {"bank_mode": "both"}
    rollout_id = db.create_rollout(
        FW, members=original_members, settings_snapshot=settings, now=fixed_now
    )

    duplicate_id = db.create_rollout(
        FW,
        members=original_members,
        settings_snapshot=settings,
        now=fixed_now + timedelta(seconds=1),
    )
    changed_id = db.create_rollout(
        FW,
        members=original_members + [{"ip": "192.0.2.11", "device_type": "switch"}],
        settings_snapshot=settings,
        now=fixed_now + timedelta(seconds=2),
    )

    assert duplicate_id == rollout_id
    assert changed_id != rollout_id
    assert len(db.get_rollout_devices(rollout_id)) == 1
    assert len(db.get_rollout_devices(changed_id)) == 2


def test_create_rollout_rejects_an_empty_membership_snapshot(mock_db):
    with pytest.raises(ValueError, match="must not be empty"):
        db.create_rollout(
            FW,
            members=[],
            settings_snapshot={"bank_mode": "both"},
            now=datetime(2026, 6, 2, 3, 15),
        )

    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rollouts").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (None, "requires a settings snapshot"),
        ({"bank_mode": object()}, "must be valid JSON"),
    ],
    ids=["missing", "invalid"],
)
def test_create_rollout_rejects_missing_or_invalid_settings_snapshot_without_writes(
    mock_db, snapshot, message
):
    kwargs = {} if snapshot is None else {"settings_snapshot": snapshot}
    with pytest.raises(ValueError, match=message):
        db.create_rollout(
            FW,
            members=[{"ip": "192.0.2.10", "device_type": "ap"}],
            now=datetime(2026, 6, 2, 3, 15),
            **kwargs,
        )

    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rollouts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rollout_devices").fetchone()[0] == 0


def test_empty_wave_reservation_writes_nothing(mock_db):
    rollout_id = db.create_rollout(FW)
    before = db.get_rollout(rollout_id)

    assert not engine.start_wave(
        before,
        {"ap_ips": [], "switch_ips": []},
        {"bank_mode": "single"},
        (1, "2026-06-02"),
    )
    after = db.get_rollout(rollout_id)
    assert after["last_phase_window"] == before["last_phase_window"]
    assert after["settings_json"] == before["settings_json"]
    assert db.get_rollout_devices(rollout_id) == []


def test_concurrent_wave_reservations_admit_one_batch(mock_db, monkeypatch, tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "wave-reservation.db"
    persisted = sqlite3.connect(path)
    mock_db.backup(persisted)
    persisted.close()

    @contextmanager
    def file_db():
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(db, "get_db", file_db)
    rollout_id = db.create_rollout(FW)
    rollout = db.get_rollout(rollout_id)
    key = ("firmware", "2026-06-02")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda ip: engine.start_wave(
                rollout, {"ap_ips": [ip], "switch_ips": []}, {"bank_mode": "both"}, key
            ),
            ("10.0.0.1", "10.0.0.2"),
        ))

    assert sorted(results) == [False, True]
    assert len(db.get_rollout_devices(rollout_id)) == 1
    assert db.get_rollout(rollout_id)["last_phase_window"] == '["firmware","2026-06-02"]'


def test_reserved_pending_ap_retries_after_scheduler_restart(mock_db, monkeypatch):
    _seed_aps(1)
    db.set_settings(_settings(hold_days=0))
    db.create_rollout(FW)
    rollout = db.get_active_rollout()
    engine.start_wave(
        rollout,
        {"ap_ips": ["192.0.2.10"], "switch_ips": []},
        {"bank_mode": "both"},
        ("firmware", "2026-06-02"),
    )

    retry_scheduler, retry_start = _make_scheduler()
    h = _Harness(retry_scheduler, monkeypatch)
    async def restart_ticks():
        await h.tick(day=2)
        assert retry_start.call_count == 0
        await h.tick(day=3)

    asyncio.run(restart_ticks())
    assert retry_start.call_count == 1
    assert db.get_rollout_devices(rollout["id"])[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_empty_scheduler_batch_does_not_advance_phase(mock_db, monkeypatch):
    _seed_aps(1)
    db.set_settings(_settings(hold_days=0))
    rollout_id = db.create_rollout(FW)
    set_version(db.get_all_access_points_dict(enabled_only=False)["192.0.2.10"], "1.12.3.55002")
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()

    rollout = db.get_rollout(rollout_id)
    assert rollout["phase"] == "pct10"
    assert rollout["last_phase_window"] is None
    assert start_update.call_count == 0


def test_overnight_window_keeps_its_start_date_key():
    schedule = MaintenanceSchedule(
        schedule_id=7, days=frozenset({0}), start_hour=23, end_hour=2, tz="UTC"
    )
    before_midnight = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 6, 2, 1, 30, tzinfo=timezone.utc)
    assert window_instance_key(schedule, before_midnight) == (7, "2026-06-01")
    assert window_instance_key(schedule, after_midnight) == (7, "2026-06-01")


class _Harness:
    """Drives _check_and_run at a controllable clock with the env gates stubbed
    out, so the tests isolate the wave-gate / hold logic."""

    def __init__(self, scheduler, monkeypatch):
        self.scheduler = scheduler
        self.now = datetime(2026, 6, 2, 3, 15)  # inside the 03:00-04:00 window
        async def _time(_tz):
            return (True, self.now)
        monkeypatch.setattr("updater.scheduler.services.validate_time_sources", _time)
        monkeypatch.setattr(
            "updater.scheduler.services.is_in_schedule_window", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "updater.scheduler.services.minutes_until_window_end", lambda *a, **k: 45
        )

    async def tick(self, day: int = None):
        if day is not None:
            self.now = datetime(2026, 6, day, 3, 15)
        await self.scheduler._check_and_run()

    def complete_job(self, failed: int = 0):
        """Simulate the in-flight job finishing for its devices (all success, or
        `failed` of them failing → halt-on-first-failure)."""
        rollout = db.get_active_rollout()
        pending = [d for d in db.get_rollout_devices(rollout["id"]) if d["status"] == "pending"]
        statuses = {d["ip"]: "success" for d in pending}
        fail_n = failed
        for d in pending:
            if fail_n <= 0:
                break
            statuses[d["ip"]] = "failed"
            fail_n -= 1
        success = sum(1 for s in statuses.values() if s == "success")
        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            self.scheduler.on_job_completed(
                self.scheduler._current_job_id, success, failed, device_statuses=statuses
            )


# ── Rule 1: one wave per maintenance window ──

@pytest.mark.asyncio
async def test_one_phase_per_window_no_cascade(mock_db, monkeypatch):
    """After the 10% wave finishes, the next wave must NOT run in the same window
    (the anti-cascade rule)."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()  # window 1: pct10
    assert start_update.call_count == 1
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"

    h.complete_job()  # pct10 done -> advances to pct50
    assert db.get_active_rollout()["phase"] == "pct50"

    await h.tick()  # SAME window — must hold
    assert start_update.call_count == 1, "second wave ran in the same window (cascade)"
    assert db.get_active_rollout()["phase"] == "pct50"
    assert scheduler._state == "waiting"


@pytest.mark.asyncio
async def test_phases_progress_one_per_window(mock_db, monkeypatch):
    """Across successive windows the rollout walks pct10 -> pct50 -> pct100,
    exactly one wave per window."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    expected = ["pct10", "pct50", "pct100"]
    seen = []
    for day in (2, 3, 4, 5):
        await h.tick(day=day)
        rollout = db.get_active_rollout()
        if rollout is None:  # rollout completed
            break
        seen.append(rollout["phase"])
        h.complete_job()

    assert seen == expected, f"waves did not advance one-per-window: {seen}"
    assert start_update.call_count == 3


@pytest.mark.asyncio
async def test_held_phase_logs_event(mock_db, monkeypatch):
    """Holding the next wave for the window rule emits an observable event."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()
    h.complete_job()
    await h.tick()  # same window -> held

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'phase_held'"
        ).fetchall()
    assert len(rows) >= 1


# ── Rule 2: the Firmware Hold gates the first fleet wave ──

@pytest.mark.asyncio
async def test_release_date_hold_gates_first_wave(mock_db, monkeypatch):
    """Newly-released firmware (release date today, 6-day hold) must NOT roll the
    first wave — the fleet waits out the Firmware Hold."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=6, firmware=FW_HELD))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)  # release 2026-06-01 + 6 days clears 06-07; today is 06-02
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'blocked_firmware_hold'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_hold_clears_after_days_elapse(mock_db, monkeypatch):
    """Once the firmware's release date + hold days has elapsed, the first wave
    runs without any confirmed device."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=6, firmware=FW_AGED))  # released 2020 -> long clear
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    assert start_update.call_count == 1
    assert scheduler._state == "running"
    assert db.get_active_rollout()["phase"] == "pct10"


@pytest.mark.asyncio
async def test_hold_clears_via_confirmed_device(mock_db, monkeypatch):
    """A device confirmed working on the new firmware clears its family's hold
    early — the first wave runs inside the release-date hold window, and the
    clearing device is logged."""
    _seed_aps(10)
    settings = _settings(hold_days=6, firmware=FW_HELD)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_confirmed("10.0.0.200", target)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)  # still inside the release-date hold, but confirmed -> RUN
    assert start_update.call_count == 1
    assert scheduler._state == "running"
    assert scheduler._hold_clear_reason and "confirmed working" in scheduler._hold_clear_reason
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'firmware_hold_cleared'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_hold_does_not_clear_for_unconfirmed_or_unhealthy_device(mock_db, monkeypatch):
    """Fail-closed: a same-family device on the target version but NOT recorded as
    confirmed (e.g. organic/observed only) does not clear the hold."""
    _seed_aps(10)
    settings = _settings(hold_days=6, firmware=FW_HELD)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    target = scheduler._target_versions(settings, None)["tna-30x"]
    # On the target version + healthy, but no confirmation event recorded.
    db.upsert_access_point("10.0.0.200", "root", "pass", enabled=True, firmware_version=target)
    db.update_ap_status("10.0.0.200", last_seen=datetime.now(timezone.utc).isoformat(), last_error=None)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"


@pytest.mark.asyncio
async def test_confirmed_clear_is_per_family(mock_db, monkeypatch):
    """A confirmed tna-30x device never clears a held tna-303l family (the
    per-family clearing invariant). Because the hold is enforced all-or-nothing,
    a still-held 303l family holds the whole first wave even though tna-30x is
    cleared — so no held firmware (incl. attached CPEs) ever slips out."""
    _seed_aps(10)  # tna-30x devices on 1.0.0
    db.upsert_access_point("10.0.0.50", "root", "pass", enabled=True,
                           firmware_version="1.0.0", model="TNA-303L")
    settings = _settings(hold_days=6, firmware=FW_HELD)
    settings["selected_firmware_303l"] = FW_303L_HELD
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    targets = scheduler._target_versions(settings, None)
    _seed_confirmed("10.0.0.200", targets["tna-30x"])  # confirms tna-30x only

    held, family_holds = scheduler._held_families(settings, None)
    assert "tna-303l" in held          # 303l still held (no confirmed device)
    assert "tna-30x" not in held       # 30x cleared by the confirmed device

    h = _Harness(scheduler, monkeypatch)
    await h.tick(day=2)
    # All-or-nothing: the held 303l family holds the whole first wave.
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"

    # Confirm a 303l device too -> both families cleared -> the wave runs.
    _seed_confirmed("10.0.0.51", targets["tna-303l"], model="TNA-303L")
    await h.tick(day=3)
    assert start_update.call_count == 1
    assert scheduler._state == "running"


# ── Halt-on-first-failure ──

@pytest.mark.asyncio
async def test_halt_on_first_failure_pauses_rollout(mock_db, monkeypatch):
    """A device that fails during a wave pauses the whole rollout — the next wave
    is never assigned (halt-on-first-failure)."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()  # pct10 runs
    assert db.get_active_rollout()["phase"] == "pct10"
    h.complete_job(failed=1)  # one device fails

    rollout = db.get_active_rollout()
    assert rollout["status"] == "paused"
    assert rollout["phase"] == "pct10"  # never advanced to pct50

    await h.tick(day=3)  # next window — paused rollout does not run
    assert start_update.call_count == 1


# ── Pure-logic gate tests (no scheduler/DB) ──

_NOW = "2026-06-10"


def _pct10(window=None):
    return {"status": "active", "phase": "pct10", "last_phase_window": window}


def test_gate_holds_first_wave_when_held():
    """The hold blocks pct10 when the caller reports first_wave_held."""
    may_run, reason = rollout_gate.phase_run_decision(_pct10(), _NOW, first_wave_held=True)
    assert may_run is False
    assert reason == "firmware_hold"


def test_gate_runs_first_wave_when_clear():
    """pct10 runs when the hold is clear."""
    may_run, reason = rollout_gate.phase_run_decision(_pct10(), _NOW, first_wave_held=False)
    assert may_run is True
    assert reason is None


def test_firmware_hold_clear_never_overrides_one_phase_per_window():
    """Even with the hold clear, a wave already run this window waits for the next
    one — the hold clearance never bypasses the anti-cascade rule (Rule 1 first)."""
    rollout = _pct10(window=_NOW)
    may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=False)
    assert may_run is False
    assert reason == "already_ran_this_window"


def test_hold_only_affects_pct10():
    """first_wave_held is ignored for non-pct10 waves (they aren't hold-gated)."""
    for phase in ("pct50", "pct100"):
        rollout = {"status": "active", "phase": phase, "last_phase_window": None}
        may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=True)
        assert may_run is True
        assert reason is None


def test_gate_fail_closed_on_non_active_status():
    """Any non-active status holds (fail-closed)."""
    rollout = {"status": "paused", "phase": "pct10", "last_phase_window": None}
    may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=False)
    assert may_run is False
    assert reason == "status_paused"


# ── Migration: in-flight canary rollouts remap to pct10 ──

def test_create_rollout_starts_at_pct10(mock_db):
    """New rollouts start at the first fleet wave, not the removed canary phase."""
    rid = db.create_rollout(FW)
    assert db.get_rollout(rid)["phase"] == "pct10"


def test_firmware_window_key_uses_schedule_zone_for_naive_scheduler_time():
    """A naive injected time represents schedule-local time on every host zone."""
    key = rollout_gate.firmware_window_key(
        {
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": 3,
            "schedule_end_hour": 4,
            "schedule_timezone": "America/Chicago",
        },
        datetime(2026, 6, 2, 3, 15),
    )

    assert key == ("firmware", "2026-06-02")

# Current-schema managed confirmation boundaries. Freeze both clocks used by
# the real Hold path: scheduler health freshness and database release age.
_HOLD_NOW = datetime(2026, 6, 2, 3, 15, tzinfo=timezone.utc)
_HOLD_VERSION = "1.12.3.55002"


@pytest.fixture
def hold_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _HOLD_NOW.astimezone(tz) if tz else _HOLD_NOW.replace(tzinfo=None)

    monkeypatch.setattr("updater.scheduler.datetime", Clock)
    monkeypatch.setattr("updater.database.datetime", Clock)
    return _HOLD_NOW


def _fleet_hold_settings(fleet, role):
    settings = _settings(hold_days=6, firmware="tna-30x-1.12.3-r55002-20260601-x.bin")
    settings.update(schedule_scope="sites", schedule_scope_data=str(fleet["sites"][0]))
    if role == "switch":
        # Same version across families must not make AP proof count for switches.
        settings["selected_firmware_tns100"] = "tns-100-1.12.3-r55002-20260601-x.bin"
    return settings


def test_managed_fleet_uses_current_schema(mock_db):
    assert db.get_devices(enabled_only=False) == []
    fleet = make_fleet()
    assert len(fleet["sites"]) == 2
    assert len(fleet["aps"]) == 10
    assert len(fleet["switches"]) == 2
    assert {d["model"] for d in fleet["aps"]} == {"TNA-301", "TNA-303L"}
    for device in fleet["aps"] + fleet["switches"]:
        row = db.get_device(device["ip"])
        assert {k: row[k] for k in device} == device
        assert row["enabled"] == 1
        assert row["vendor"] == "tachyon"
        assert row["firmware_version"] == "1.0.0"
    assert len(db.get_devices(enabled_only=False)) == 12


@pytest.mark.parametrize("role", ["ap", "switch"])
@pytest.mark.parametrize("case", [
    "valid", "wrong_version", "unconfirmed", "stale", "missing_seen",
    "invalid_seen", "error", "disabled", "other_site", "wrong_family",
    "at_cutoff", "before_cutoff", "after_cutoff",
])
@pytest.mark.asyncio
async def test_managed_confirmation_hold_boundaries(mock_db, monkeypatch, hold_clock, role, case):
    fleet = make_fleet(aps_per_site=3, switches_per_site=3, ap_models=("TNA-301",))
    settings = _fleet_hold_settings(fleet, role)
    devices = fleet["aps"] if role == "ap" else fleet["switches"]
    prover = devices[3] if case == "other_site" else devices[0]
    family = "tna-30x" if role == "ap" else "tns-100"
    set_version(prover, "1.1.0" if case == "wrong_version" else _HOLD_VERSION)
    if case != "missing_seen":
        seen = hold_clock
        if case == "stale":
            seen -= timedelta(days=2)
        elif case in ("at_cutoff", "before_cutoff", "after_cutoff"):
            seen -= timedelta(hours=24)
            seen += timedelta(microseconds={"at_cutoff": 0, "before_cutoff": -1,
                                            "after_cutoff": 1}[case])
        set_health(prover, "invalid" if case == "invalid_seen" else seen.isoformat(),
                   error="synthetic poll failure" if case == "error" else None)
    if case != "unconfirmed":
        confirm(prover, _HOLD_VERSION)
    if case == "disabled":
        assert db.bulk_set_enabled(role, [prover["ip"]], False) == 1
    if case == "wrong_family":
        update = db.update_ap_status if role == "ap" else db.update_switch_status
        update(prover["ip"], model="TNA-303L" if role == "ap" else "TNA-301")
    if role == "switch":
        # Valid AP proof leaves only the switch family's gate to test.
        ap = fleet["aps"][0]
        set_version(ap, _HOLD_VERSION)
        set_health(ap, hold_clock.isoformat())
        confirm(ap, _HOLD_VERSION)

    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    accepted = case in ("valid", "at_cutoff", "after_cutoff")
    held, holds = scheduler._held_families(settings, None)
    assert (family not in held) is accepted
    assert holds[family]["confirmed_by"] == (prover["ip"] if accepted else None)
    h = _Harness(scheduler, monkeypatch)
    await h.tick()
    assert start_update.call_count == int(accepted)
    assert scheduler._state == ("running" if accepted else "blocked_firmware_hold")
    if accepted:
        call = start_update.call_args.kwargs
        candidates = call["switch_ips"] if role == "switch" else call["ap_ips"]
        assert candidates and prover["ip"] not in candidates
        h.complete_job()
        assert db.get_active_rollout()["phase"] == "pct50"
        await h.tick()
        assert start_update.call_count == 1, "Hold clearance started a second wave in one window"
        assert scheduler._state == "waiting"


@pytest.mark.parametrize("failed_read", ["get_all_access_points_dict", "get_all_switches_dict"])
@pytest.mark.asyncio
async def test_confirmation_fleet_read_failure_keeps_wave_held(mock_db, monkeypatch, hold_clock, failed_read):
    fleet = make_fleet(aps_per_site=3, switches_per_site=3, ap_models=("TNA-301",))
    settings = _fleet_hold_settings(fleet, "switch")
    for prover in (fleet["aps"][0], fleet["switches"][0]):
        set_version(prover, _HOLD_VERSION)
        set_health(prover, hold_clock.isoformat())
        confirm(prover, _HOLD_VERSION)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    real_read = getattr(db, failed_read)

    def read(*, enabled_only=True):
        # Fail the confirmation read; leave the pending-work read available.
        # No Hold or scope helper is replaced.
        if enabled_only:
            raise RuntimeError("synthetic fleet read failure")
        return real_read(enabled_only=enabled_only)

    monkeypatch.setattr(db, failed_read, read)
    assert scheduler._confirmed_family_devices(settings, {}, scheduler._target_versions(settings, None)) == {}
    held, _ = scheduler._held_families(settings, None)
    assert held == {"tna-30x", "tns-100"}
    await _Harness(scheduler, monkeypatch).tick()
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"
