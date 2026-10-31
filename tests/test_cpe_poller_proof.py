"""Exercise the poller with the trusted session-proof hooks and fake devices."""

import importlib.util
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from updater import database as db
from updater import poller as poller_module
from updater.models import CPEInfo
from updater.poller import NetworkPoller
from updater.vendors import init_vendors
from updater.vendors.tachyon import TachyonDriver
from updater.vendors.tachyon.client import TachyonClient

AP = "192.0.2.1"
CPE = "192.0.2.2"


@pytest.fixture
def fake_devices(mock_db, monkeypatch):
    init_vendors()
    db.upsert_access_point(AP, "test-user", "test-password")
    logins = []

    async def login(client):
        logins.append(client.ip)
        client._token = "fake-token"
        return True

    async def session_valid(client):
        return "ok" if client._token else "expired"

    monkeypatch.setattr(TachyonClient, "login", login)
    monkeypatch.setattr(TachyonClient, "session_valid", session_valid)
    monkeypatch.setattr(TachyonClient, "get_connected_cpes", AsyncMock(
        return_value=[CPEInfo(ip=CPE, mac="00:00:00:00:00:02", link_uptime=1000)]
    ))
    monkeypatch.setattr(TachyonClient, "get_ap_info", AsyncMock(
        return_value={"model": "TNA-301", "firmware_version": "1.0"}
    ))
    monkeypatch.setattr(TachyonDriver, "get_device_info", AsyncMock(
        return_value=SimpleNamespace(bank1_version="1.0", bank2_version="1.0", active_bank=1)
    ))
    monkeypatch.setattr(TachyonDriver, "get_bootbank_info", AsyncMock(
        return_value=SimpleNamespace(warning=None, bank1_version="1.0", bank2_version="1.0", active_bank=1)
    ))
    return logins


def _instrument(monkeypatch):
    default = Path(__file__).resolve().parents[1] / "scripts/bench/session_proof.py"
    proof_path = Path(os.environ.get("TRUSTED_SESSION_PROOF", default))
    spec = importlib.util.spec_from_file_location("trusted_session_proof", proof_path)
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    recorder = proof.Recorder(time.time())
    # Register restoration before the harness replaces these methods.
    for cls, name in [(TachyonClient, "login"), (TachyonClient, "session_valid"),
                      (TachyonClient, "get_connected_cpes"), (NetworkPoller, "_check_cpe_auth")]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    proof._instrument(recorder)
    return recorder


@pytest.mark.asyncio
@pytest.mark.parametrize("instrumented", [False, True])
async def test_poller_alone_counts_logins_and_reuse(fake_devices, monkeypatch, instrumented):
    recorder = _instrument(monkeypatch) if instrumented else None

    poller = NetworkPoller(poll_interval=30)
    await poller._poll_all_aps()
    await poller._poll_all_aps()
    print(f"instrumented={instrumented} actual_cpe_logins={fake_devices.count(CPE)} "
          f"counted_logins={len(recorder.logins[CPE]) if recorder else 0} "
          f"reuse={recorder.checks[CPE]['ok'] if recorder else 0}")
    assert fake_devices.count(CPE) == 1
    assert db.get_all_cpes_grouped()[AP][0]["auth_status"] == "ok"
    assert CPE in poller._cpe_clients
    if instrumented:
        assert len(recorder.logins[CPE]) == 1
        assert recorder.checks[CPE]["poll_ok"] == 2
        assert recorder.checks[CPE]["ok"] == 1


@pytest.mark.asyncio
async def test_poller_alone_daily_and_window_rules(fake_devices, monkeypatch):
    zone = ZoneInfo("America/Chicago")
    current = datetime(2026, 10, 27, 1, 0, tzinfo=zone)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(poller_module, "datetime", Clock)
    for key, value in {"schedule_enabled": "true", "schedule_days": "tue,wed",
                       "schedule_start_hour": "3", "schedule_end_hour": "4",
                       "schedule_timezone": "America/Chicago"}.items():
        db.set_setting(key, value)
    poller = NetworkPoller()
    for now, expected_logins in [
        (current, 1),
        (datetime(2026, 10, 27, 2, 0, tzinfo=zone), 1),
        (datetime(2026, 10, 27, 2, 50, tzinfo=zone), 2),
        (datetime(2026, 10, 27, 3, 15, tzinfo=zone), 2),
        (datetime(2026, 10, 27, 12, 0, tzinfo=zone), 2),
        (datetime(2026, 10, 28, 1, 0, tzinfo=zone), 3),
        (datetime(2026, 10, 28, 3, 0, tzinfo=zone), 4),
    ]:
        current = now
        await poller._poll_all_aps()
        assert fake_devices.count(CPE) == expected_logins
        assert db.get_all_cpes_grouped()[AP][0]["auth_status"] == "ok"



@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(12, 0), (2, 50)])
async def test_sampling_pause_skips_auth_and_keeps_login_due(fake_devices, monkeypatch, hour, minute):
    zone = ZoneInfo("America/Chicago")
    current = datetime(2026, 10, 26, 12, 0, tzinfo=zone)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(poller_module, "datetime", Clock)
    for key, value in {"schedule_enabled": "true", "schedule_days": "tue",
                       "schedule_start_hour": "3", "schedule_end_hour": "4",
                       "schedule_timezone": "America/Chicago"}.items():
        db.set_setting(key, value)
    poller = NetworkPoller()
    await poller._poll_all_aps()
    assert fake_devices.count(CPE) == 1
    markers = set(poller._cpe_fresh_login_periods[CPE])
    current = datetime(2026, 10, 27, hour, minute, tzinfo=zone)
    assert poller._fresh_cpe_login_due(CPE)
    probe = AsyncMock(wraps=poller._probe_cpe_auth)
    banks = AsyncMock(wraps=poller._maybe_fetch_cpe_banks)
    auth_updates = Mock(wraps=db.update_cpe_auth_status)
    monkeypatch.setattr(db, "update_cpe_auth_status", auth_updates)
    monkeypatch.setattr(poller, "_probe_cpe_auth", probe)
    monkeypatch.setattr(poller, "_maybe_fetch_cpe_banks", banks)

    async with poller.pause_cpe_requests(CPE):
        await poller._poll_all_aps()
        await poller._poll_all_aps()
        probe.assert_not_awaited()
        banks.assert_not_awaited()
        auth_updates.assert_not_called()
        assert fake_devices.count(CPE) == 1
        assert db.get_all_cpes_grouped()[AP][0]["auth_status"] not in {"auth_failed", "unreachable"}
        assert poller._cpe_fresh_login_periods[CPE] == markers
        assert poller._fresh_cpe_login_due(CPE)

    await poller._poll_all_aps()
    assert fake_devices.count(CPE) == 2
    assert not poller._fresh_cpe_login_due(CPE)
    assert db.get_all_cpes_grouped()[AP][0]["auth_status"] == "ok"
    await poller._poll_all_aps()
    assert fake_devices.count(CPE) == 2
