"""Unit tests for CPE sign-in detection (`NetworkPoller._check_cpe_auth`).

These pin the contract the UI relies on: a rejected login (different
credentials than the parent AP) is reported as "auth_failed", a network
failure as "unreachable", and a good login as "ok". The values are distinct
because the monitor surfaces them differently — an amber "Can't sign in"
badge vs. the offline dot.
"""

import time

import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call

from updater import radius_config
from updater.poller import NetworkPoller, _CLIENT_TTL_SECONDS


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


class TestCheckCpeAuthCaching:
    """The probe reuses an authenticated session across poll cycles instead of
    re-logging in every minute (which floods each device's audit log)."""

    @pytest.mark.asyncio
    async def test_successful_login_is_cached(self, mock_db):
        poller = NetworkPoller()
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning(True)):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "ok"
        assert "10.0.0.11" in poller._cpe_clients

    @pytest.mark.asyncio
    async def test_auth_failed_is_not_cached(self, mock_db):
        poller = NetworkPoller()
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning("Invalid credentials")), \
             patch("updater.poller.radius_config.is_device_auth_enabled",
                   return_value=False):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "auth_failed"
        assert "10.0.0.11" not in poller._cpe_clients

    @pytest.mark.asyncio
    async def test_valid_cached_session_skips_relogin(self, mock_db):
        # A live cached session is verified with a lightweight call; no fresh
        # login (no new client built, connect() never called).
        poller = NetworkPoller()
        cached = MagicMock()
        cached.session_valid = AsyncMock(return_value="ok")
        cached.connect = AsyncMock(return_value=True)
        poller._cpe_clients["10.0.0.11"] = (cached, time.time())
        with patch("updater.poller.get_driver") as get_driver_mock:
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "ok"
        cached.session_valid.assert_awaited_once()
        cached.connect.assert_not_awaited()
        get_driver_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_cached_session_reauthenticates(self, mock_db):
        poller = NetworkPoller()
        stale = MagicMock()
        stale.session_valid = AsyncMock(return_value="expired")
        poller._cpe_clients["10.0.0.11"] = (stale, time.time())
        with patch("updater.poller.get_driver",
                   return_value=_driver_returning(True)):
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "ok"
        # The stale client was dropped and replaced by the freshly-authed one.
        assert poller._cpe_clients["10.0.0.11"][0] is not stale

    @pytest.mark.asyncio
    async def test_unreachable_cached_session_kept_for_revalidation(self, mock_db):
        # A device that's momentarily offline should report "unreachable"
        # without burning a login, and keep its cached client so the next cycle
        # can revalidate once it's back.
        poller = NetworkPoller()
        cached = MagicMock()
        cached.session_valid = AsyncMock(return_value="unreachable")
        cached.connect = AsyncMock(return_value=True)
        poller._cpe_clients["10.0.0.11"] = (cached, time.time())
        with patch("updater.poller.get_driver") as get_driver_mock:
            status = await poller._check_cpe_auth("10.0.0.11", "root", "ap-pass")
        assert status == "unreachable"
        cached.connect.assert_not_awaited()
        get_driver_mock.assert_not_called()
        assert "10.0.0.11" in poller._cpe_clients

    def test_evict_stale_cpe_clients_drops_expired_by_ttl(self):
        poller = NetworkPoller()
        now = time.time()
        poller._cpe_clients["fresh"] = (MagicMock(), now)
        poller._cpe_clients["old"] = (MagicMock(), now - _CLIENT_TTL_SECONDS - 1)
        poller._evict_stale_cpe_clients(now)
        assert "fresh" in poller._cpe_clients
        assert "old" not in poller._cpe_clients
