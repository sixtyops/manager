"""A5b sampling, identity, and fail-closed recovery tests."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module
from updater.poller import NetworkPoller
from updater.vendors.tachyon.client import TachyonClient


RADIO = "02:00:00:00:00:01"
CUSTOMER = "02:00:00:00:00:02"
CPE_IP = "192.0.2.8"


def _baseline(*, two_way=True, rate=100, age=None):
    lan = [{"mac": CUSTOMER, "port": "eth0"}]
    upstream = [{"mac": CUSTOMER, "port": "prs0"}]
    if age is not None:
        lan[0]["learning_age_seconds"] = age
        upstream[0]["learning_age_seconds"] = age
    return {
        "peers": {RADIO: {
            "ip": CPE_IP,
            "customer_macs": [CUSTOMER],
            "lan": lan,
            "eth0_link_up": True,
            "eth0_state": {"link_up": True, "link_rate_mbps": rate},
            "ap_bytes": (100, 200),
            "eth0_bytes": (100, 200),
            "two_way": two_way,
        }},
        "upstream": upstream,
    }


def _sample(rx, tx, *, eth_rx=None, eth_tx=None, link=True, rate=100, age=None):
    lan = [{"mac": CUSTOMER, "port": "eth0"}]
    upstream = [{"mac": CUSTOMER, "port": "prs0"}]
    if age is not None:
        lan[0]["learning_age_seconds"] = age
        upstream[0]["learning_age_seconds"] = age
    return {
        "peers": {RADIO: {
            "ip": CPE_IP,
            "lan": lan,
            "eth0_link_up": link,
            "eth0_state": {"link_up": link, "link_rate_mbps": rate},
            "ap_bytes": (rx, tx),
            "eth0_bytes": (rx if eth_rx is None else eth_rx,
                           tx if eth_tx is None else eth_tx),
        }},
        "upstream": upstream,
    }


def _decision(baseline, first, second):
    return app_module._ap_customer_recovery_decision(baseline, first, second)


@pytest.mark.asyncio
async def test_customer_recovery_deadline_expiry_fails_closed(monkeypatch):
    monkeypatch.setattr(app_module, "monotonic", lambda: 50.0)
    monkeypatch.setattr(app_module, "get_poller", lambda: NetworkPoller())

    passed, evidence = await app_module._check_ap_customer_recovery(
        None, "192.0.2.1", None, _baseline(), deadline=49.0
    )

    assert not passed
    assert "deadline" in evidence["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mac", "ip", "reason"), [
    ("not-a-mac", CPE_IP, "MAC"),
    (RADIO, "not-an-ip", "IP"),
    (RADIO, "0.0.0.0", "IP"),
])
async def test_unusable_listed_peer_holds_instead_of_being_skipped(mac, ip, reason):
    class AP:
        last_connected_cpes_valid = True

        async def get_connected_cpes(self):
            return [SimpleNamespace(mac=mac, ip=ip)]

    job = SimpleNamespace(ap_cpe_map={}, credentials={})
    with pytest.raises(RuntimeError, match=f"unusable {reason}"):
        await app_module._capture_ap_customer_recovery(job, "192.0.2.1", AP())


@pytest.mark.asyncio
async def test_unknown_a5a_eth0_read_holds_customer_recovery(monkeypatch):
    class AP:
        last_connected_cpes_valid = True

        async def get_connected_cpes(self):
            return [SimpleNamespace(mac=RADIO, ip=CPE_IP, rx_bytes=100, tx_bytes=200)]

        async def get_bridge_table(self):
            return [{"mac": CUSTOMER, "port": "prs0"}]

    class CPE:
        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(extra={"macs": {"eth0": "02:00:00:00:00:03"}})

        async def get_bridge_table(self):
            return [{"mac": CUSTOMER, "port": "eth0"}]

        async def get_eth0_recovery_state(self):
            return {
                "read_state": "unknown", "reason": "missing_rx_bytes",
                "link_up": True, "link_rate_mbps": 100,
                "rx_bytes": None, "tx_bytes": 200,
            }

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: lambda *_args: CPE())
    job = SimpleNamespace(ap_cpe_map={}, credentials={CPE_IP: ("user", "password")})

    with pytest.raises(RuntimeError, match="unknown: missing_rx_bytes"):
        await app_module._capture_ap_customer_recovery(job, "192.0.2.1", AP())


@pytest.mark.asyncio
async def test_bridge_learning_age_is_preserved_for_freshness_gate():
    client = TachyonClient("192.0.2.1", "user", "password")
    client._curl = AsyncMock(return_value=(200, '{"data":"02:00:00:00:00:02 dev eth0 master br-wan age 12\\n"}'))

    rows = await client.get_bridge_table()

    assert rows == [{
        "mac": CUSTOMER, "port": "eth0", "learning_age_seconds": 12,
    }]


@pytest.mark.asyncio
async def test_sampling_repeats_same_run_while_poller_paused_and_records_unproven_age(monkeypatch):
    clock = {"now": 0.0}
    poller = NetworkPoller()
    poller._fetch_and_store_config_unlocked = AsyncMock()
    samples = [_sample(110, 210), _sample(120, 210), _sample(130, 220)]
    seen = []

    async def capture(_job, _ap_ip, _ap, _expected):
        assert CPE_IP in poller._paused_cpe_ips
        await poller._fetch_and_store_config(CPE_IP, "user", "password")
        seen.append(samples[len(seen)])
        clock["now"] += 1
        return seen[-1]

    async def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(app_module, "get_poller", lambda: poller)
    monkeypatch.setattr(app_module, "_capture_ap_customer_recovery", capture)
    monkeypatch.setattr(app_module, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)

    passed, evidence = await app_module._check_ap_customer_recovery(
        None, "192.0.2.1", None, _baseline(age=None), deadline=30.0
    )

    assert passed
    assert len(seen) == 3
    assert evidence["learning_freshness"] == "unproven"
    poller._fetch_and_store_config_unlocked.assert_not_awaited()
    assert not poller._paused_cpe_ips


@pytest.mark.asyncio
async def test_sample_finishing_after_absolute_deadline_cannot_pass(monkeypatch):
    clock = {"now": 0.0}
    poller = NetworkPoller()
    samples = [_sample(110, 210), _sample(120, 220)]
    calls = []

    async def capture(*_args):
        sample = samples[len(calls)]
        calls.append(sample)
        clock["now"] += 1 if len(calls) == 1 else 2
        return sample

    async def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(app_module, "get_poller", lambda: poller)
    monkeypatch.setattr(app_module, "_capture_ap_customer_recovery", capture)
    monkeypatch.setattr(app_module, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)

    passed, evidence = await app_module._check_ap_customer_recovery(
        None, "192.0.2.1", None, _baseline(), deadline=12.0
    )

    assert not passed
    assert len(calls) == 2
    assert "deadline" in evidence["reason"]


def test_management_only_or_one_way_samples_do_not_pass():
    baseline = _baseline()
    first = _sample(110, 210, eth_rx=100, eth_tx=200)
    second = _sample(120, 220, eth_rx=100, eth_tx=200)

    assert _decision(baseline, first, second)[0] is False
    assert _decision(baseline, _sample(110, 210), _sample(120, 210))[0] is False


def test_zero_or_one_way_baseline_does_not_require_two_way_traffic():
    assert _decision(_baseline(two_way=False), _sample(100, 200), _sample(100, 200)) == (True, "")


@pytest.mark.parametrize(("first", "second"), [
    (_sample(110, 210), _sample(109, 220)),
    (_sample(110, 210), _sample(120, 209)),
    (_sample(110, 210, eth_rx=110), _sample(120, 220, eth_rx=109)),
    (_sample(110, 210, eth_tx=210), _sample(120, 220, eth_tx=209)),
])
def test_counter_reset_in_either_counter_run_holds(first, second):
    passed, reason = _decision(_baseline(), first, second)
    assert not passed
    assert "reset" in reason
