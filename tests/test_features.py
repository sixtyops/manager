"""Tests for feature definitions and stability classification."""

import pytest

from updater.features import (
    Feature, DANGEROUS_FEATURES,
    is_feature_enabled, is_dangerous, get_feature_map,
    require_feature, require_pro,
    get_instance_id,
)


class TestFeatureEnabled:
    """All features should be enabled (no gating)."""

    def test_all_features_enabled(self):
        for feature in Feature:
            assert is_feature_enabled(feature), f"{feature.value} should be enabled"


class TestDangerousClassification:
    """Tests for dangerous feature classification."""

    EXPECTED_DANGEROUS = {
        Feature.CONFIG_BACKUP,
        Feature.CONFIG_TEMPLATES,
        Feature.CONFIG_COMPLIANCE,
        Feature.CONFIG_PUSH,
        Feature.RADIUS_AUTH,
        Feature.SSO_OIDC,
    }

    EXPECTED_STABLE = {
        Feature.UPDATE_SINGLE_DEVICE,
        Feature.SLACK_NOTIFICATIONS,
        Feature.DEVICE_PORTAL,
        Feature.DEVICE_HISTORY,
        Feature.TOWER_SITES,
        Feature.BETA_FIRMWARE,
        Feature.FIRMWARE_HOLD_CUSTOM,
        Feature.SNMP_TRAPS,
        Feature.WEBHOOKS,
    }

    def test_dangerous_features_correct(self):
        assert DANGEROUS_FEATURES == self.EXPECTED_DANGEROUS

    def test_stable_features_not_dangerous(self):
        for feature in self.EXPECTED_STABLE:
            assert not is_dangerous(feature), f"{feature.value} should be stable"

    def test_dangerous_features_flagged(self):
        for feature in self.EXPECTED_DANGEROUS:
            assert is_dangerous(feature), f"{feature.value} should be dangerous"

    def test_all_features_classified(self):
        """Every feature should be in either dangerous or stable set."""
        all_features = set(Feature)
        classified = self.EXPECTED_DANGEROUS | self.EXPECTED_STABLE
        assert all_features == classified, f"Unclassified features: {all_features - classified}"


class TestFeatureMap:
    """Tests for get_feature_map()."""

    def test_returns_all_features(self):
        fm = get_feature_map()
        for feature in Feature:
            assert feature.value in fm

    def test_all_enabled(self):
        fm = get_feature_map()
        for info in fm.values():
            assert info["enabled"] is True

    def test_dangerous_flag_matches(self):
        fm = get_feature_map()
        for feature in Feature:
            expected = feature in DANGEROUS_FEATURES
            assert fm[feature.value]["dangerous"] == expected

    def test_has_display_name(self):
        fm = get_feature_map()
        for info in fm.values():
            assert "name" in info and len(info["name"]) > 0


class TestNoOpDependencies:
    """Tests that require_feature and require_pro are no-ops."""

    @pytest.mark.asyncio
    async def test_require_pro_is_noop(self):
        await require_pro()  # Should not raise

    @pytest.mark.asyncio
    async def test_require_feature_is_noop(self):
        dep = require_feature(Feature.CONFIG_BACKUP)
        await dep()  # Should not raise

    @pytest.mark.asyncio
    async def test_require_feature_all_features(self):
        for feature in Feature:
            dep = require_feature(feature)
            await dep()  # None should raise


class TestInstanceId:
    """Tests for instance ID persistence."""

    def test_instance_id_returns_uuid(self, mock_db):
        iid = get_instance_id()
        assert len(iid) == 36  # UUID4 format

    def test_instance_id_stable(self, mock_db):
        iid1 = get_instance_id()
        iid2 = get_instance_id()
        assert iid1 == iid2


class TestAPIEndpoints:
    """Tests that formerly-gated endpoints are now accessible."""

    def test_license_status_returns_pro(self, authed_client):
        resp = authed_client.get("/api/license")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_pro"] is True
        assert data["tier"] == "pro"
        assert all(data["features"].values())

    def test_features_endpoint(self, authed_client):
        resp = authed_client.get("/api/features")
        assert resp.status_code == 200
        data = resp.json()
        assert "features" in data
        assert "feature_info" in data
        assert all(data["features"].values())

    def test_instance_id_endpoint(self, authed_client):
        resp = authed_client.get("/api/license/instance-id")
        assert resp.status_code == 200
        assert "instance_id" in resp.json()

    def test_update_device_allowed(self, authed_client):
        """Manual update endpoint should not return 403."""
        resp = authed_client.post("/api/update-device", data={
            "ip": "10.0.0.1", "firmware_file": "test.bin",
        })
        # May return 400/404 (no device), but NOT 403
        assert resp.status_code != 403

    def test_device_portal_allowed(self, authed_client):
        resp = authed_client.get("/api/device-portal/10.0.0.1")
        assert resp.status_code != 403

    def test_device_history_allowed(self, authed_client):
        resp = authed_client.get("/api/device-history")
        assert resp.status_code != 403

    def test_sites_create_allowed(self, authed_client):
        resp = authed_client.post("/api/sites", data={"name": "Test Site"})
        assert resp.status_code != 403

    def test_slack_test_allowed(self, authed_client):
        resp = authed_client.post("/api/slack/test")
        assert resp.status_code != 403

    def test_config_templates_allowed(self, authed_client):
        resp = authed_client.get("/api/config-templates")
        assert resp.status_code != 403


class TestNetworkConfigValidation:
    """Tests for network config input sanitization (kept from original)."""

    @pytest.fixture(autouse=True)
    def _appliance_mode(self, monkeypatch):
        monkeypatch.setenv("SIXTYOPS_APPLIANCE", "1")

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
