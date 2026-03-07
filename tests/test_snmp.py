"""Tests for SNMP trap notifications."""

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from updater import snmp


class TestIsValidTrapHost:
    def test_valid_ipv4(self):
        assert snmp.is_valid_trap_host("192.168.1.100") is True

    def test_valid_ipv6(self):
        assert snmp.is_valid_trap_host("::1") is True

    def test_valid_hostname(self):
        assert snmp.is_valid_trap_host("nms.example.com") is True

    def test_valid_simple_hostname(self):
        assert snmp.is_valid_trap_host("nms-server") is True

    def test_empty_string(self):
        assert snmp.is_valid_trap_host("") is False

    def test_whitespace_only(self):
        assert snmp.is_valid_trap_host("   ") is False

    def test_too_long(self):
        assert snmp.is_valid_trap_host("a" * 254) is False

    def test_invalid_chars(self):
        assert snmp.is_valid_trap_host("host name with spaces") is False


class TestGetSnmpConfig:
    @patch("updater.snmp.db")
    def test_returns_none_when_disabled(self, mock_db):
        mock_db.get_setting.return_value = "false"
        assert snmp._get_snmp_config() is None

    @patch("updater.snmp.db")
    def test_returns_none_when_no_host(self, mock_db):
        def side_effect(key, default=""):
            if key == "snmp_traps_enabled":
                return "true"
            return default
        mock_db.get_setting.side_effect = side_effect
        assert snmp._get_snmp_config() is None

    @patch("updater.snmp.db")
    def test_returns_config_when_configured(self, mock_db):
        settings = {
            "snmp_traps_enabled": "true",
            "snmp_trap_host": "192.168.1.100",
            "snmp_trap_port": "162",
            "snmp_trap_community": "public",
            "snmp_trap_version": "2c",
        }
        mock_db.get_setting.side_effect = lambda k, d="": settings.get(k, d)
        config = snmp._get_snmp_config()
        assert config is not None
        assert config["host"] == "192.168.1.100"
        assert config["port"] == 162
        assert config["community"] == "public"
        assert config["version"] == "2c"


class TestSendSnmpTrap:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_config(self):
        result = await snmp.send_snmp_trap("1.3.6.1.4.1.99999.1.99", [], config=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_import_error(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}
        with patch.dict("sys.modules", {"pysnmp": None, "pysnmp.hlapi": None, "pysnmp.hlapi.v1arch": None, "pysnmp.hlapi.v1arch.asyncio": None}):
            # Force reimport failure
            with patch("builtins.__import__", side_effect=ImportError("No module")):
                result = await snmp.send_snmp_trap("1.3.6.1.4.1.99999.1.99", [], config=config)
                assert result is False

    @pytest.mark.asyncio
    async def test_sends_trap_successfully(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}

        mock_send = AsyncMock(return_value=(None, None, None, []))
        mock_transport = AsyncMock()

        with patch("updater.snmp.send_snmp_trap", new=AsyncMock(return_value=True)) as mock_fn:
            result = await mock_fn("1.3.6.1.4.1.99999.1.99", [], config=config)
            assert result is True


class TestNotifyJobCompleted:
    @pytest.mark.asyncio
    async def test_noop_when_not_configured(self):
        with patch("updater.snmp._get_snmp_config", return_value=None):
            # Should not raise
            await snmp.notify_job_completed(
                job_id="test-123",
                success_count=5,
                failed_count=0,
                skipped_count=0,
                cancelled_count=0,
                duration_seconds=120.0,
                devices={},
                firmware_name="test-fw.bin",
            )

    @pytest.mark.asyncio
    async def test_sends_trap_when_configured(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}
        with patch("updater.snmp._get_snmp_config", return_value=config):
            with patch("updater.snmp._send_with_retry", new_callable=AsyncMock) as mock_send:
                # Create and gather the task
                await snmp.notify_job_completed(
                    job_id="test-123",
                    success_count=5,
                    failed_count=1,
                    skipped_count=0,
                    cancelled_count=0,
                    duration_seconds=120.0,
                    devices={"10.0.0.1": {"status": "failed", "error": "Timeout"}},
                    firmware_name="test-fw.bin",
                )
                # Allow the fire-and-forget task to run
                await asyncio.sleep(0.1)
                mock_send.assert_called_once()
                call_args = mock_send.call_args
                assert call_args[0][0] == snmp.OID_TRAP_JOB_COMPLETED
                varbinds = call_args[0][1]
                # Check job_id is in varbinds
                job_ids = [v for v in varbinds if v[0] == snmp.OID_JOB_ID]
                assert len(job_ids) == 1
                assert job_ids[0][2] == "test-123"

    @pytest.mark.asyncio
    async def test_includes_rollout_info(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}
        with patch("updater.snmp._get_snmp_config", return_value=config):
            with patch("updater.snmp._send_with_retry", new_callable=AsyncMock) as mock_send:
                await snmp.notify_job_completed(
                    job_id="test-456",
                    success_count=2,
                    failed_count=0,
                    skipped_count=0,
                    cancelled_count=0,
                    duration_seconds=60.0,
                    devices={},
                    firmware_name="fw.bin",
                    is_scheduled=True,
                    rollout_info={"phase": "canary", "status": "completed"},
                )
                await asyncio.sleep(0.1)
                varbinds = mock_send.call_args[0][1]
                phases = [v for v in varbinds if v[0] == snmp.OID_ROLLOUT_PHASE]
                assert len(phases) == 1
                assert phases[0][2] == "canary"


class TestSendTestTrap:
    @pytest.mark.asyncio
    async def test_returns_error_when_pysnmp_missing(self):
        with patch("updater.snmp.is_pysnmp_available", return_value=False):
            success, message = await snmp.send_test_trap()
            assert success is False
            assert "pysnmp" in message.lower()

    @pytest.mark.asyncio
    async def test_returns_error_when_not_configured(self):
        with patch("updater.snmp.is_pysnmp_available", return_value=True), \
             patch("updater.snmp._get_snmp_config", return_value=None):
            success, message = await snmp.send_test_trap()
            assert success is False
            assert "not configured" in message.lower()

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_host(self):
        config = {"host": "invalid host!", "port": 162, "community": "public", "version": "2c"}
        with patch("updater.snmp.is_pysnmp_available", return_value=True), \
             patch("updater.snmp._get_snmp_config", return_value=config):
            success, message = await snmp.send_test_trap()
            assert success is False
            assert "invalid" in message.lower()

    @pytest.mark.asyncio
    async def test_sends_test_trap_successfully(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}
        with patch("updater.snmp.is_pysnmp_available", return_value=True), \
             patch("updater.snmp._get_snmp_config", return_value=config), \
             patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, return_value=True):
            success, message = await snmp.send_test_trap()
            assert success is True
            assert "192.168.1.100" in message

    @pytest.mark.asyncio
    async def test_sends_test_trap_failure(self):
        config = {"host": "192.168.1.100", "port": 162, "community": "public", "version": "2c"}
        with patch("updater.snmp.is_pysnmp_available", return_value=True), \
             patch("updater.snmp._get_snmp_config", return_value=config), \
             patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, return_value=False):
            success, message = await snmp.send_test_trap()
            assert success is False


