"""Boot bank read handling in the Tachyon client and AP poller."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater.poller import NetworkPoller
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
    "warning", ["Boot bank read failed (HTTP 503)", "Boot bank read failed (JSONDecodeError)"]
)
async def test_ap_poll_records_warning_and_preserves_last_good_bank(monkeypatch, warning):
    import updater.poller as poller_module

    writes = []
    client = SimpleNamespace(
        get_ap_info=AsyncMock(return_value={"model": "TNA-301", "firmware_version": "1.15.0"}),
        get_device_info=AsyncMock(return_value=SimpleNamespace(
            bank1_version=None, bank2_version=None, active_bank=None,
        )),
        get_bootbank_info=AsyncMock(return_value=SimpleNamespace(
            bank1_version=None, bank2_version=None, active_bank=None, warning=warning,
        )),
        get_connected_cpes=AsyncMock(return_value=[]),
        last_radio_params=None,
    )
    poller = NetworkPoller()
    poller._get_client = AsyncMock(return_value=(client, None))
    monkeypatch.setattr(poller_module.db, "update_ap_status", lambda ip, **kwargs: writes.append(kwargs))
    monkeypatch.setattr(poller_module.db, "update_ap_radio_params", lambda *args, **kwargs: None)
    monkeypatch.setattr(poller_module.db, "prune_stale_cpes", lambda *args: None)
    monkeypatch.setattr(poller, "_check_uptime_transition", lambda *args: None)

    await poller._poll_ap({
        "ip": IP, "username": "user", "password": "password", "last_error": None,
    })

    update = writes[0]
    assert update["last_error"] == warning
    assert not {"bank1_version", "bank2_version", "active_bank"} & update.keys()
