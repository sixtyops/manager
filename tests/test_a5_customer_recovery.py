"""A5 AP peer-return and reported CPE link recovery tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module


RADIO = "02:00:00:00:00:01"
CPE_IP = "192.0.2.8"
AP_IP = "192.0.2.1"


class AP:
    last_connected_cpes_valid = True

    def __init__(self, peers):
        self.peers = peers
        self.get_bridge_table = AsyncMock(side_effect=AssertionError("bridge data is not A5 evidence"))

    async def get_connected_cpes(self):
        return self.peers


class CPE:
    def __init__(self, link=True):
        self.link = link
        self.connect = AsyncMock(return_value=True)
        self.get_eth0_recovery_state = AsyncMock(return_value={
            "read_state": "ok", "link_up": link,
            "rx_bytes": 0, "tx_bytes": 0, "link_rate_mbps": 1,
        })
        self.get_bridge_table = AsyncMock(side_effect=AssertionError("bridge data is not A5 evidence"))


def _peer():
    return SimpleNamespace(mac=RADIO, ip=CPE_IP)


def _baseline(link_up=True):
    return {"peers": {RADIO: {"ip": CPE_IP, "eth0_link_was_up": link_up}}}


def _job():
    return SimpleNamespace(
        credentials={CPE_IP: ("user", "password")}, ap_cpe_map={},
    )


@pytest.mark.asyncio
async def test_baseline_records_reported_peers_and_eth0_link_only(monkeypatch):
    cpe = CPE(link=True)
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: lambda *_args: cpe)
    ap = AP([_peer()])

    baseline = await app_module._capture_ap_recovery_baseline(_job(), AP_IP, ap)

    assert baseline == _baseline()
    assert cpe.get_eth0_recovery_state.await_count == 1
    ap.get_bridge_table.assert_not_awaited()
    cpe.get_bridge_table.assert_not_awaited()


@pytest.mark.asyncio
async def test_baseline_fails_closed_when_ap_peer_report_is_invalid():
    ap = AP([SimpleNamespace(mac="not-a-mac", ip=CPE_IP)])

    with pytest.raises(RuntimeError, match="unusable MAC"):
        await app_module._capture_ap_recovery_baseline(_job(), AP_IP, ap)


@pytest.mark.asyncio
@pytest.mark.parametrize(("reported_peers", "link_up", "link_was_up", "expected_reason"), [
    ([], True, True, "missing from the AP peer list"),
    ([_peer()], False, True, "eth0 link is down"),
])
async def test_recovery_fails_closed_for_missing_peer_or_previously_up_link(
    monkeypatch, reported_peers, link_up, link_was_up, expected_reason,
):
    clock = {"now": 0.0}
    cpe = CPE(link=link_up)
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: lambda *_args: cpe)
    monkeypatch.setattr(app_module, "monotonic", lambda: clock["now"])

    async def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)
    passed, evidence = await app_module._check_ap_recovery(
        _job(), AP_IP, AP(reported_peers), _baseline(link_was_up), deadline=1.0
    )

    assert not passed
    assert expected_reason in evidence["reason"]


@pytest.mark.asyncio
async def test_recovery_accepts_returned_peer_when_eth0_was_down_before_reboot(monkeypatch):
    cpe_factory = AsyncMock()
    cpe_factory.connect = AsyncMock(return_value=True)
    cpe_factory.get_eth0_recovery_state = AsyncMock(return_value={"read_state": "ok", "link_up": False})
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: lambda *_args: cpe_factory)

    passed, evidence = await app_module._check_ap_recovery(
        _job(), AP_IP, AP([_peer()]), _baseline(link_up=False),
        deadline=app_module.monotonic() + 10.0,
    )

    assert passed
    assert evidence == {"returned_peers": [RADIO]}
    cpe_factory.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_uses_reported_link_even_when_counters_are_missing(monkeypatch):
    cpe = CPE(link=True)
    cpe.get_eth0_recovery_state.return_value = {
        "read_state": "unknown", "link_up": True,
        "rx_bytes": None, "tx_bytes": None,
    }
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: lambda *_args: cpe)

    passed, evidence = await app_module._check_ap_recovery(
        _job(), AP_IP, AP([_peer()]), _baseline(),
        deadline=app_module.monotonic() + 10.0,
    )

    assert passed
    assert evidence == {"returned_peers": [RADIO]}


@pytest.mark.asyncio
async def test_recovery_requires_valid_report_and_deadline(monkeypatch):
    ap = AP([_peer()])
    ap.last_connected_cpes_valid = False
    times = iter((0.0, 2.0, 3.0))
    monkeypatch.setattr(app_module, "monotonic", lambda: next(times))
    passed, evidence = await app_module._check_ap_recovery(_job(), AP_IP, ap, _baseline(), deadline=1.0)
    assert not passed
    assert "peer list could not be read" in evidence["reason"]

    passed, evidence = await app_module._check_ap_recovery(_job(), AP_IP, AP([_peer()]), _baseline(), deadline=None)
    assert not passed
    assert "deadline is unavailable" in evidence["reason"]
