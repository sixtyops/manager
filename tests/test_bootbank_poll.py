"""Boot bank read handling in the Tachyon client and AP poller."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater.poller import NetworkPoller
from updater import database
from updater.vendors.tachyon.client import TachyonClient

FIXTURES = Path(__file__).parent / "fixtures" / "tachyon"
IP = "192.0.2.30"


def captured_body():
    capture = json.loads((FIXTURES / "cpe_bootbank.json").read_text())
    return json.dumps(capture["reads"][0]["response"])


@pytest.mark.asyncio
async def test_bootbank_parser_reads_captured_fixture_shape():
    client = TachyonClient(IP, "user", "password")
    client._curl = AsyncMock(return_value=(200, captured_body()))

    result = await client.get_bootbank_info()

    assert (result.active_bank, result.bank1_version, result.bank2_version) == (
        1, "1.15.0.55159", "1.15.0.55151",
    )
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,warning",
    [(503, "private response", "Boot bank read failed (HTTP 503)"),
     (200, "{", "Boot bank read failed (JSONDecodeError)")],
)
async def test_bootbank_parser_reports_status_and_parse_class(status, body, warning):
    client = TachyonClient(IP, "user", "password")
    client._curl = AsyncMock(return_value=(status, body))

    result = await client.get_bootbank_info()

    assert result.warning == warning
    assert result.active_bank is None
    assert body not in result.warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "warning",
    [
        "Boot bank read failed (HTTP 503)",
        "Boot bank read failed (JSONDecodeError)",
        None,
    ],
)
async def test_manual_ap_poll_outcome_matches_bank_read_and_failure_count(monkeypatch, warning):
    import updater.poller as poller_module

    writes = []
    client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={"model": "TNA-301", "firmware_version": "1.15.0"}),
        get_device_info=AsyncMock(return_value=SimpleNamespace(
            bank1_version=None, bank2_version=None, active_bank=None,
        )),
        get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
            bank1_version="1.15.0.55159" if warning is None else None,
            bank2_version="1.15.0.55151" if warning is None else None,
            active_bank=1 if warning is None else None,
            warning=warning,
        )),
        get_connected_cpes=AsyncMock(return_value=[]),
        last_radio_params=None,
    )
    poller = NetworkPoller()
    for _ in range(2):
        poller._record_poll_failure(IP)
    ap = {
        "ip": IP,
        "username": "user",
        "password": "password",
        "last_error": None,
    }
    poller._get_client = AsyncMock(return_value=(client, None))

    def update_ap_status(_ip, **kwargs):
        writes.append(kwargs)
        ap.update(kwargs)

    monkeypatch.setattr(poller_module.db, "get_access_point", lambda _ip: ap)
    monkeypatch.setattr(poller_module.db, "update_ap_status", update_ap_status)
    monkeypatch.setattr(poller_module.db, "update_ap_radio_params", lambda *args, **kwargs: None)
    monkeypatch.setattr(poller_module.db, "prune_stale_cpes", lambda *args: None)
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    succeeded = await poller.poll_ap_now(IP)

    update = writes[0]
    assert update["last_error"] == warning
    assert succeeded is (warning is None)
    if warning:
        assert not {"bank1_version", "bank2_version", "active_bank"} & update.keys()
        assert poller._failure_counts[IP] == 3
        assert poller._is_backed_off(IP)
    else:
        assert update["bank1_version"] == "1.15.0.55159"
        assert update["bank2_version"] == "1.15.0.55151"
        assert update["active_bank"] == 1
        assert IP not in poller._failure_counts
        assert not poller._is_backed_off(IP)


@pytest.mark.asyncio
async def test_tna301_poll_radio_update_keeps_saved_banks_in_sqlite(tmp_path, monkeypatch):
    import updater.poller as poller_module

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sixtyops.db")
    database.init_db()
    database.upsert_access_point(IP, "user", "password", model="TNA-301")
    database.upsert_device("192.0.2.31", "cpe", "user", "password",
                           model="CPE-101")
    database.update_device_status("192.0.2.31", bank1_version="cpe-bank")
    with database.get_db() as db:
        db.execute("UPDATE devices SET bank_last_fetched=? WHERE ip=?", ("kept", IP))

    client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={"model": "TNA-301", "firmware_version": "1.15.0"}),
        get_device_info=AsyncMock(return_value=SimpleNamespace()),
        get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
            bank1_version="1.15.0.55159", bank2_version="1.15.0.55151",
            active_bank=1, warning=None,
        )),
        get_connected_cpes=AsyncMock(return_value=[]),
        last_radio_params={"channel": 36, "channel_width_mhz": 20,
                           "frequency_mhz": 5180, "antenna_kit": "kit-a"},
    )
    poller = NetworkPoller()
    poller._get_client = AsyncMock(return_value=(client, None))
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    await poller.poll_ap_now(IP)

    with database.get_db() as db:
        ap = db.execute(
            "SELECT active_bank,bank1_version,bank2_version,bank_last_fetched "
            "FROM devices WHERE ip=?", (IP,),
        ).fetchone()
        cpe = db.execute(
            "SELECT role,bank1_version FROM devices WHERE ip='192.0.2.31'",
        ).fetchone()
    assert tuple(ap) == (1, "1.15.0.55159", "1.15.0.55151", "kept")
    assert tuple(cpe) == ("cpe", "cpe-bank")
