"""Tests for syslog forwarding module."""

import logging
from unittest.mock import patch, MagicMock

import pytest


class TestSyslogForwarder:
    """Tests for the syslog forwarder module."""

    def test_get_config_defaults(self, mock_db):
        from updater import syslog_forwarder as sf
        config = sf._get_config()
        assert config["enabled"] is False
        assert config["host"] == ""
        assert config["port"] == 514
        assert config["protocol"] == "udp"
        assert config["facility"] == "local0"

    def test_get_config_from_settings(self, mock_db):
        from updater import database as db, syslog_forwarder as sf
        db.set_settings({
            "syslog_forward_enabled": "true",
            "syslog_forward_host": "10.0.0.1",
            "syslog_forward_port": "1514",
            "syslog_forward_protocol": "tcp",
            "syslog_forward_facility": "local3",
        })
        config = sf._get_config()
        assert config["enabled"] is True
        assert config["host"] == "10.0.0.1"
        assert config["port"] == 1514
        assert config["protocol"] == "tcp"
        assert config["facility"] == "local3"

    def test_get_status_disabled(self, mock_db):
        from updater import syslog_forwarder as sf
        # Reset state
        sf._syslog_handler = None
        sf._syslog_logger = None
        sf._current_config = {}

        status = sf.get_status()
        assert status["enabled"] is False
        assert status["connected"] is False

    def test_send_event_noop_when_disabled(self, mock_db):
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None
        # Should not raise
        sf.send_event("test", "Test message", "info")

    @patch("updater.syslog_forwarder.logging.handlers.SysLogHandler")
    def test_setup_handler_udp(self, mock_handler_cls, mock_db):
        import socket
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler

        config = {
            "enabled": True,
            "host": "10.0.0.1",
            "port": 514,
            "protocol": "udp",
            "facility": "local0",
        }
        result = sf._setup_handler(config)
        assert result is mock_handler
        mock_handler_cls.assert_called_once()
        call_kwargs = mock_handler_cls.call_args
        assert call_kwargs[1]["address"] == ("10.0.0.1", 514)
        assert call_kwargs[1]["socktype"] == socket.SOCK_DGRAM

    @patch("updater.syslog_forwarder.logging.handlers.SysLogHandler")
    def test_setup_handler_tcp(self, mock_handler_cls, mock_db):
        import socket
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler

        config = {
            "enabled": True,
            "host": "10.0.0.1",
            "port": 1514,
            "protocol": "tcp",
            "facility": "local2",
        }
        result = sf._setup_handler(config)
        assert result is mock_handler
        call_kwargs = mock_handler_cls.call_args
        assert call_kwargs[1]["socktype"] == socket.SOCK_STREAM

    @patch("updater.syslog_forwarder.logging.handlers.SysLogHandler")
    def test_send_event_with_handler(self, mock_handler_cls, mock_db):
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler

        sf._setup_handler({
            "enabled": True, "host": "10.0.0.1", "port": 514,
            "protocol": "udp", "facility": "local0",
        })

        assert sf._syslog_handler is mock_handler
        assert sf._syslog_logger is not None
        # send_event should not raise
        sf.send_event("job", "Job abc completed", "info")

    @patch("updater.syslog_forwarder.logging.handlers.SysLogHandler")
    def test_reload_config_changes(self, mock_handler_cls, mock_db):
        from updater import database as db, syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None
        sf._current_config = {}

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler

        db.set_settings({
            "syslog_forward_enabled": "true",
            "syslog_forward_host": "10.0.0.1",
        })
        sf.reload_config()
        assert sf._syslog_handler is mock_handler

    def test_test_connection_disabled(self, mock_db):
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None
        sf._current_config = {}

        success, msg = sf.test_connection()
        assert success is False
        assert "not enabled" in msg

    def test_setup_handler_disabled(self, mock_db):
        from updater import syslog_forwarder as sf
        sf._syslog_handler = None
        sf._syslog_logger = None

        config = {"enabled": False, "host": "", "port": 514,
                  "protocol": "udp", "facility": "local0"}
        result = sf._setup_handler(config)
        assert result is None

    def test_facilities_mapping(self):
        from updater import syslog_forwarder as sf
        assert "local0" in sf.FACILITIES
        assert "local7" in sf.FACILITIES
        assert len(sf.FACILITIES) == 8

    def test_send_event_severity_levels(self, mock_db):
        from updater import syslog_forwarder as sf
        # With no handler, all severities should be no-ops
        sf._syslog_handler = None
        sf._syslog_logger = None
        for sev in ("info", "warning", "error", "critical"):
            sf.send_event("test", f"Test {sev}", sev)

    def test_syslog_status_api(self, authed_client):
        resp = authed_client.get("/api/syslog/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "connected" in data

    def test_syslog_test_api(self, authed_client):
        resp = authed_client.post("/api/syslog/test")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data
        assert data["success"] is False  # Not enabled by default
