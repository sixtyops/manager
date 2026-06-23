import json

from updater import database as db
from updater.firmware_policy import (
    auto_select_platform_target,
    classify_device_version,
    firmware_file_health,
)


FW_303L_STABLE = "tna-303l-1.12.4-r7782-20251209-sysupgrade.bin"
FW_303L_BETA = "tna-303l-1.15.0-r8515-20260609-sysupgrade.bin"


def test_newer_than_target_is_not_reported_as_behind():
    state = classify_device_version(
        current="1.15.0.8515",
        target="1.12.4.7782",
        allow_downgrade=True,
    )

    assert state.status == "ahead"
    assert state.update_state == "downgrade_available"
    assert state.action == "downgrade"
    assert state.needs_update is True


def test_missing_registered_firmware_is_not_deployable(mock_db, tmp_path):
    db.register_firmware(FW_303L_BETA, source="auto", sha256=None)

    health = firmware_file_health(tmp_path, FW_303L_BETA)

    assert health.deployable is False
    assert health.reason == "file_missing"


def test_auto_target_uses_highest_deployable_version(mock_db, tmp_path):
    (tmp_path / FW_303L_STABLE).write_bytes(b"stable")
    (tmp_path / FW_303L_BETA).write_bytes(b"beta")
    db.register_firmware(FW_303L_STABLE, source="auto", sha256="s" * 64)
    db.register_firmware(FW_303L_BETA, source="auto", sha256="b" * 64)
    db.set_setting("firmware_channels", json.dumps({
        FW_303L_STABLE: "stable",
        FW_303L_BETA: "beta",
    }))

    selected = auto_select_platform_target("tna-303l", tmp_path, beta_enabled=True)

    assert selected == FW_303L_BETA
    assert db.get_setting("selected_firmware_303l", "") == FW_303L_BETA


def test_auto_target_does_not_move_backward_from_deployable_current(mock_db, tmp_path):
    (tmp_path / FW_303L_STABLE).write_bytes(b"stable")
    (tmp_path / FW_303L_BETA).write_bytes(b"beta")
    db.register_firmware(FW_303L_STABLE, source="auto", sha256="s" * 64)
    db.register_firmware(FW_303L_BETA, source="auto", sha256="b" * 64)
    db.set_setting("selected_firmware_303l", FW_303L_BETA)
    db.set_setting("selected_firmware_303l_pinned", "false")
    db.set_setting("firmware_channels", json.dumps({
        FW_303L_STABLE: "stable",
    }))

    selected = auto_select_platform_target("tna-303l", tmp_path, beta_enabled=False)

    assert selected == FW_303L_BETA
    assert db.get_setting("selected_firmware_303l", "") == FW_303L_BETA
