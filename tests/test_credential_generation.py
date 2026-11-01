"""Discard authentication results that overlap a credential change."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from updater import database as db
from updater.poller import NetworkPoller, set_poller
from updater.scheduler import AutoUpdateScheduler


CPE = "192.0.2.20"
AP = "192.0.2.10"
SETTINGS = {"schedule_enabled": "false"}
NOW = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo("UTC"))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cached", "primary", "fallback"])
@pytest.mark.parametrize("result", [True, "Invalid credentials"])
async def test_auth_discards_result_after_invalidation(mock_db, path, result):
    poller = NetworkPoller()
    client = MagicMock()

    async def invalidate():
        poller.invalidate_cpe_clients()
        return ("ok" if result is True else "expired") if path == "cached" else result

    client.connect = AsyncMock(side_effect=invalidate)
    client.session_valid = AsyncMock(side_effect=invalidate)
    if path == "cached":
        poller._cache_cpe_client(CPE, client)
    rejected = MagicMock(connect=AsyncMock(return_value="Invalid credentials"))
    factory = MagicMock(side_effect=[rejected, client] if path == "fallback" else [client])
    defaults = MagicMock(username="default", password="test-default")
    with patch("updater.poller.get_driver", return_value=factory), \
         patch("updater.poller.radius_config.get_device_credentials", return_value=("ap", "test-ap")), \
         patch("updater.poller.radius_config.is_device_auth_enabled", return_value=True), \
         patch("updater.poller.radius_config.get_device_auth_config", return_value=defaults):
        assert await poller._check_cpe_auth(CPE, "ap", "test-ap") == "unreachable"
    assert CPE not in poller._cpe_clients
    assert factory.call_count == {"cached": 0, "primary": 1, "fallback": 2}[path]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_marker,validate_cached", [(False, False), (False, True), (True, True)])
async def test_period_discards_stale_success_from_auth_hook(mock_db, has_marker, validate_cached):
    poller = NetworkPoller()
    if has_marker:
        poller.record_cpe_fresh_login_check(CPE, SETTINGS, NOW)

    async def stale_success(*args, **kwargs):
        poller.invalidate_cpe_clients()
        return "ok"

    with patch.object(poller, "_check_cpe_auth", side_effect=stale_success) as check:
        status = await poller.check_cpe_auth_for_period(
            CPE, "ap", "test-ap", SETTINGS, NOW,
            force_fresh_login=True, validate_cached_session=validate_cached,
        )
    check.assert_awaited_once()
    assert status == "unreachable"
    assert not poller.has_successful_cpe_fresh_login(CPE, SETTINGS, NOW)


def test_stale_generation_cannot_store_client_or_success_marker(mock_db):
    poller = NetworkPoller()
    generation = poller._cpe_credentials_generation
    poller.invalidate_cpe_clients()
    assert poller._cache_cpe_client(CPE, MagicMock(), generation) is False
    poller.record_cpe_fresh_login_check(
        CPE, SETTINGS, NOW, credential_generation=generation,
    )
    assert CPE not in poller._cpe_clients
    assert not poller.has_successful_cpe_fresh_login(CPE, SETTINGS, NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_at,cached", [("login", False), ("read", False), ("read", True)])
async def test_bank_path_discards_stale_login_or_read(mock_db, invalidate_at, cached):
    poller = NetworkPoller()
    client = MagicMock()

    async def connect():
        if invalidate_at == "login":
            poller.invalidate_cpe_clients()
        return True

    async def read():
        poller.invalidate_cpe_clients()
        return MagicMock(bank1_version="1.0", bank2_version="2.0", active_bank=1)

    client.connect = AsyncMock(side_effect=connect)
    client.get_device_info = AsyncMock(side_effect=read)
    if cached:
        poller._cache_cpe_client(CPE, client)
    with patch("updater.poller.db.get_cpe_bank_last_fetched", return_value=None), \
         patch("updater.poller.db.update_cpe_bank_info") as store, \
         patch("updater.poller.get_driver", return_value=MagicMock(return_value=client)):
        await poller._maybe_fetch_cpe_banks(AP, CPE, "ap", "test-ap")
    store.assert_not_called()
    assert CPE not in poller._cpe_clients
    assert client.get_device_info.await_count == (0 if invalidate_at == "login" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_at", ["credentials", "login", "hook"])
async def test_scheduler_precheck_fails_closed_and_recovers(mock_db, invalidate_at):
    poller = NetworkPoller()
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
    db.upsert_access_point(AP, "ap", "test-old", enabled=True)
    db.upsert_cpe(AP, {"ip": CPE, "auth_status": "ok"})
    original_read = db.get_access_point

    def change_credentials():
        db.upsert_access_point(AP, "ap", "test-current", enabled=True)
        poller.invalidate_client(AP)

    def read_credentials(ip):
        ap = original_read(ip)
        if invalidate_at == "credentials":
            change_credentials()
        return ap

    async def stale_success(*args, **kwargs):
        change_credentials()
        return "ok" if invalidate_at == "hook" else True

    client = MagicMock(connect=AsyncMock(side_effect=stale_success))
    set_poller(poller)
    try:
        with patch("updater.scheduler.db.get_access_point", side_effect=read_credentials), \
             patch("updater.poller.get_driver", return_value=MagicMock(return_value=client)):
            if invalidate_at == "hook":
                with patch.object(poller, "_check_cpe_auth", side_effect=stale_success):
                    failures = await scheduler._precheck_cpe_credentials([AP], SETTINGS, NOW)
            else:
                failures = await scheduler._precheck_cpe_credentials([AP], SETTINGS, NOW)
        assert failures == ((AP, CPE, "unreachable"),)
        assert db.get_cpe_by_ip(CPE)["auth_status"] == "unreachable"
        assert CPE not in poller._cpe_clients
        assert not poller.has_successful_cpe_fresh_login(CPE, SETTINGS, NOW)

        client.connect = AsyncMock(return_value=True)
        factory = MagicMock(return_value=client)
        with patch("updater.poller.get_driver", return_value=factory):
            assert await scheduler._precheck_cpe_credentials([AP], SETTINGS, NOW) == ()
        factory.assert_called_once_with(CPE, "ap", "test-current", timeout=10)
        assert poller.has_successful_cpe_fresh_login(CPE, SETTINGS, NOW)
    finally:
        set_poller(None)
