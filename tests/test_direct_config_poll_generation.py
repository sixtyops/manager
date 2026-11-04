"""Reject config poll results that overlap a credential change."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater.poller import NetworkPoller


AP = "192.0.2.10"
SWITCH = "192.0.2.11"
CPE = "192.0.2.20"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap", "switch", "cpe"])
async def test_config_poll_rejects_change_during_credential_read(mock_db, role):
    poller = NetworkPoller()
    device_ip = {"ap": AP, "switch": SWITCH, "cpe": CPE}[role]
    row = {"ip": device_ip, "username": "device", "password": "test-old"}

    def get_ap(ip):
        if role == "ap" and ip == AP:
            poller.invalidate_client(AP)
            return row
        if role == "cpe" and ip == CPE:
            return None
        if role == "cpe" and ip == AP:
            poller.invalidate_client(AP)
            return {"ip": AP, "username": "parent", "password": "test-old"}
        return None

    def get_switch(ip):
        if role == "switch" and ip == SWITCH:
            poller.invalidate_client(SWITCH)
            return row
        return None

    def get_cpe(ip):
        return {"ip": CPE, "ap_ip": AP} if role == "cpe" and ip == CPE else None

    with patch("updater.poller.db.get_access_point", side_effect=get_ap), \
         patch("updater.poller.db.get_switch", side_effect=get_switch), \
         patch("updater.poller.db.get_cpe_by_ip", side_effect=get_cpe), \
         patch("updater.poller.get_driver") as factory:
        await poller.poll_configs_for_ips([device_ip])

    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap", "switch", "cpe"])
async def test_config_poll_rejects_change_during_semaphore_wait(mock_db, role):
    poller = NetworkPoller()
    device_ip = {"ap": AP, "switch": SWITCH, "cpe": CPE}[role]
    row = {"ip": device_ip, "username": "device", "password": "test-old"}

    class ChangingSemaphore:
        async def __aenter__(self):
            poller.invalidate_client(AP if role == "cpe" else device_ip)

        async def __aexit__(self, *_args):
            return False

    def get_ap(ip):
        if role == "ap" and ip == AP:
            return row
        if role == "cpe" and ip == AP:
            return {"ip": AP, "username": "parent", "password": "test-old"}
        return None

    def get_switch(ip):
        return row if role == "switch" and ip == SWITCH else None

    with patch("updater.poller.asyncio.Semaphore", return_value=ChangingSemaphore()), \
         patch("updater.poller.db.get_access_point", side_effect=get_ap), \
         patch("updater.poller.db.get_switch", side_effect=get_switch), \
         patch("updater.poller.db.get_cpe_by_ip",
               return_value={"ip": CPE, "ap_ip": AP} if role == "cpe" else None), \
         patch("updater.poller.get_driver") as factory:
        await poller.poll_configs_for_ips([device_ip])

    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap", "switch", "cpe"])
async def test_poll_all_configs_carries_generation_from_inventory(mock_db, role):
    poller = NetworkPoller()
    ap = {"ip": AP, "username": "parent", "password": "test-old"}
    device = {"ip": SWITCH, "username": "switch", "password": "test-old"}

    def get_aps(**_kwargs):
        if role == "ap":
            poller.invalidate_client(AP)
            return [ap]
        return []

    def get_switches(**_kwargs):
        if role == "switch":
            poller.invalidate_client(SWITCH)
            return [device]
        return []

    def get_ap_dict(**_kwargs):
        if role == "cpe":
            poller.invalidate_client(AP)
            return {AP: ap}
        return {}

    with patch("updater.poller.db.get_access_points", side_effect=get_aps), \
         patch("updater.poller.db.get_switches", side_effect=get_switches), \
         patch("updater.poller.db.get_all_cpes",
               return_value=[{"ip": CPE, "ap_ip": AP, "auth_status": "ok"}] if role == "cpe" else []), \
         patch("updater.poller.db.get_all_access_points_dict", side_effect=get_ap_dict), \
         patch("updater.poller.get_driver") as factory:
        await poller.poll_all_configs()

    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap", "switch", "cpe"])
async def test_config_poll_rejects_change_during_device_lock_wait(mock_db, role):
    poller = NetworkPoller()
    device_ip = {"ap": AP, "switch": SWITCH, "cpe": CPE}[role]
    generation = (poller._cpe_credentials_generation if role == "cpe"
                  else poller._client_credentials_generation.get(device_ip, 0))
    credential_ip = None if role == "cpe" else device_ip
    lock = poller._cpe_request_locks.setdefault(device_ip, asyncio.Lock())
    await lock.acquire()
    with patch("updater.poller.get_driver") as factory:
        task = asyncio.create_task(poller._fetch_and_store_config(
            device_ip, "device", "test-old", credential_generation=generation,
            credential_ip=credential_ip,
        ))
        await asyncio.sleep(0)
        poller.invalidate_client(AP if role == "cpe" else device_ip)
        lock.release()
        await task

    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap", "switch", "cpe"])
@pytest.mark.parametrize("invalidate_at", ["login", "read"])
async def test_config_poll_discards_stale_login_and_config(mock_db, role, invalidate_at):
    poller = NetworkPoller()
    device_ip = {"ap": AP, "switch": SWITCH, "cpe": CPE}[role]
    credential_ip = None if role == "cpe" else device_ip
    generation = (poller._cpe_credentials_generation if role == "cpe"
                  else poller._client_credentials_generation.get(device_ip, 0))

    def invalidate():
        poller.invalidate_client(AP if role == "cpe" else device_ip)

    async def connect():
        if invalidate_at == "login":
            invalidate()
        return True

    async def fetch_config():
        if invalidate_at == "read":
            invalidate()
        return {"system": {"name": "device"}}, "ok", None

    client = MagicMock(connect=AsyncMock(side_effect=connect),
                       fetch_config=AsyncMock(side_effect=fetch_config))
    with patch("updater.poller.get_driver", return_value=lambda *args, **kwargs: client), \
         patch("updater.poller.db.update_device_config_poll_status") as update_status, \
         patch("updater.poller.db.save_device_config") as save_config, \
         patch("updater.poller.db.rebind_snapshots") as rebind:
        await poller._fetch_and_store_config(
            device_ip, "device", "test-old", credential_generation=generation,
            credential_ip=credential_ip,
        )

    update_status.assert_not_called()
    save_config.assert_not_called()
    rebind.assert_not_called()
    assert client.fetch_config.await_count == (0 if invalidate_at == "login" else 1)


@pytest.mark.asyncio
async def test_config_poll_recovers_with_current_credentials(mock_db):
    poller = NetworkPoller()
    old = {"ip": AP, "username": "device", "password": "test-old"}
    current = {"ip": AP, "username": "device", "password": "test-current"}

    async def stale_login():
        poller.invalidate_client(AP)
        return True

    stale_client = MagicMock(connect=AsyncMock(side_effect=stale_login))
    current_client = MagicMock(
        connect=AsyncMock(return_value=True),
        fetch_config=AsyncMock(return_value=({"system": {"name": "device"}}, "ok", None)),
    )
    rows = iter([old, current])
    with patch("updater.poller.db.get_access_point", side_effect=lambda _ip: next(rows, current)), \
         patch("updater.poller.db.get_switch", return_value=None), \
         patch("updater.poller.db.get_cpe_by_ip", return_value=None), \
         patch("updater.poller.get_driver",
               side_effect=[lambda *args, **kwargs: stale_client,
                            lambda *args, **kwargs: current_client,
                            lambda *args, **kwargs: current_client]), \
         patch("updater.poller.db.get_latest_config_hash", return_value=None), \
         patch("updater.poller.db.save_device_config") as save_config:
        await poller.poll_configs_for_ips([AP])
        await poller.poll_configs_for_ips([AP])

    assert stale_client.connect.await_count == 1
    assert current_client.connect.await_count == 1
    current_client.fetch_config.assert_awaited_once()
    save_config.assert_called_once()
