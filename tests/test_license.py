"""Tests for license management and feature gating."""

import pytest
from unittest.mock import patch, AsyncMock

from updater.license import (
    Feature, LicenseState, LicenseStatus, LicenseTier,
    get_billable_device_count, get_nag_info, FREE_DEVICE_NAG_THRESHOLD,
)


class TestLicenseState:
    """Unit tests for LicenseState dataclass."""

    def test_free_state_defaults(self):
        state = LicenseState()
        assert state.tier == LicenseTier.FREE
        assert state.status == LicenseStatus.FREE
        assert not state.is_pro()

    def test_active_pro_state(self):
        state = LicenseState(tier=LicenseTier.PRO, status=LicenseStatus.ACTIVE)
        assert state.is_pro()

    def test_grace_is_still_pro(self):
        state = LicenseState(tier=LicenseTier.PRO, status=LicenseStatus.GRACE)
        assert state.is_pro()

    def test_expired_is_not_pro(self):
        state = LicenseState(tier=LicenseTier.FREE, status=LicenseStatus.EXPIRED)
        assert not state.is_pro()

    def test_invalid_is_not_pro(self):
        state = LicenseState(tier=LicenseTier.FREE, status=LicenseStatus.INVALID)
        assert not state.is_pro()

    def test_feature_enabled_on_pro(self):
        state = LicenseState(tier=LicenseTier.PRO, status=LicenseStatus.ACTIVE)
        for feature in Feature:
            assert state.is_feature_enabled(feature)

    def test_feature_disabled_on_free(self):
        state = LicenseState()
        for feature in Feature:
            assert not state.is_feature_enabled(feature)

    def test_to_dict_redacts_key(self):
        state = LicenseState(license_key="SECRET-KEY-123")
        d = state.to_dict()
        assert "license_key" not in d
        assert d["has_key"] is True

    def test_to_dict_no_key(self):
        state = LicenseState()
        d = state.to_dict()
        assert d["has_key"] is False
        assert d["tier"] == "free"
        assert d["status"] == "free"


class TestDeviceCounting:
    """Tests for billable device counting."""

    def test_count_empty_db(self, mock_db):
        count = get_billable_device_count()
        assert count == 0

    def test_count_aps_only(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "admin", "pass", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.2", "admin", "pass", 1),
        )
        mock_db.commit()
        assert get_billable_device_count() == 2

    def test_count_excludes_disabled(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "admin", "pass", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.2", "admin", "pass", 0),
        )
        mock_db.commit()
        assert get_billable_device_count() == 1

    def test_count_aps_plus_switches(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "admin", "pass", 1),
        )
        mock_db.execute(
            "INSERT INTO switches (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.1.1", "admin", "pass", 1),
        )
        mock_db.commit()
        assert get_billable_device_count() == 2

    def test_count_excludes_cpes(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "admin", "pass", 1),
        )
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip) VALUES (?, ?)",
            ("10.0.0.1", "10.0.0.100"),
        )
        mock_db.commit()
        assert get_billable_device_count() == 1  # CPE not counted


class TestNagInfo:
    """Tests for the free-tier nag threshold."""

    def test_no_nag_when_below_threshold(self, mock_db):
        import updater.license as lic
        old_force = lic._FORCE_PRO
        lic._FORCE_PRO = False
        lic._license_state = None
        try:
            info = get_nag_info()
            assert not info["should_nag"]
            assert info["threshold"] == FREE_DEVICE_NAG_THRESHOLD
        finally:
            lic._FORCE_PRO = old_force
            lic._license_state = None

    def test_nag_when_above_threshold_free(self, mock_db):
        import updater.license as lic
        old_force = lic._FORCE_PRO
        lic._FORCE_PRO = False
        lic._license_state = None
        try:
            # Add 11 APs
            for i in range(11):
                mock_db.execute(
                    "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
                    (f"10.0.0.{i+1}", "admin", "pass", 1),
                )
            mock_db.commit()
            info = get_nag_info()
            assert info["should_nag"]
            assert info["billable_count"] == 11
        finally:
            lic._FORCE_PRO = old_force
            lic._license_state = None

    def test_no_nag_when_pro(self, mock_db, pro_license):
        # Even with many devices, pro users don't get nagged
        for i in range(20):
            mock_db.execute(
                "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
                (f"10.0.0.{i+1}", "admin", "pass", 1),
            )
        mock_db.commit()
        info = get_nag_info()
        assert not info["should_nag"]
        assert info["is_pro"]


