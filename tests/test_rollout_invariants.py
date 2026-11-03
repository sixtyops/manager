"""Timing and failure invariants for scheduled firmware queue admission.

The scheduler derives pending devices from current inventory, applies Firmware
Hold per family, and persists each target's cutoff across restarts. One failure
stops later queue admission across families. Maintenance window identity remains
timezone-aware across midnight and daylight-saving changes.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from updater import database as db
from updater.core import engine
from updater.core.gates import MaintenanceSchedule, window_instance_key
from tests.fixtures.fleet import make_fleet, set_version, set_health, confirm
from updater import rollout_gate
from updater import app as app_module
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
    # These wave-gate tests use synthetic CPE inventory without a poller. The
    # dedicated scheduler admission tests cover the fresh-login result path.
    scheduler._precheck_cpe_credentials = AsyncMock(return_value=())
    return scheduler, start_update


def _saved_rollout(members):
    return db.get_rollout(db.create_rollout(
        FW, members=members, settings_snapshot={}
    ))


def test_wave_selects_only_saved_members(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])
    added_ip = "192.0.2.99"
    db.upsert_access_point(
        added_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=True,
    )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [saved_ip, added_ip], settings={"selected_firmware_30x": FW}
    )

    assert selected == [saved_ip]


def test_wave_skips_disabled_saved_members(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=2, switches_per_site=0,
                       ap_models=("TNA-301",))
    disabled_ip, enabled_ip = (device["ip"] for device in fleet["aps"])
    rollout = _saved_rollout([
        {"ip": disabled_ip, "device_type": "ap"},
        {"ip": enabled_ip, "device_type": "ap"},
    ])
    db.upsert_access_point(
        disabled_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=False,
    )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [disabled_ip, enabled_ip], settings={"selected_firmware_30x": FW}
    )

    assert selected == [enabled_ip]


def test_wave_selects_eligible_saved_members(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [saved_ip], settings={"selected_firmware_30x": FW}
    )

    assert selected == [saved_ip]


def test_pct100_scheduler_selects_new_saved_family_stragglers(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])
    rollout["phase"] = "pct100"
    new_ip = "192.0.2.99"
    db.upsert_access_point(
        new_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=True,
    )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [saved_ip, new_ip], settings={"allow_downgrade": "false"}
    )

    assert selected == [saved_ip, new_ip]


def test_pct100_scheduler_excludes_stragglers_inside_update_cooldown(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])
    rollout["phase"] = "pct100"
    new_ip = "192.0.2.99"
    db.upsert_access_point(
        new_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=True,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE devices SET last_firmware_update = ? WHERE ip = ?",
            (datetime.now().isoformat(), new_ip),
        )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout,
        [saved_ip, new_ip],
        settings={
            "allow_downgrade": "false",
            "firmware_update_cooldown_days": "30",
        },
    )

    assert selected == [saved_ip]


@pytest.mark.parametrize("current_version", ["1.12.3.55002"])
def test_pct100_scheduler_excludes_current_or_out_of_scope_stragglers(
    mock_db, current_version
):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])
    rollout["phase"] = "pct100"
    current_ip, out_of_scope_ip = "192.0.2.98", "192.0.2.99"
    for ip in (current_ip, out_of_scope_ip):
        db.upsert_access_point(
            ip, "fixture-user", "synthetic-password", model="TNA-301",
            firmware_version=current_version if ip == current_ip else "1.0.0",
            enabled=True,
        )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [saved_ip, current_ip], settings={"allow_downgrade": "false"}
    )

    assert selected == [saved_ip]


def test_pct100_scheduler_excludes_existing_members_disabled_archived_and_unknown_family(
    mock_db, monkeypatch
):
    fleet = make_fleet(sites=1, aps_per_site=2, switches_per_site=0,
                       ap_models=("TNA-301", "TNA-301"))
    saved_ip, existing_ip = (device["ip"] for device in fleet["aps"])
    rollout = _saved_rollout([
        {"ip": saved_ip, "device_type": "ap"},
        {"ip": existing_ip, "device_type": "ap"},
    ])
    rollout["phase"] = "pct100"
    disabled_ip, archived_ip, unknown_ip = (
        "192.0.2.97", "192.0.2.98", "192.0.2.99"
    )
    db.upsert_access_point(
        disabled_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=False,
    )
    db.upsert_access_point(
        archived_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=True,
    )
    db.upsert_access_point(
        unknown_ip, "fixture-user", "synthetic-password", model="UNLISTED",
        firmware_version="1.0.0", enabled=True,
    )
    inventory = db.get_all_access_points_dict(enabled_only=False)
    inventory[disabled_ip]["enabled"] = 0
    inventory[archived_ip]["archived_at"] = "2026-10-01T00:00:00"
    monkeypatch.setattr(
        db,
        "get_all_access_points_dict",
        lambda enabled_only=True: {
            ip: device for ip, device in inventory.items()
            if not enabled_only or device["enabled"]
        },
    )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout,
        [saved_ip, existing_ip, disabled_ip, archived_ip, unknown_ip],
        settings={"allow_downgrade": "false"},
    )

    assert selected == [saved_ip, existing_ip]


def test_pct100_scheduler_fails_closed_when_straggler_discovery_fails(
    mock_db, monkeypatch
):
    fleet = make_fleet(sites=1, aps_per_site=1, switches_per_site=0,
                       ap_models=("TNA-301",))
    saved_ip = fleet["aps"][0]["ip"]
    rollout = _saved_rollout([{"ip": saved_ip, "device_type": "ap"}])
    rollout["phase"] = "pct100"
    new_ip = "192.0.2.99"
    db.upsert_access_point(
        new_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0", enabled=True,
    )
    monkeypatch.setattr(
        db, "get_rollout_settings_snapshot", lambda _rollout_id: (_ for _ in ()).throw(
            RuntimeError("injected discovery failure")
        )
    )

    scheduler, _ = _make_scheduler()
    selected = scheduler._get_devices_for_phase(
        rollout, [saved_ip, new_ip], settings={"allow_downgrade": "false"}
    )

    assert selected == [saved_ip]


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


def test_straggler_migration_defaults_existing_rows_to_zero(mock_db):
    rollout_id = db.create_rollout(
        FW, members=[{"ip": "192.0.2.10", "device_type": "ap"}],
        settings_snapshot={},
    )
    mock_db.execute("ALTER TABLE rollout_devices DROP COLUMN is_straggler")
    mock_db.execute("ALTER TABLE rollout_devices DROP COLUMN model_family")

    db._migrate(mock_db)
    db._migrate(mock_db)

    columns = {row[1] for row in mock_db.execute("PRAGMA table_info(rollout_devices)")}
    member = mock_db.execute(
        "SELECT is_straggler, model_family FROM rollout_devices WHERE rollout_id = ?",
        (rollout_id,),
    ).fetchone()
    assert "is_straggler" in columns
    assert "model_family" in columns
    assert tuple(member) == (0, None)


def _straggler_rollout():
    original_ip = "192.0.2.10"
    db.upsert_access_point(
        original_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    rollout_id = db.create_rollout(
        FW, members=[{"ip": original_ip, "device_type": "ap"}],
        settings_snapshot={"allow_downgrade": "false"},
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE rollouts SET status = 'active', phase = 'pct100' WHERE id = ?",
            (rollout_id,),
        )
    return rollout_id, original_ip


def _reserve_stragglers(rollout_id, original_ip, candidates, *, window="2026-06-02"):
    return db.reserve_rollout_wave(
        rollout_id=rollout_id,
        phase="pct100",
        window_key=(1, window),
        assignments=[(original_ip, "ap", "pct100")],
        settings={"bank_mode": "single"},
        straggler_candidates=candidates,
    )


def test_pct100_reservation_admits_eligible_saved_family_straggler_once(mock_db):
    rollout_id, original_ip = _straggler_rollout()
    candidate_ip = "192.0.2.99"
    db.upsert_access_point(
        candidate_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )

    assert _reserve_stragglers(
        rollout_id, original_ip, [{"ip": candidate_ip, "device_type": "ap"}]
    )
    candidate = next(
        row for row in db.get_rollout_devices(rollout_id) if row["ip"] == candidate_ip
    )
    assert (candidate["phase_assigned"], candidate["status"], candidate["is_straggler"]) == (
        "pct100", "pending", 1
    )
    assert candidate["model_family"] == "tna-30x"


def test_straggler_eligibility_uses_family_saved_at_rollout_creation(mock_db):
    rollout_id, original_ip = _straggler_rollout()
    changed_family_ip = "192.0.2.88"
    matching_family_ip = "192.0.2.89"
    db.upsert_access_point(
        changed_family_ip, "fixture-user", "synthetic-password", model="TNA-303L",
        firmware_version="1.0.0",
    )
    db.upsert_access_point(
        matching_family_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    # The original member's live model changed after the rollout snapshot.
    db.upsert_access_point(
        original_ip, "fixture-user", "synthetic-password", model="TNA-303L",
        firmware_version="1.0.0",
    )

    assert not _reserve_stragglers(
        rollout_id, original_ip, [{"ip": changed_family_ip, "device_type": "ap"}]
    )
    assert _reserve_stragglers(
        rollout_id, original_ip, [{"ip": matching_family_ip, "device_type": "ap"}]
    )
    rows = {row["ip"]: row for row in db.get_rollout_devices(rollout_id)}
    assert changed_family_ip not in rows
    assert rows[matching_family_ip]["model_family"] == "tna-30x"


@pytest.mark.parametrize("candidate", [
    {"ip": "192.0.2.10", "device_type": "ap"},  # existing member
    {"ip": "192.0.2.99", "device_type": "ap"},  # unsupported model/family
    {"ip": "192.0.2.98", "device_type": "ap"},  # already at the target
    {"ip": "192.0.2.97", "device_type": "switch"},  # wrong role
])
def test_straggler_reservation_rejects_stale_or_unrepresented_candidates(mock_db, candidate):
    rollout_id, original_ip = _straggler_rollout()
    if candidate["ip"] == "192.0.2.99":
        db.upsert_access_point(
            candidate["ip"], "fixture-user", "synthetic-password", model="unknown-model",
            firmware_version="1.0.0",
        )
    elif candidate["ip"] == "192.0.2.98":
        db.upsert_access_point(
            candidate["ip"], "fixture-user", "synthetic-password", model="TNA-301",
            firmware_version="1.12.3.55002",
        )
    elif candidate["ip"] == "192.0.2.97":
        db.upsert_device(
            candidate["ip"], "ap", "fixture-user", "synthetic-password",
            model="TNA-301", firmware_version="1.0.0",
        )

    assert not _reserve_stragglers(
        rollout_id, original_ip, [candidate]
    )
    rows = db.get_rollout_devices(rollout_id)
    assert len(rows) == 1 and rows[0]["ip"] == original_ip
    assert db.get_rollout(rollout_id)["last_phase_window"] is None


def test_straggler_reservation_rejects_new_family_and_late_candidate(mock_db):
    rollout_id, original_ip = _straggler_rollout()
    new_family_ip = "192.0.2.96"
    late_ip = "192.0.2.95"
    db.upsert_access_point(
        new_family_ip, "fixture-user", "synthetic-password", model="TNA-303L",
        firmware_version="1.0.0",
    )
    db.upsert_access_point(
        late_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    assert not _reserve_stragglers(
        rollout_id, original_ip, [{"ip": new_family_ip, "device_type": "ap"}]
    )
    assert _reserve_stragglers(rollout_id, original_ip, [])
    assert not db.reserve_rollout_wave(
        rollout_id=rollout_id, phase="pct100", window_key=(1, "2026-06-03"),
        assignments=[(original_ip, "ap", "pct100")], settings={},
        straggler_candidates=[{"ip": late_ip, "device_type": "ap"}],
    )
    assert late_ip not in {row["ip"] for row in db.get_rollout_devices(rollout_id)}


def test_straggler_reservation_rejects_wrong_phase_or_duplicate_proposal(mock_db):
    rollout_id, original_ip = _straggler_rollout()
    candidate_ip = "192.0.2.93"
    db.upsert_access_point(
        candidate_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    candidate = {"ip": candidate_ip, "device_type": "ap"}

    assert not db.reserve_rollout_wave(
        rollout_id=rollout_id, phase="pct50", window_key=(1, "2026-06-02"),
        assignments=[(original_ip, "ap", "pct50")], settings={},
        straggler_candidates=[candidate],
    )
    with db.get_db() as conn:
        conn.execute("UPDATE rollouts SET status = 'paused' WHERE id = ?", (rollout_id,))
    assert not _reserve_stragglers(rollout_id, original_ip, [candidate])
    with db.get_db() as conn:
        conn.execute("UPDATE rollouts SET status = 'active' WHERE id = ?", (rollout_id,))
    assert not _reserve_stragglers(rollout_id, original_ip, [candidate, candidate])
    assert candidate_ip not in {row["ip"] for row in db.get_rollout_devices(rollout_id)}
    assert db.get_rollout(rollout_id)["last_phase_window"] is None


def test_straggler_reservation_rejects_missing_saved_family_artifact(mock_db):
    original_ip = "192.0.2.11"
    candidate_ip = "192.0.2.91"
    for ip in (original_ip, candidate_ip):
        db.upsert_access_point(
            ip, "fixture-user", "synthetic-password", model="TNA-303L",
            firmware_version="1.0.0",
        )
    rollout_id = db.create_rollout(
        FW, members=[{"ip": original_ip, "device_type": "ap"}],
        settings_snapshot={},
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE rollouts SET status = 'active', phase = 'pct100' WHERE id = ?",
            (rollout_id,),
        )

    assert not _reserve_stragglers(
        rollout_id, original_ip, [{"ip": candidate_ip, "device_type": "ap"}]
    )
    assert {row["ip"] for row in db.get_rollout_devices(rollout_id)} == {original_ip}


def test_straggler_insert_and_wave_reservation_roll_back_together(mock_db):
    rollout_id, original_ip = _straggler_rollout()
    candidate_ip = "192.0.2.94"
    db.upsert_access_point(
        candidate_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    db.set_rollout_settings_snapshot(rollout_id, {"old": "snapshot"})
    with db.get_db() as conn:
        conn.execute(
            """CREATE TRIGGER reject_final_assignment BEFORE UPDATE OF phase_assigned
               ON rollout_devices WHEN NEW.ip = '192.0.2.10'
               BEGIN SELECT RAISE(ABORT, 'injected final assignment failure'); END"""
        )

    with pytest.raises(Exception, match="injected final assignment failure"):
        _reserve_stragglers(
            rollout_id, original_ip, [{"ip": candidate_ip, "device_type": "ap"}]
        )

    assert {row["ip"] for row in db.get_rollout_devices(rollout_id)} == {original_ip}
    assert db.get_rollout(rollout_id)["last_phase_window"] is None
    assert db.get_rollout_settings_snapshot(rollout_id) == {"old": "snapshot"}


def test_straggler_reservation_uses_pinned_clock(mock_db, monkeypatch):
    rollout_id, original_ip = _straggler_rollout()
    fixed = datetime(2026, 6, 2, 3, 15)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=tz) if tz else fixed

    monkeypatch.setattr(db, "datetime", FixedDateTime)
    assert _reserve_stragglers(rollout_id, original_ip, [])
    assert db.get_rollout(rollout_id)["updated_at"] == fixed.isoformat()


def test_concurrent_pct100_reservations_admit_candidate_once(mock_db, monkeypatch, tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor

    rollout_id, original_ip = _straggler_rollout()
    candidate_ip = "192.0.2.92"
    db.upsert_access_point(
        candidate_ip, "fixture-user", "synthetic-password", model="TNA-301",
        firmware_version="1.0.0",
    )
    path = tmp_path / "straggler-reservation.db"
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
    candidate = [{"ip": candidate_ip, "device_type": "ap"}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: _reserve_stragglers(rollout_id, original_ip, candidate), range(2)
        ))

    assert sorted(results) == [False, True]
    rows = db.get_rollout_devices(rollout_id)
    admitted = [row for row in rows if row["ip"] == candidate_ip]
    assert len(admitted) == 1 and admitted[0]["is_straggler"] == 1


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


def test_activate_rollout_requires_every_member_family_unheld(mock_db, hold_clock):
    rollout_id = db.create_rollout(
        FW_HELD,
        firmware_file_tns100="tns-100-1.12.3-r55002-20260601-x.bin",
        members=[
            {"ip": "192.0.2.10", "device_type": "ap"},
            {"ip": "192.0.2.11", "device_type": "switch"},
        ],
        settings_snapshot={"bank_mode": "both"},
        now=hold_clock.replace(tzinfo=None),
    )

    families = {"tna-30x", "tns-100"}
    assert not db.activate_rollout_if_unheld(rollout_id, families, {"tns-100"})
    assert db.get_rollout(rollout_id)["status"] == "waiting_hold"
    assert db.activate_rollout_if_unheld(rollout_id, families, set())
    assert db.get_rollout(rollout_id)["status"] == "active"
    assert db.get_rollout(rollout_id)["updated_at"] == hold_clock.replace(tzinfo=None).isoformat()


def test_activate_rollout_rolls_back_when_status_write_fails(mock_db, hold_clock):
    rollout_id = db.create_rollout(
        FW,
        members=[{"ip": "192.0.2.10", "device_type": "ap"}],
        settings_snapshot={"bank_mode": "both"},
        now=hold_clock.replace(tzinfo=None),
    )
    with db.get_db() as conn:
        conn.execute(
            """CREATE TRIGGER reject_rollout_activation BEFORE UPDATE OF status ON rollouts
               WHEN NEW.status = 'active'
               BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"""
        )

    with pytest.raises(Exception, match="injected activation failure"):
        db.activate_rollout_if_unheld(rollout_id, {"tna-30x"}, set())

    assert db.get_rollout(rollout_id)["status"] == "waiting_hold"


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


def test_engine_forwards_straggler_snapshot_to_wave_reservation(mock_db, monkeypatch):
    rollout_id = db.create_rollout(FW)
    rollout = {"id": rollout_id, "phase": "pct100"}
    candidates = [{"ip": "192.0.2.99", "device_type": "ap"}]
    captured = {}

    def reserve(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(db, "reserve_rollout_wave", reserve)

    assert engine.start_wave(
        rollout,
        {"ap_ips": ["192.0.2.10"], "switch_ips": ["192.0.2.11"]},
        {"bank_mode": "single"},
        (1, "2026-06-02"),
        straggler_candidates=candidates,
    )
    assert captured["straggler_candidates"] is candidates
    assert captured["assignments"] == [
        ("192.0.2.10", "ap", "pct100"),
        ("192.0.2.11", "switch", "pct100"),
    ]
    assert captured["settings"] == {"bank_mode": "single"}


@pytest.mark.parametrize(("rollout", "batch"), [
    (None, {"ap_ips": ["192.0.2.10"]}),
    ({"id": 0, "phase": "pct100"}, {"ap_ips": ["192.0.2.10"]}),
    ({"id": True, "phase": "pct100"}, {"ap_ips": ["192.0.2.10"]}),
    ({"id": 1, "phase": "unknown"}, {"ap_ips": ["192.0.2.10"]}),
    ({"id": 1, "phase": "pct100"}, {"ap_ips": []}),
    ({"id": 1, "phase": "pct100"}, {"ap_ips": [""]}),
    ({"id": 1, "phase": "pct100"}, {"ap_ips": [None]}),
    ({"id": 1, "phase": "pct100"}, {"ap_ips": "192.0.2.10"}),
])
def test_engine_rejects_invalid_wave_before_reservation(rollout, batch, monkeypatch):
    reserve_calls = []
    monkeypatch.setattr(db, "reserve_rollout_wave", lambda **kwargs: reserve_calls.append(kwargs))

    assert not engine.start_wave(rollout, batch, {}, (1, "2026-06-02"))
    assert reserve_calls == []


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


def test_overnight_window_keeps_its_start_date_key():
    schedule = MaintenanceSchedule(
        schedule_id=7, days=frozenset({0}), start_hour=23, end_hour=2, tz="UTC"
    )
    before_midnight = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 6, 2, 1, 30, tzinfo=timezone.utc)
    assert window_instance_key(schedule, before_midnight) == (7, "2026-06-01")
    assert window_instance_key(schedule, after_midnight) == (7, "2026-06-01")


class _Harness:
    """Drive scheduler ticks at a fixed clock with environmental checks stubbed."""

    def __init__(self, scheduler, monkeypatch):
        self.scheduler = scheduler
        self.now = datetime(2026, 6, 2, 3, 15, tzinfo=ZoneInfo("America/Chicago"))

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
            self.now = datetime(2026, 6, day, 3, 15, tzinfo=ZoneInfo("America/Chicago"))
        import hashlib
        for key in ("selected_firmware_30x", "selected_firmware_303l", "selected_firmware_tns100"):
            filename = db.get_setting(key)
            if filename and not db.get_firmware_sha256(filename):
                db.register_firmware(filename, sha256=hashlib.sha256(filename.encode()).hexdigest())
        await self.scheduler._check_and_run()

    def complete_job(self, failed: int = 0):
        context = self.scheduler._queue_job_context[self.scheduler._current_job_id]
        entries = context["entries"]
        statuses = {entry["ip"]: "success" for entry in entries}
        if failed and entries:
            statuses[entries[0]["ip"]] = "failed"
        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            self.scheduler.on_job_completed(
                self.scheduler._current_job_id, len(entries) - failed, failed,
                device_statuses=statuses,
            )


@pytest.mark.asyncio
async def test_scheduler_dispatches_exact_serial_queue(mock_db, monkeypatch):
    _seed_aps(2)
    parent_ip = "192.0.2.10"
    db.upsert_cpe(parent_ip, {
        "ip": "192.0.2.50", "model": "TNA-301", "firmware_version": "1.0.0",
        "auth_status": "ok",
    })
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()

    await _Harness(scheduler, monkeypatch).tick()

    start_update.assert_awaited_once()
    kwargs = start_update.await_args.kwargs
    assert [entry["role"] for entry in kwargs["device_entries"]] == ["cpe", "ap", "ap"]
    assert kwargs["concurrency"] == 1
    assert kwargs["bank_mode"] == "one"
    assert kwargs["device_entries"][0]["parent_ap"] == parent_ip


@pytest.mark.asyncio
async def test_queue_failure_stops_later_windows_and_target_changes(mock_db, monkeypatch):
    _seed_aps(2)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)

    await harness.tick()
    harness.complete_job(failed=1)
    stop = db.get_firmware_window_stop(("firmware", "2026-06-02"))
    assert stop and stop["device_ip"]
    assert db.get_unresolved_firmware_window_stops()

    settings = db.get_all_settings()
    settings["selected_firmware_30x"] = _dated("tna-30x-1.12.4-r55003", 30)
    db.set_settings(settings)
    await harness.tick(day=3)

    assert start_update.await_count == 1
    assert db.get_unresolved_firmware_window_stops()[0]["device_ip"] == stop["device_ip"]
    assert scheduler._state == "blocked_safety"


@pytest.mark.asyncio
async def test_held_family_waits_without_blocking_an_unheld_family(mock_db, monkeypatch):
    fleet = make_fleet(sites=1, aps_per_site=2, switches_per_site=0,
                       ap_models=("TNA-301", "TNA-303L"))
    settings = _settings(hold_days=6, firmware=FW_AGED)
    settings["selected_firmware_303l"] = FW_303L_HELD
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()

    await _Harness(scheduler, monkeypatch).tick()

    assert start_update.await_count == 1
    entries = start_update.await_args.kwargs["device_entries"]
    assert [entry["ip"] for entry in entries] == [fleet["aps"][0]["ip"]]
    assert scheduler._state == "running"


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
    should_run = accepted or role == "switch"
    assert start_update.call_count == int(should_run)
    assert scheduler._state == ("running" if should_run else "blocked_firmware_hold")
    if accepted:
        call = start_update.call_args.kwargs
        candidates = [
            entry["ip"] for entry in call["device_entries"]
            if entry["role"] == role
        ]
        assert candidates and prover["ip"] not in candidates


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
