"""Unit tests for CPE sign-in detection (`NetworkPoller._check_cpe_auth`).

These pin the contract the UI relies on: a rejected login (different
credentials than the parent AP) is reported as "auth_failed", a network
failure as "unreachable", and a good login as "ok". The values are distinct
because the monitor surfaces them differently — an amber "Can't sign in"
badge vs. the offline dot.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call

from updater import radius_config
from updater.poller import NetworkPoller


def _driver_returning(connect_result):
    """A get_driver() stand-in whose client.connect() yields connect_result."""
    client = MagicMock()
    client.connect = AsyncMock(return_value=connect_result)
    factory = MagicMock(return_value=client)  # called as Driver(ip, user, pw, timeout=...)
    return factory


class TestCheckCpeAuth:
    @pytest.mark.asyncio
    async def test_invalid_credentials_maps_to_auth_failed(self, mock_db):
        # TachyonClient.login() returns "Invalid credentials" on HTTP 401.
        poller = NetworkPoller()
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning("Invalid credentials")), \
             patch("updater.poller.radius_config.is_device_auth_enabled",
                   return_value=False):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "auth_failed"

    @pytest.mark.asyncio
    async def test_network_error_maps_to_unreachable(self, mock_db):
        # "Device not reachable" comes from a curl/network-layer failure.
        poller = NetworkPoller()
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning("Device not reachable")):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "unreachable"

    @pytest.mark.asyncio
    async def test_successful_login_maps_to_ok(self, mock_db):
        poller = NetworkPoller()
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning(True)):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "ok"

    @pytest.mark.asyncio
    async def test_fallback_network_failure_maps_to_unreachable(self, mock_db):
        # AP creds are rejected, then the global-default fallback attempt hits a
        # network failure. That must report "unreachable", not "auth_failed".
        poller = NetworkPoller()
        client = MagicMock()
        client.connect = AsyncMock(side_effect=["Invalid credentials",
                                                "Device not reachable"])
        factory = MagicMock(return_value=client)  # same client reused per attempt
        cfg = radius_config.DeviceAuthConfig(enabled=True, username="other",
                                             password="default-pass")
        with patch("updater.poller.get_driver", return_value=factory), \
             patch("updater.poller.radius_config.is_device_auth_enabled",
                   return_value=True), \
             patch("updater.poller.radius_config.get_device_auth_config",
                   return_value=cfg):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "unreachable"
        assert client.connect.await_count == 2
        # Second attempt must use the global-default credentials.
        assert factory.call_args_list[1] == call("10.0.0.11", "other",
                                                 "default-pass", timeout=10)

    @pytest.mark.asyncio
    async def test_fallback_retries_when_only_password_differs(self, mock_db):
        # AP uses root/ap-pass; the client shares the username but uses a
        # different password that matches the global default. The retry must
        # still fire (full-tuple compare), so the client reports "ok".
        poller = NetworkPoller()
        client = MagicMock()
        client.connect = AsyncMock(side_effect=["Invalid credentials", True])
        factory = MagicMock(return_value=client)
        cfg = radius_config.DeviceAuthConfig(enabled=True, username="root",
                                             password="cpe-pass")
        with patch("updater.poller.get_driver", return_value=factory), \
             patch("updater.poller.radius_config.is_device_auth_enabled",
                   return_value=True), \
             patch("updater.poller.radius_config.get_device_auth_config",
                   return_value=cfg):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "ok"
        assert client.connect.await_count == 2
        # Second attempt must reuse the username but swap in the default password.
        assert factory.call_args_list[1] == call("10.0.0.11", "root",
                                                 "cpe-pass", timeout=10)
