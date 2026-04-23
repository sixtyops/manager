"""Tests for the live-dev feature validation matrix."""

from tests.integration.validation_matrix import (
    DEFERRED_EXTERNAL,
    DEV_BLOCKING,
    DEV_SSO,
    FEATURE_VALIDATION_MATRIX,
    UNIT_API_ONLY,
    VALIDATION_LANES,
)
from updater.features import Feature


class TestValidationMatrix:
    def test_every_feature_has_exactly_one_proof_lane(self):
        assert set(FEATURE_VALIDATION_MATRIX) == set(Feature)

    def test_all_lanes_are_known(self):
        assert set(FEATURE_VALIDATION_MATRIX.values()) <= VALIDATION_LANES

    def test_sso_uses_dedicated_lane(self):
        assert FEATURE_VALIDATION_MATRIX[Feature.SSO_OIDC] == DEV_SSO

    def test_outbound_integrations_are_deferred(self):
        assert FEATURE_VALIDATION_MATRIX[Feature.SLACK_NOTIFICATIONS] == DEFERRED_EXTERNAL
        assert FEATURE_VALIDATION_MATRIX[Feature.SNMP_TRAPS] == DEFERRED_EXTERNAL
        assert FEATURE_VALIDATION_MATRIX[Feature.WEBHOOKS] == DEFERRED_EXTERNAL

    def test_live_device_features_stay_in_blocking_lane(self):
        assert FEATURE_VALIDATION_MATRIX[Feature.UPDATE_SINGLE_DEVICE] == DEV_BLOCKING
        assert FEATURE_VALIDATION_MATRIX[Feature.RADIUS_AUTH] == DEV_BLOCKING
        assert FEATURE_VALIDATION_MATRIX[Feature.CONFIG_PUSH] == DEV_BLOCKING

    def test_release_channel_features_remain_unit_or_api_only(self):
        assert FEATURE_VALIDATION_MATRIX[Feature.BETA_FIRMWARE] == UNIT_API_ONLY
        assert FEATURE_VALIDATION_MATRIX[Feature.FIRMWARE_HOLD_CUSTOM] == UNIT_API_ONLY
