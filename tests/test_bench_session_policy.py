"""Exercise the poller with the trusted session-proof hooks and fake devices."""

import importlib.util
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    monkeypatch.setenv("SIXTYOPS_BENCH_AP_IP", AP)
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
    proof_path = Path(os.environ.get("SESSION_PROOF_UNDER_TEST", default))
    spec = importlib.util.spec_from_file_location("trusted_session_proof", proof_path)
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    recorder = proof.Recorder(time.time())
    # Register restoration before the harness replaces these methods.
    for cls, name in [(TachyonClient, "login"), (TachyonClient, "session_valid"),
                      (TachyonClient, "get_connected_cpes"), (NetworkPoller, "_check_cpe_auth")]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    proof._instrument(recorder)
    return recorder, proof


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["actual", "uncapped"])
@pytest.mark.parametrize("recovers", [True, False])
async def test_session_proof_policy_and_logout_recovery(fake_devices, monkeypatch, policy, recovers):
    if policy == "uncapped":
        def cached_without_age(poller, ip):
            entry = poller._cpe_clients.get(ip)
            return entry[0] if entry else None

        monkeypatch.setattr(NetworkPoller, "_get_cached_cpe_client", cached_without_age)
        monkeypatch.setattr(NetworkPoller, "_evict_stale_cpe_clients", lambda *args: None)
    if not recovers:
        monkeypatch.setattr(TachyonClient, "session_valid", AsyncMock(return_value="ok"))
    with monkeypatch.context() as detection:
        if policy == "uncapped":
            detection.delattr(poller_module, "_CLIENT_TTL_SECONDS", raising=False)
        recorder, proof = _instrument(monkeypatch)
    poller = NetworkPoller(poll_interval=30)
    for _ in range(6):
        await poller._poll_all_aps()

    failures = proof._evaluate(recorder, set(), True)
    print(f"policy={policy} cap={recorder.max_age_seconds} "
          f"logins={fake_devices.count(CPE)} reuse={recorder.checks[CPE]['ok']} failures={failures}")
    assert CPE in recorder.logout_succeeded
    if recovers:
        assert failures == []
        assert CPE in recorder.post_logout_logins
        assert CPE in recorder.post_logout_reuse
        assert fake_devices.count(CPE) == (3 if recorder.max_age_seconds is not None else 2)
    else:
        assert any("no re-login after CPE logout" in failure for failure in failures)
        assert any("polling did not continue after CPE logout" in failure for failure in failures)
    if recorder.max_age_seconds is None:
        assert not recorder.aged_cpes
        assert not recorder.renewal_logins
    else:
        assert CPE in recorder.aged_cpes
        assert CPE in recorder.renewal_logins