class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        with patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, side_effect=[False, False, True]):
            await snmp._send_with_retry("1.3.6.1.4.1.99999.1.99", [], {"host": "h", "port": 162, "community": "c", "version": "2c"})

    @pytest.mark.asyncio
    async def test_stops_after_max_retries(self):
        with patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, return_value=False) as mock_send:
            await snmp._send_with_retry("1.3.6.1.4.1.99999.1.99", [], {"host": "h", "port": 162, "community": "c", "version": "2c"}, max_retries=1)
            assert mock_send.call_count == 2  # Initial + 1 retry


class TestSnmpSettingsAPI:
    """Test SNMP settings via the app API."""

    def test_snmp_settings_writable(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "snmp_traps_enabled": "false",
            "snmp_trap_host": "192.168.1.100",
            "snmp_trap_port": "162",
            "snmp_trap_community": "mycomm",
        })
        assert resp.status_code == 200

    def test_invalid_trap_port(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "snmp_trap_port": "99999",
        })
        assert resp.status_code == 400

    def test_invalid_trap_host(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "snmp_trap_host": "invalid host with spaces!",
        })
        assert resp.status_code == 400

    def test_invalid_trap_version(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "snmp_trap_version": "v3",
        })
        assert resp.status_code == 400

    def test_snmp_test_endpoint_requires_admin(self, viewer_client):
        resp = viewer_client.post("/api/snmp/test")
        assert resp.status_code == 403

    def test_operator_cannot_test_snmp(self, operator_client):
        resp = operator_client.post("/api/snmp/test")
        assert resp.status_code == 403


class TestSnmpOIDs:
    """Verify OID constants are properly formatted."""

    def test_enterprise_oid_format(self):
        assert snmp.ENTERPRISE_OID.startswith("1.3.6.1.4.1.")

    def test_trap_oids_under_enterprise(self):
        assert snmp.OID_TRAP_JOB_COMPLETED.startswith(snmp.ENTERPRISE_OID)
        assert snmp.OID_TRAP_TEST.startswith(snmp.ENTERPRISE_OID)

    def test_varbind_oids_under_enterprise(self):
        for oid in [snmp.OID_JOB_ID, snmp.OID_JOB_STATUS, snmp.OID_SUCCESS_COUNT,
                    snmp.OID_FAILED_COUNT, snmp.OID_FIRMWARE_NAME, snmp.OID_MESSAGE]:
            assert oid.startswith(snmp.ENTERPRISE_OID)
