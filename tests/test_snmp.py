"""Tests for SNMP trap notifications."""

import asyncio
from importlib.metadata import PackageNotFoundError, version
import socket
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from packaging.version import Version
from pyasn1.codec.ber import decoder
from pysnmp.hlapi.v3arch import asyncio as snmp_api
from pysnmp.proto import api as protocol_api

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
        with patch.dict("sys.modules", {"pysnmp": None, "pysnmp.hlapi": None, "pysnmp.hlapi.v3arch": None, "pysnmp.hlapi.v3arch.asyncio": None}):
            # Force reimport failure
            with patch("builtins.__import__", side_effect=ImportError("No module")):
                result = await snmp.send_snmp_trap("1.3.6.1.4.1.99999.1.99", [], config=config)
                assert result is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("indication,status,success", [
        (None, 0, True), ("synthetic failure", 0, False), (None, 1, False),
    ])
    async def test_real_adapter_awaits_library_and_closes_engine(self, indication, status, success):
        config = {"host": "127.0.0.1", "port": 1162, "community": "synthetic", "version": "2c"}
        engine = MagicMock(spec=snmp_api.SnmpEngine)
        transport = MagicMock()
        with patch.object(snmp_api, "SnmpEngine", return_value=engine), \
             patch.object(snmp_api.UdpTransportTarget, "create", new_callable=AsyncMock, return_value=transport) as create, \
             patch.object(snmp_api, "send_notification", new_callable=AsyncMock, return_value=(indication, status, 0, [])) as send:
            assert await snmp.send_snmp_trap(snmp.OID_TRAP_TEST, [(snmp.OID_MESSAGE, "s", "test")], config=config) is success
        create.assert_awaited_once_with(("127.0.0.1", 1162))
        send.assert_awaited_once()
        args = send.call_args.args
        assert args[0] is engine and args[2] is transport
        assert isinstance(args[1], snmp_api.CommunityData) and args[1].message_processing_model == 1
        assert isinstance(args[3], snmp_api.ContextData)
        assert args[4] == "trap"
        assert isinstance(args[5], snmp_api.NotificationType)
        assert isinstance(args[6], snmp_api.ObjectType)
        engine.close_dispatcher.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_transport_exception_does_not_send(self):
        config = {"host": "127.0.0.1", "port": 1162, "community": "synthetic", "version": "2c"}
        with patch.object(snmp_api.UdpTransportTarget, "create", new_callable=AsyncMock, side_effect=OSError("synthetic")), \
             patch.object(snmp_api, "send_notification", new_callable=AsyncMock) as send:
            assert await snmp.send_snmp_trap(snmp.OID_TRAP_TEST, [], config=config) is False
        send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("synthetic"), asyncio.CancelledError()])
    async def test_send_exception_or_cancellation_closes_engine(self, error):
        config = {"host": "127.0.0.1", "port": 1162, "community": "synthetic", "version": "2c"}
        engine = MagicMock(spec=snmp_api.SnmpEngine)
        with patch.object(snmp_api, "SnmpEngine", return_value=engine), \
             patch.object(snmp_api.UdpTransportTarget, "create", new_callable=AsyncMock), \
             patch.object(snmp_api, "send_notification", new_callable=AsyncMock, side_effect=error):
            if isinstance(error, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await snmp.send_snmp_trap(snmp.OID_TRAP_TEST, [], config=config)
            else:
                assert await snmp.send_snmp_trap(snmp.OID_TRAP_TEST, [], config=config) is False
        engine.close_dispatcher.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_real_v2c_packet_on_synthetic_loopback(self):
        """Send only to an ephemeral loopback socket and decode the actual packet."""
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.setblocking(False)
            config = {"host": "127.0.0.1", "port": receiver.getsockname()[1],
                      "community": "synthetic", "version": "2c"}
            engine = snmp_api.SnmpEngine()
            with patch.object(snmp_api, "SnmpEngine", return_value=engine):
                assert await snmp.send_snmp_trap(snmp.OID_TRAP_TEST, [
                    (snmp.OID_MESSAGE, "s", "synthetic notification"),
                    (snmp.OID_SUCCESS_COUNT, "i", "3"),
                ], config=config) is True
            packet, sender = await asyncio.wait_for(loop.sock_recvfrom(receiver, 65535), timeout=3)
        assert sender[0] == "127.0.0.1"
        assert engine.transport_dispatcher is None
        v2c = protocol_api.PROTOCOL_MODULES[protocol_api.SNMP_VERSION_2C]
        message, remainder = decoder.decode(packet, asn1Spec=v2c.Message())
        assert not remainder
        assert int(message["version"]) == 1
        assert bytes(v2c.apiMessage.get_community(message)) == b"synthetic"
        pdu = v2c.apiMessage.get_pdu(message)
        assert pdu.isSameTypeWith(v2c.SNMPv2TrapPDU())
        pairs = [(str(oid), value) for oid, value in v2c.apiPDU.get_varbinds(pdu)]
        assert [oid for oid, _ in pairs] == [
            "1.3.6.1.2.1.1.3.0", "1.3.6.1.6.3.1.1.4.1.0",
            snmp.OID_MESSAGE, snmp.OID_SUCCESS_COUNT,
        ]
        assert str(pairs[1][1]) == snmp.OID_TRAP_TEST
        assert isinstance(pairs[2][1], v2c.OctetString)
        assert bytes(pairs[2][1]) == b"synthetic notification"
        assert isinstance(pairs[3][1], v2c.Integer32) and int(pairs[3][1]) == 3


def test_maintained_dependency_and_patched_decoder_are_installed():
    assert Version(version("pysnmp")) == Version("7.1.30")
    assert Version(version("pyasn1")) >= Version("0.6.4")
    with pytest.raises(PackageNotFoundError):
        version("pysnmp-lextudio")
    assert snmp.is_pysnmp_available()


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
        with patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, side_effect=[False, False, True]) as send, \
             patch("updater.snmp.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await snmp._send_with_retry("1.3.6.1.4.1.99999.1.99", [], {"host": "h", "port": 162, "community": "c", "version": "2c"})
        assert send.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [1, 2]

    @pytest.mark.asyncio
    async def test_stops_after_max_retries(self):
        with patch("updater.snmp.send_snmp_trap", new_callable=AsyncMock, return_value=False) as mock_send, \
             patch("updater.snmp.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await snmp._send_with_retry("1.3.6.1.4.1.99999.1.99", [], {"host": "h", "port": 162, "community": "c", "version": "2c"}, max_retries=1)
            assert mock_send.call_count == 2  # Initial + 1 retry
            sleep.assert_awaited_once_with(1)


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
