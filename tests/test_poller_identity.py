"""Identity recording from authenticated poll results."""

from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import pytest

from updater import database
from updater.models import CPEInfo
from updater.poller import NetworkPoller


IP = "192.0.2.51"
CPE_IP = "192.0.2.52"
SWITCH_IP = "192.0.2.53"


@pytest.mark.asyncio
async def test_ap_poll_promotes_legacy_identity_after_authenticated_info(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sixtyops.db")
    database.init_db()
    database.upsert_access_point(IP, "user", "password")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE devices SET uid=?,uid_source='ip_legacy' WHERE ip=?",
            (f"legacy-ip:{IP}", IP),
        )
    client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={
            "model": "TNA-301", "firmware_version": "1.0", "mac": "00:11:22:33:44:55",
        }),
        get_device_info=AsyncMock(return_value=SimpleNamespace(
            serial="SN-POLL-AP", mac="00:11:22:33:44:55",
            bank1_version="1.0", bank2_version="1.0", active_bank=1,
        )),
        get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
            warning=None, bank1_version="1.0", bank2_version="1.0", active_bank=1,
        )),
        get_connected_cpes=AsyncMock(return_value=[]),
        last_radio_params=None,
    )
    poller = NetworkPoller()
    poller._get_client = AsyncMock(return_value=(client, None))
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    await poller.poll_ap_now(IP)

    with database.get_db() as conn:
        row = conn.execute(
            "SELECT id,uid,uid_source FROM devices WHERE ip=?", (IP,),
        ).fetchone()
        mac = conn.execute(
            "SELECT mac,interface FROM device_macs WHERE device_id=?", (row["id"],),
        ).fetchone()
    assert (row["uid"], row["uid_source"]) == ("SN-POLL-AP", "serial")
    assert tuple(mac) == ("00:11:22:33:44:55", "eth0")


@pytest.mark.parametrize("serial", [None, "unknown"])
def test_poll_identity_without_valid_serial_changes_nothing(mock_db, serial):
    device_id = database.upsert_device(IP, "ap", "user", "password")
    with mock_db:
        mock_db.execute(
            "UPDATE devices SET uid=?,uid_source='ip_legacy' WHERE id=?",
            (f"legacy-ip:{IP}", device_id),
        )
    poller = NetworkPoller()

    poller._record_polled_identity(
        device_id, SimpleNamespace(serial=serial, mac="00:11:22:33:44:55"), 0, IP,
    )

    row = mock_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (device_id,),
    ).fetchone()
    assert tuple(row) == (f"legacy-ip:{IP}", "ip_legacy")
    assert mock_db.execute(
        "SELECT COUNT(*) FROM device_macs WHERE device_id=?", (device_id,),
    ).fetchone()[0] == 0
    assert mock_db.execute(
        "SELECT COUNT(*) FROM identity_conflicts",
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cpe_poll_records_identity_after_successful_authentication(mock_db, monkeypatch):
    database.upsert_access_point(IP, "user", "password")
    cpe_id = database.upsert_device(CPE_IP, "cpe", "user", "password")
    database.upsert_cpe(IP, {"ip": CPE_IP, "mac": "00:11:22:33:44:66"})
    ap_client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={"model": "TNA-301", "firmware_version": "1.0"}),
        get_device_info=AsyncMock(return_value=SimpleNamespace(
            serial=None, mac=None, bank1_version="1.0", bank2_version="1.0", active_bank=1,
        )),
        get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
            warning=None, bank1_version="1.0", bank2_version="1.0", active_bank=1,
        )),
        get_connected_cpes=AsyncMock(return_value=[CPEInfo(
            ip=CPE_IP, mac="00:11:22:33:44:66",
        )]),
        last_radio_params=None,
    )
    cpe_client = SimpleNamespace(get_device_info=AsyncMock(return_value=SimpleNamespace(
        serial="SN-POLL-CPE", mac="00:11:22:33:44:77",
        bank1_version="1.0", bank2_version="1.0", active_bank=1,
    )))
    poller = NetworkPoller()
    poller._get_client = AsyncMock(return_value=(ap_client, None))
    poller._probe_cpe_auth = AsyncMock(return_value="ok")
    poller._cpe_clients[CPE_IP] = (cpe_client, time.time())
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    await poller._poll_ap(database.get_access_point(IP))

    row = mock_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (cpe_id,),
    ).fetchone()
    mac = mock_db.execute(
        "SELECT mac,interface FROM device_macs WHERE device_id=?", (cpe_id,),
    ).fetchone()
    assert tuple(row) == ("SN-POLL-CPE", "serial")
    assert tuple(mac) == ("00:11:22:33:44:77", "eth0")


@pytest.mark.asyncio
async def test_switch_poll_promotes_legacy_identity_after_authenticated_info(mock_db, monkeypatch):
    switch_id = database.upsert_switch(SWITCH_IP, "user", "password")
    client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={"model": "TNS-100", "firmware_version": "1.0"}),
        get_device_info=AsyncMock(return_value=SimpleNamespace(
            serial="SN-POLL-SWITCH", mac="00:11:22:33:44:88",
            bank1_version="1.0", bank2_version="1.0", active_bank=1,
        )),
        get_bridge_table=AsyncMock(return_value=[]),
    )
    poller = NetworkPoller()
    poller._get_client = AsyncMock(return_value=(client, None))
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    await poller._poll_switch(database.get_switch(SWITCH_IP))

    row = mock_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (switch_id,),
    ).fetchone()
    assert tuple(row) == ("SN-POLL-SWITCH", "serial")
