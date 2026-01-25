"""Tests for the telemetry module."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from updater.telemetry import (
    build_telemetry_payload,
    send_telemetry,
    _aggregate_error_types,
    _aggregate_model_stats,
    _aggregate_role_stats,
)


# Sample device results for testing
SAMPLE_DEVICES = {
    "10.0.0.1": {"status": "success", "model": "T5c", "role": "ap", "error": None},
    "10.0.0.2": {"status": "success", "model": "T5c", "role": "ap", "error": None},
    "10.0.0.3": {"status": "failed", "model": "T5c+", "role": "cpe", "error": "Connection timed out"},
    "10.0.0.4": {"status": "success", "model": "T5c+", "role": "cpe", "error": None},
    "10.0.0.5": {"status": "failed", "model": "T5c", "role": "ap", "error": "Upload failed"},
}


class TestBuildTelemetryPayload:
    def test_payload_structure(self):
        payload = build_telemetry_payload(
            job_id="test-123",
            success_count=3,
            failed_count=2,
            skipped_count=0,
            cancelled_count=0,
            duration_seconds=120.5,
            bank_mode="both",
            is_scheduled=False,
            devices=SAMPLE_DEVICES,
        )
        assert payload["event"] == "job_completed"
        assert "timestamp" in payload
        assert "install_id" in payload
        assert "job" in payload
        assert "models" in payload
        assert "errors" in payload
        assert "by_role" in payload

    def test_job_counts(self):
        payload = build_telemetry_payload(
            job_id="test-123",
            success_count=3,
            failed_count=2,
            skipped_count=1,
            cancelled_count=0,
            duration_seconds=60.0,
            bank_mode="both",
            is_scheduled=True,
            devices=SAMPLE_DEVICES,
        )
        job = payload["job"]
        assert job["total_devices"] == 6
        assert job["success_count"] == 3
        assert job["failed_count"] == 2
        assert job["skipped_count"] == 1
        assert job["success_rate"] == 0.5
        assert job["bank_mode"] == "both"
        assert job["is_scheduled"] is True

    def test_no_identifiable_info(self):
        payload = build_telemetry_payload(
            job_id="test-123",
            success_count=3,
            failed_count=2,
            skipped_count=0,
            cancelled_count=0,
            duration_seconds=60.0,
            bank_mode="both",
            is_scheduled=False,
            devices=SAMPLE_DEVICES,
        )
        payload_str = str(payload)
        assert "10.0.0" not in payload_str
        assert "test-123" not in payload_str


class TestAggregateErrorTypes:
    def test_categorizes_timeout(self):
        devices = {"1": {"error": "Connection timed out after 300s"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"timeout": 1}

    def test_categorizes_connection(self):
        devices = {"1": {"error": "Cannot connect to device"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"connection_error": 1}

    def test_categorizes_auth(self):
        devices = {"1": {"error": "Login failed: bad credentials"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"authentication_error": 1}

    def test_categorizes_upload(self):
        devices = {"1": {"error": "Upload failed"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"upload_error": 1}

    def test_categorizes_reboot(self):
        devices = {"1": {"error": "Device did not reboot"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"reboot_error": 1}

    def test_categorizes_verification(self):
        devices = {"1": {"error": "Version mismatch after verify"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"verification_error": 1}

    def test_categorizes_other(self):
        devices = {"1": {"error": "Something unexpected happened"}}
        errors = _aggregate_error_types(devices)
        assert errors == {"other_error": 1}

    def test_skips_devices_without_errors(self):
        devices = {"1": {"error": None}, "2": {}}
        errors = _aggregate_error_types(devices)
        assert errors == {}

    def test_multiple_errors(self):
        devices = {
            "1": {"error": "Connection timed out"},
            "2": {"error": "Upload failed"},
            "3": {"error": "Connection timed out"},
        }
        errors = _aggregate_error_types(devices)
        assert errors == {"timeout": 2, "upload_error": 1}


class TestAggregateModelStats:
    def test_counts_models(self):
        stats = _aggregate_model_stats(SAMPLE_DEVICES)
        assert stats["model_distribution"]["T5c"] == 3
        assert stats["model_distribution"]["T5c+"] == 2

    def test_tracks_unknown_models(self):
        devices = {"1": {"model": None}, "2": {"model": "T5c"}}
        stats = _aggregate_model_stats(devices)
        assert stats["unknown_model_count"] == 1
        assert stats["model_distribution"]["unknown"] == 1


class TestAggregateRoleStats:
    def test_counts_by_role(self):
        stats = _aggregate_role_stats(SAMPLE_DEVICES)
        assert stats["ap"]["success"] == 2
        assert stats["ap"]["failed"] == 1
        assert stats["cpe"]["success"] == 1
        assert stats["cpe"]["failed"] == 1


class TestSendTelemetry:
    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch("updater.telemetry.TELEMETRY_ENABLED", False):
            result = await send_telemetry(
                job_id="test", success_count=1, failed_count=0,
                skipped_count=0, cancelled_count=0, duration_seconds=10.0,
                bank_mode="both", is_scheduled=False, devices={},
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_no_endpoint_returns_false(self):
        with patch("updater.telemetry.TELEMETRY_ENDPOINT", None):
            result = await send_telemetry(
                job_id="test", success_count=1, failed_count=0,
                skipped_count=0, cancelled_count=0, duration_seconds=10.0,
                bank_mode="both", is_scheduled=False, devices={},
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_successful_send(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("updater.telemetry.TELEMETRY_ENDPOINT", "https://example.com/telemetry"), \
             patch("aiohttp.ClientSession", return_value=mock_session):
            result = await send_telemetry(
                job_id="test", success_count=1, failed_count=0,
                skipped_count=0, cancelled_count=0, duration_seconds=10.0,
                bank_mode="both", is_scheduled=False,
                devices={"1": {"status": "success", "model": "T5c", "role": "ap", "error": None}},
            )
            assert result is True
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        with patch("updater.telemetry.TELEMETRY_ENDPOINT", "https://example.com/telemetry"), \
             patch("aiohttp.ClientSession", side_effect=Exception("Network error")):
            result = await send_telemetry(
                job_id="test", success_count=1, failed_count=0,
                skipped_count=0, cancelled_count=0, duration_seconds=10.0,
                bank_mode="both", is_scheduled=False, devices={},
            )
            assert result is False