class TestFeatureGating:
    """Tests for feature gating on specific features."""

    def test_all_features_gated(self):
        """Every Feature enum member should be PRO-only."""
        state = LicenseState()
        for feature in Feature:
            assert not state.is_feature_enabled(feature), f"{feature.value} should be gated on free tier"

    def test_all_features_enabled_pro(self):
        state = LicenseState(tier=LicenseTier.PRO, status=LicenseStatus.ACTIVE)
        for feature in Feature:
            assert state.is_feature_enabled(feature), f"{feature.value} should be enabled on pro tier"


class TestAPIGating:
    """Tests for license gating on API endpoints."""

    def test_update_device_blocked_free(self, authed_client, free_license):
        resp = authed_client.post("/api/update-device", data={
            "ip": "10.0.0.1", "firmware_file": "test.bin",
        })
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "feature_locked"
        assert resp.json()["detail"]["feature"] == "update_single_device"

    def test_device_portal_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/device-portal/10.0.0.1")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "feature_locked"

    def test_device_history_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/device-history")
        assert resp.status_code == 403

    def test_config_compliance_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/config-compliance")
        assert resp.status_code == 403

    def test_sites_read_allowed_free(self, authed_client, free_license):
        """GET /api/sites should work on free tier (read-only access)."""
        resp = authed_client.get("/api/sites")
        assert resp.status_code == 200

    def test_sites_create_blocked_free(self, authed_client, free_license):
        resp = authed_client.post("/api/sites", data={"name": "Test Site"})
        assert resp.status_code == 403

    def test_slack_test_blocked_free(self, authed_client, free_license):
        resp = authed_client.post("/api/slack/test")
        assert resp.status_code == 403

    def test_backup_export_blocked_free(self, authed_client, free_license):
        resp = authed_client.post("/api/backup/export", json={"passphrase": "testpass123"})
        assert resp.status_code == 403

    def test_configs_list_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/configs")
        assert resp.status_code == 403

    def test_configs_history_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/configs/10.0.0.1")
        assert resp.status_code == 403

    def test_configs_latest_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/configs/10.0.0.1/latest")
        assert resp.status_code == 403

    def test_configs_snapshot_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/configs/10.0.0.1/snapshot/1")
        assert resp.status_code == 403

    def test_configs_download_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/configs/10.0.0.1/download/1")
        assert resp.status_code == 403

    def test_config_templates_list_blocked_free(self, authed_client, free_license):
        resp = authed_client.get("/api/config-templates")
        assert resp.status_code == 403


class TestNetworkConfigValidation:
    """Tests for network config input sanitization."""

    @pytest.fixture(autouse=True)
    def _appliance_mode(self, monkeypatch):
        monkeypatch.setenv("TACHYON_APPLIANCE", "1")

    def test_network_rejects_shell_injection_in_address(self, authed_client):
        resp = authed_client.post("/api/system/network", json={
            "mode": "static",
            "address": "10.0.0.1; rm -rf /",
            "gateway": "10.0.0.254",
        })
        assert resp.status_code == 400

    def test_network_rejects_backtick_injection(self, authed_client):
        resp = authed_client.post("/api/system/network", json={
            "mode": "static",
            "address": "`cat /etc/passwd`",
            "gateway": "10.0.0.254",
        })
        assert resp.status_code == 400

    def test_network_rejects_pipe_injection(self, authed_client):
        resp = authed_client.post("/api/system/network", json={
            "mode": "static",
            "address": "10.0.0.1|whoami",
            "gateway": "10.0.0.254",
        })
        assert resp.status_code == 400

    def test_network_rejects_invalid_ip_format(self, authed_client):
        resp = authed_client.post("/api/system/network", json={
            "mode": "static",
            "address": "not-an-ip",
            "gateway": "10.0.0.254",
        })
        assert resp.status_code == 400

    def test_network_rejects_invalid_mode(self, authed_client):
        resp = authed_client.post("/api/system/network", json={
            "mode": "malicious",
        })
        assert resp.status_code == 400


class TestLicenseAPI:
    """Tests for the license management API endpoints."""

    def test_get_license_status(self, authed_client, free_license):
        resp = authed_client.get("/api/license")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "free"
        assert data["status"] == "free"
        assert "features" in data
        assert data["features"]["update_single_device"] is False
        assert "billable_count" in data

    def test_get_license_status_pro(self, authed_client, pro_license):
        resp = authed_client.get("/api/license")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "pro"
        assert data["features"]["update_single_device"] is True

    def test_activate_empty_key_rejected(self, authed_client):
        resp = authed_client.post("/api/license/activate", json={"license_key": ""})
        assert resp.status_code == 400

    def test_deactivate_license(self, authed_client, pro_license):
        resp = authed_client.post("/api/license/deactivate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "free"

    def test_validate_without_key(self, authed_client, free_license):
        resp = authed_client.post("/api/license/validate")
        assert resp.status_code == 400
