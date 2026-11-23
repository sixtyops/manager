"""Tests for the live-dev feature validation matrix."""

from tests.integration.validation_matrix import (
    DEFERRED_EXTERNAL,
    DEV_BLOCKING,
    DEV_SSO,
    FEATURE_VALIDATION_MATRIX,
    UNIT_API_ONLY,
    VALIDATION_LANES,
)


class TestValidationMatrix:
    def test_every_feature_has_exactly_one_proof_lane(self):
        assert set(FEATURE_VALIDATION_MATRIX) == {
            "update_single_device", "sso_oidc", "radius_auth", "config_backup",
            "config_templates", "config_compliance", "config_push",
            "slack_notifications", "device_portal", "device_history", "tower_sites",
            "beta_firmware", "firmware_hold_custom", "snmp_traps", "webhooks",
        }

    def test_all_lanes_are_known(self):
        assert set(FEATURE_VALIDATION_MATRIX.values()) <= VALIDATION_LANES

    def test_sso_uses_dedicated_lane(self):
        assert FEATURE_VALIDATION_MATRIX["sso_oidc"] == DEV_SSO

    def test_outbound_integrations_are_deferred(self):
        assert FEATURE_VALIDATION_MATRIX["slack_notifications"] == DEFERRED_EXTERNAL
        assert FEATURE_VALIDATION_MATRIX["snmp_traps"] == DEFERRED_EXTERNAL
        assert FEATURE_VALIDATION_MATRIX["webhooks"] == DEFERRED_EXTERNAL

    def test_live_device_features_stay_in_blocking_lane(self):
        assert FEATURE_VALIDATION_MATRIX["update_single_device"] == DEV_BLOCKING
        assert FEATURE_VALIDATION_MATRIX["radius_auth"] == DEV_BLOCKING
        assert FEATURE_VALIDATION_MATRIX["config_push"] == DEV_BLOCKING

    def test_release_channel_features_remain_unit_or_api_only(self):
        assert FEATURE_VALIDATION_MATRIX["beta_firmware"] == UNIT_API_ONLY
        assert FEATURE_VALIDATION_MATRIX["firmware_hold_custom"] == UNIT_API_ONLY
