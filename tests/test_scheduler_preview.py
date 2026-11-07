"""Read-only API preview of the next scheduled firmware window."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module
from updater import database as db
from updater.scheduler import AutoUpdateScheduler


FIRMWARE = "tna-30x-2.0.0-r200.bin"


def _configure_preview(mock_db, *, schedule_enabled="true", safety_ready="true"):
    db.set_settings({
        "schedule_enabled": schedule_enabled,
        "scheduled_rollout_safety_ready": safety_ready,
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "schedule_start_hour": "3",
        "schedule_end_hour": "6",
        "schedule_scope": "all",
        "schedule_timezone": "America/Chicago",
        "timezone": "America/Chicago",
        "firmware_window_ramp_minutes": "[30, 120]",
        "firmware_update_cooldown_days": "0",
        "firmware_canary_hold_days": "6",
        "selected_firmware_30x": FIRMWARE,
        "selected_firmware_303l": "",
        "selected_firmware_tns100": "",
    })
    db.register_firmware(FIRMWARE, sha256="a" * 64)
    db.upsert_access_point(
        "192.0.2.10", "operator", "credential-value", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )


def _set_scheduler(monkeypatch, available=True):
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
    monkeypatch.setattr(app_module, "get_scheduler", lambda: scheduler if available else None)


def _snapshot(conn):
    return tuple(conn.iterdump())


def test_preview_is_read_only_and_omits_credentials(viewer_client, mock_db, monkeypatch):
    _configure_preview(mock_db)
    _set_scheduler(monkeypatch)
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="America/Chicago"))
    monkeypatch.setattr(
        app_module, "get_poller",
        lambda: (_ for _ in ()).throw(AssertionError("preview must not access a device poller")),
    )
    before = _snapshot(mock_db)

    response = viewer_client.get("/api/scheduler/preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "America/Chicago"
    assert payload["window_start"]
    assert payload["window_end"]
    assert payload["cutoff_utc"]
    assert payload["ramp_limit_minutes"] == 30
    assert payload["safety_ready"] is True
    assert payload["schedule_enabled"] is True
    assert payload["auto_update_enabled"] is True
    assert [(device["role"], device["ip"]) for device in payload["devices"]] == [
        ("ap", "192.0.2.10"),
    ]
    assert payload["devices"][0]["current_firmware"] == "1.0.0"
    assert payload["devices"][0]["target_firmware"] == "2.0.0.200"
    assert payload["devices"][0]["needs_update"] is True
    serialized = str(payload).lower()
    assert "credential-value" not in serialized
    assert "password" not in serialized
    assert _snapshot(mock_db) == before


@pytest.mark.parametrize(
    ("schedule_enabled", "safety_ready", "scheduler_available", "expected_reason"),
    [
        ("true", "false", True, "Scheduled firmware updates are paused for safety qualification."),
        ("true", "true", False, "Scheduler is not initialized"),
        ("false", "true", True, "Auto-update is off"),
    ],
)
def test_preview_reports_global_hold_reasons(
    viewer_client, mock_db, monkeypatch,
    schedule_enabled, safety_ready, scheduler_available, expected_reason,
):
    _configure_preview(
        mock_db, schedule_enabled=schedule_enabled, safety_ready=safety_ready,
    )
    _set_scheduler(monkeypatch, scheduler_available)

    response = viewer_client.get("/api/scheduler/preview")

    assert response.status_code == 200
    payload = response.json()
    device = payload["devices"][0]
    assert device["held"] is True
    assert device["hold_reason"].startswith(expected_reason)
    if not scheduler_available:
        assert payload["scheduler_enabled"] is False
    if schedule_enabled == "false":
        assert payload["auto_update_enabled"] is False


def test_preview_reports_firmware_hold_reason(viewer_client, mock_db, monkeypatch):
    _configure_preview(mock_db)
    _set_scheduler(monkeypatch)

    response = viewer_client.get("/api/scheduler/preview")

    assert response.status_code == 200
    device = response.json()["devices"][0]
    assert device["needs_update"] is True
    assert device["held"] is True
    assert device["hold_reason"].startswith("Firmware hold")


def test_preview_reports_unauthenticated_cpe_only_when_behind(viewer_client, mock_db, monkeypatch):
    _configure_preview(mock_db)
    db.set_setting("selected_firmware_303l", "tna-303l-2.0.0-r200.bin")
    db.register_firmware("tna-303l-2.0.0-r200.bin", sha256="b" * 64)
    db.upsert_cpe("192.0.2.10", {
        "ip": "192.0.2.11", "model": "TNA-303L", "firmware_version": "1.0.0",
        "auth_status": "failed",
    })
    db.upsert_cpe("192.0.2.10", {
        "ip": "192.0.2.12", "model": "TNA-303L", "firmware_version": "2.0.0.200",
        "auth_status": "failed",
    })
    _set_scheduler(monkeypatch)

    response = viewer_client.get("/api/scheduler/preview")

    assert response.status_code == 200
    by_ip = {item["ip"]: item for item in response.json()["devices"]}
    assert by_ip["192.0.2.11"]["needs_update"] is True
    assert by_ip["192.0.2.11"]["held"] is True
    assert by_ip["192.0.2.11"]["hold_reason"] == "CPE credentials are not verified"
    assert by_ip["192.0.2.12"]["needs_update"] is False
    assert by_ip["192.0.2.12"]["held"] is False


def test_preview_marks_malformed_firmware_owner_and_later_cpe_duplicate_held(
    viewer_client, mock_db, monkeypatch,
):
    _configure_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.10", "operator", "credential-value", enabled=True,
        model="TNA-301", firmware_version=None,
    )
    cpe_firmware = "tna-303l-2.0.0-r200.bin"
    db.set_setting("selected_firmware_303l", cpe_firmware)
    db.register_firmware(cpe_firmware, sha256="b" * 64)
    db.upsert_cpe("192.0.2.10", {
        "ip": "192.0.2.10", "model": "TNA-303L", "firmware_version": "1.0.0",
        "auth_status": "ok",
    })
    _set_scheduler(monkeypatch)

    response = viewer_client.get("/api/scheduler/preview")

    assert response.status_code == 200
    rows = [item for item in response.json()["devices"] if item["ip"] == "192.0.2.10"]
    assert [item["role"] for item in rows] == ["cpe", "ap"]
    assert all(item["held"] for item in rows)
    assert all("duplicate management address" in item["hold_reason"] for item in rows)


def test_preview_reports_cooldown_as_hold_for_behind_device(viewer_client, mock_db, monkeypatch):
    _configure_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.10", "operator", "credential-value", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE devices SET last_firmware_update = ? WHERE ip = ?",
            (datetime.now().isoformat(), "192.0.2.10"),
        )
    db.set_setting("firmware_update_cooldown_days", "30")
    _set_scheduler(monkeypatch)

    response = viewer_client.get("/api/scheduler/preview")

    device = response.json()["devices"][0]
    assert device["needs_update"] is True
    assert device["held"] is True
    assert "cooldown" in device["hold_reason"]


def _fixed_preview(mock_db, *, now=None):
    _configure_preview(mock_db)
    fixed_now = now or datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
    return scheduler, db.get_all_settings(), fixed_now


def test_preview_orders_resolved_failed_device_first(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.1", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    db.upsert_access_point(
        "192.0.2.2", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    stop = db.stop_firmware_window(
        ("firmware", "2026-11-01"), "prior-job", "192.0.2.2", "snapshot", "prior failure",
    )
    db.resolve_firmware_window_stop(stop["id"], "admin", "Reviewed failure")

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    assert [item["ip"] for item in preview["devices"]] == [
        "192.0.2.2", "192.0.2.1", "192.0.2.10",
    ]


@pytest.mark.parametrize("kind", ["freeze", "current_stop", "unresolved_stop"])
def test_preview_reports_scheduler_admission_holds(mock_db, kind):
    scheduler, settings, now = _fixed_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.10", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    current_key = ("firmware", "2026-11-02")
    if kind == "freeze":
        db.create_freeze_window("planned freeze", "2026-11-02", "2026-11-02")
        expected = "Maintenance freeze: planned freeze"
    elif kind == "current_stop":
        db.stop_firmware_window(current_key, "current-job", "192.0.2.10", "snapshot", "failed")
        expected = "This maintenance window stopped after a device failure"
    else:
        db.stop_firmware_window(("firmware", "2026-11-01"), "prior-job", "192.0.2.10", "snapshot", "failed")
        expected = "Firmware queue stopped at 192.0.2.10"

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    device = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert device["held"] is True
    assert device["hold_reason"].startswith(expected)


def test_preview_holds_behind_devices_after_persisted_cutoff(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.10", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    cutoff = "2026-11-02T09:45:00+00:00"
    db.record_firmware_window_admission(
        "tna-30x", "a" * 64, ("firmware", "2026-11-02"), cutoff,
    )

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    device = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert device["needs_update"] is True
    assert device["held"] is True
    assert device["hold_reason"] == "Firmware window cutoff has passed; device will retry next window"


def test_preview_holds_recovery_at_target_without_saved_serial(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.10", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="2.0.0.200",
    )
    stop = db.stop_firmware_window(
        ("firmware", "2026-11-01"), "prior-job", "192.0.2.10", "scheduled_update", "failed",
    )
    db.resolve_firmware_window_stop(stop["id"], "admin", "Reviewed failure")

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    device = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert device["needs_update"] is False
    assert device["held"] is True
    assert "no saved serial" in device["hold_reason"]


def test_preview_checks_freeze_at_future_window_start(mock_db):
    future_now = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)
    scheduler, settings, _ = _fixed_preview(mock_db, now=future_now)
    db.create_freeze_window("future freeze", "2026-11-02", "2026-11-02")

    preview = scheduler.get_preview(settings, future_now, "America/Chicago")

    assert preview["window_start"].startswith("2026-11-02T03:00:00")
    device = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert device["held"] is True
    assert device["hold_reason"] == "Maintenance freeze: future freeze"


def test_preview_reports_and_applies_cutoffs_per_family(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    settings["firmware_canary_hold_days"] = "0"
    db.set_setting("firmware_canary_hold_days", "0")
    second_firmware = "tna-303l-2.0.0-r200.bin"
    db.set_setting("selected_firmware_303l", second_firmware)
    settings["selected_firmware_303l"] = second_firmware
    db.register_firmware(second_firmware, sha256="b" * 64)
    db.upsert_access_point(
        "192.0.2.20", "operator", "secret", enabled=True,
        model="TNA-303L", firmware_version="1.0.0",
    )
    key = ("firmware", "2026-11-02")
    db.record_firmware_window_admission(
        "tna-30x", "a" * 64, ("firmware", "2026-11-01"), "2026-11-01T09:30:00+00:00",
    )
    db.record_firmware_window_admission("tna-30x", "a" * 64, key, "2026-11-02T11:00:00+00:00")
    db.record_firmware_window_admission("tna-303l", "b" * 64, key, "2026-11-02T09:00:00+00:00")

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    by_ip = {item["ip"]: item for item in preview["devices"]}
    assert by_ip["192.0.2.10"]["held"] is False
    assert by_ip["192.0.2.20"]["held"] is True
    assert by_ip["192.0.2.20"]["hold_reason"] == "Firmware window cutoff has passed; device will retry next window"
    assert preview["family_timing"]["tna-30x"] == {
        "ramp_ordinal": 2, "ramp_limit_minutes": 120,
        "cutoff_utc": "2026-11-02T11:00:00+00:00",
    }
    assert preview["family_timing"]["tna-303l"] == {
        "ramp_ordinal": 1, "ramp_limit_minutes": 30,
        "cutoff_utc": "2026-11-02T09:00:00+00:00",
    }


def test_preview_includes_unsupported_model_as_held_when_target_selected(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    db.upsert_access_point(
        "192.0.2.30", "operator", "secret", enabled=True,
        model="TNA-UNKNOWN", firmware_version="1.0.0",
    )

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    device = next(item for item in preview["devices"] if item["ip"] == "192.0.2.30")
    assert device["held"] is True
    assert "Unsupported model" in device["hold_reason"]

    supported = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert supported["held"] is True
    assert "unsupported" in supported["hold_reason"].lower()


def test_preview_keeps_unreadable_selected_target_and_holds_update_queue(mock_db):
    scheduler, settings, now = _fixed_preview(mock_db)
    settings["firmware_canary_hold_days"] = "0"
    settings["selected_firmware_303l"] = "malformed-target.bin"
    db.upsert_access_point(
        "192.0.2.30", "operator", "secret", enabled=True,
        model="TNA-303L", firmware_version="1.0.0",
    )

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    malformed = next(item for item in preview["devices"] if item["ip"] == "192.0.2.30")
    assert malformed["needs_update"] is False
    assert malformed["held"] is True
    assert malformed["target_firmware"] is None
    assert "Target firmware version is unreadable" in malformed["hold_reason"]

    supported = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert supported["needs_update"] is True
    assert supported["held"] is True
    assert "queue held" in supported["hold_reason"].lower()


@pytest.mark.parametrize("reported_firmware", [None, ""])
def test_preview_keeps_unreadable_reported_firmware_and_holds_update_queue(
    mock_db, reported_firmware,
):
    scheduler, settings, now = _fixed_preview(mock_db)
    settings["firmware_canary_hold_days"] = "0"
    db.upsert_access_point(
        "192.0.2.30", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version=reported_firmware,
    )

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    malformed = next(item for item in preview["devices"] if item["ip"] == "192.0.2.30")
    assert malformed["needs_update"] is False
    assert malformed["held"] is True
    assert malformed["current_firmware"] in (None, "")
    assert "Reported firmware version is unreadable" in malformed["hold_reason"]

    supported = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10")
    assert supported["needs_update"] is True
    assert supported["held"] is True
    assert "queue held" in supported["hold_reason"].lower()


@pytest.mark.parametrize(
    ("bad_ip", "reason"),
    [
        (None, "has no management address"),
        ("not-an-ip", "invalid management address"),
        ("192.0.2.10", "duplicate management address"),
    ],
)
def test_preview_keeps_bad_address_rows_held(mock_db, monkeypatch, bad_ip, reason):
    scheduler, settings, now = _fixed_preview(mock_db)
    original = db.get_all_access_points_dict
    aps = original(enabled_only=True)
    bad = dict(aps["192.0.2.10"])
    bad["ip"] = bad_ip
    if bad_ip == "192.0.2.10":
        bad["model"] = "TNA-301"
        db.upsert_access_point(
            "192.0.2.11", "operator", "secret", enabled=True,
            model="TNA-301", firmware_version="1.0.0",
        )
        extra = dict(bad)
        extra["ip"] = "192.0.2.10 "
        aps["192.0.2.11"] = extra
    aps["192.0.2.10"] = bad
    monkeypatch.setattr(db, "get_all_access_points_dict", lambda enabled_only=True: aps)

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    row = next(item for item in preview["devices"] if item["ip"] == bad_ip)
    assert row["held"] is True
    assert reason in row["hold_reason"]
    if bad_ip == "192.0.2.10":
        duplicate = next(item for item in preview["devices"] if item["ip"] == "192.0.2.10 ")
        assert duplicate["held"] is True
        assert "duplicate management address" in duplicate["hold_reason"]


def test_preview_holds_all_update_rows_when_duplicate_address_breaks_queue(mock_db, monkeypatch):
    scheduler, settings, now = _fixed_preview(
        mock_db, now=datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc),
    )
    settings["firmware_canary_hold_days"] = "0"
    db.upsert_access_point(
        "192.0.2.11", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    db.upsert_access_point(
        "192.0.2.20", "operator", "secret", enabled=True,
        model="TNA-301", firmware_version="1.0.0",
    )
    original = db.get_all_access_points_dict
    aps = original(enabled_only=True)
    duplicate = dict(aps["192.0.2.10"])
    duplicate["ip"] = "192.0.2.10 "
    aps["192.0.2.11"] = duplicate
    monkeypatch.setattr(db, "get_all_access_points_dict", lambda enabled_only=True: aps)

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    update_rows = [item for item in preview["devices"] if item["needs_update"]]
    assert {item["ip"] for item in update_rows} == {
        "192.0.2.10", "192.0.2.10 ", "192.0.2.20",
    }
    assert all(item["held"] for item in update_rows)
    assert "duplicate management address" in next(
        item["hold_reason"] for item in update_rows if item["ip"] == "192.0.2.10 "
    )
    assert "Scheduled queue held" in next(
        item["hold_reason"] for item in update_rows if item["ip"] == "192.0.2.20"
    )


def test_preview_ignores_missing_hash_for_already_held_family(mock_db, monkeypatch):
    scheduler, settings, now = _fixed_preview(mock_db)
    second_firmware = "tna-303l-2.0.0-r200.bin"
    settings["selected_firmware_303l"] = second_firmware
    settings["firmware_canary_hold_days"] = "0"
    db.upsert_access_point(
        "192.0.2.20", "operator", "secret", enabled=True,
        model="TNA-303L", firmware_version="1.0.0",
    )
    monkeypatch.setattr(
        scheduler, "_held_families",
        lambda *_args: ({"tna-303l"}, {"tna-303l": {"held": True}}),
    )
    original_hash = db.get_firmware_sha256
    monkeypatch.setattr(
        db, "get_firmware_sha256",
        lambda artifact: None if artifact == second_firmware else original_hash(artifact),
    )

    preview = scheduler.get_preview(settings, now, "America/Chicago")

    assert preview["hold_reason"] is None
    assert set(preview["family_timing"]) == {"tna-30x"}
    held = next(item for item in preview["devices"] if item["ip"] == "192.0.2.20")
    assert held["held"] is True
    assert "Firmware hold" in held["hold_reason"]
