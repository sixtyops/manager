"""Tests for the single firmware target resolver (updater/firmware_target.py)."""

from updater import firmware_target as ft

F30X_NEW = "tna-30x-1.15.0-r55151-20260609-tn-110-prs-squashfs-sysupgrade.bin"
F303L_NEW = "tna-303l-1.15.0-r8515-20260609-sysupgrade.bin"
F303L_OLD = "tna-303l-1.12.4-r7782-20251209-sysupgrade.bin"


def _settings(**kw):
    base = {
        "selected_firmware_30x": "",
        "selected_firmware_303l": "",
        "selected_firmware_tns100": "",
    }
    base.update(kw)
    return base


class TestResolveTarget:
    def test_no_selection(self, tmp_path):
        ti = ft.resolve_target("tna-303l", settings=_settings(), firmware_dir=tmp_path)
        assert ti.filename == "" and ti.version == ""
        assert ti.deployable is False and ti.health_reason == "no_selection"
        assert ti.source == "selection"

    def test_selected_and_present_is_deployable(self, tmp_path):
        (tmp_path / F303L_NEW).write_bytes(b"x")
        ti = ft.resolve_target("tna-303l", settings=_settings(selected_firmware_303l=F303L_NEW),
                               firmware_dir=tmp_path)
        assert ti.filename == F303L_NEW
        assert ti.version == "1.15.0.8515"
        assert ti.deployable is True and ti.health_reason == ""

    def test_selected_but_missing_reports_target_not_a_fallback(self, tmp_path):
        # The newest selected file is missing; an older one IS on disk. The
        # resolver must report the SELECTED (newest) target as not-deployable,
        # never silently fall back to the older on-disk build.
        (tmp_path / F303L_OLD).write_bytes(b"x")
        ti = ft.resolve_target("tna-303l", settings=_settings(selected_firmware_303l=F303L_NEW),
                               firmware_dir=tmp_path)
        assert ti.filename == F303L_NEW          # not the older on-disk file
        assert ti.version == "1.15.0.8515"
        assert ti.deployable is False and ti.health_reason == "missing_file"

    def test_unparseable_filename_is_unknown(self, tmp_path):
        (tmp_path / "mystery.bin").write_bytes(b"x")
        ti = ft.resolve_target("tna-30x", settings=_settings(selected_firmware_30x="mystery.bin"),
                               firmware_dir=tmp_path)
        assert ti.version == "__unknown__"
        assert ti.deployable is False and ti.health_reason == "unparseable_version"

    def test_rollout_pin_overrides_selection(self, tmp_path):
        (tmp_path / F303L_OLD).write_bytes(b"x")
        rollout = {"firmware_file_303l": F303L_OLD}
        ti = ft.resolve_target("tna-303l", settings=_settings(selected_firmware_303l=F303L_NEW),
                               rollout=rollout, firmware_dir=tmp_path)
        assert ti.filename == F303L_OLD and ti.source == "rollout"
        assert ti.deployable is True


class TestResolveFleet:
    def test_all_three_families(self, tmp_path):
        (tmp_path / F30X_NEW).write_bytes(b"x")
        s = _settings(selected_firmware_30x=F30X_NEW, selected_firmware_303l=F303L_NEW)
        fleet = ft.resolve_fleet(settings=s, firmware_dir=tmp_path)
        assert set(fleet) == {"tna-30x", "tna-303l", "tns-100"}
        assert fleet["tna-30x"].deployable is True
        assert fleet["tna-303l"].deployable is False  # selected but file absent
        assert fleet["tns-100"].health_reason == "no_selection"


class TestTargetVersionsShim:
    """Must reproduce the scheduler's prior _target_versions exactly."""

    def test_basic_extraction(self):
        s = _settings(selected_firmware_30x=F30X_NEW, selected_firmware_303l=F303L_OLD)
        t = ft.target_versions(s)
        assert t == {"tna-30x": "1.15.0.55151", "tna-303l": "1.12.4.7782", "tns-100": ""}

    def test_rollout_filename_precedence(self):
        s = _settings(selected_firmware_303l=F303L_OLD)
        t = ft.target_versions(s, {"firmware_file_303l": F303L_NEW})
        assert t["tna-303l"] == "1.15.0.8515"

    def test_rollout_target_version_fallback(self):
        # Filename present but version unparseable -> fall back to rollout column.
        s = _settings(selected_firmware_30x="mystery.bin")
        t = ft.target_versions(s, {"target_version": "9.9.9.1"})
        assert t["tna-30x"] == "9.9.9.1"

    def test_unknown_marker(self):
        s = _settings(selected_firmware_30x="mystery.bin")
        t = ft.target_versions(s)
        assert t["tna-30x"] == "__unknown__"
