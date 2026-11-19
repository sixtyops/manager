"""Offline proof for bounded AP identity discovery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from updater.ap_locator import APLocatorError, SavedCPE, SearchState, locate_ap

OLD = "192.0.2.10"
NEW = "192.0.2.11"
OTHER = "192.0.2.12"
CPE = "192.0.2.20"
ETHERNET = "02:11:22:33:44:50"
RADIO = "02:11:22:33:44:51"
AP_INFO = SimpleNamespace(serial="ap-serial", model="TNA-301")
CPE_INFO = SimpleNamespace(serial="cpe-serial", model="TNA-302")
PEER = SavedCPE(CPE, CPE_INFO.serial, CPE_INFO.model, "saved-cpe-source")


def row(ip=NEW, mac=RADIO):
    return {"ip": ip, "mac": mac, "system_name": "not-identity", "model": "not-identity"}


class Harness:
    def __init__(self):
        self.now = 0
        self.sleeps = []
        self.events = []
        self.saved = []
        self.state = SearchState()
        self.responses = {
            CPE: {"info": CPE_INFO, "report": [row()]},
            OLD: {"login": OSError("offline")},
            NEW: {"info": AP_INFO},
        }
        self.options = dict(
            serial=AP_INFO.serial, model=AP_INFO.model,
            ethernet_mac=ETHERNET, radio_mac=RADIO, last_ip=OLD,
            credential_ref="saved-ap-source", peers=[PEER],
            reboot_started=0, deadline=600, state=self.state,
            client_factory=self.factory, checkpoint=self.checkpoint,
            clock=lambda: self.now, sleep=self.sleep,
        )

    async def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration

    async def checkpoint(self, state):
        self.saved.append(deepcopy(state))

    def factory(self, ip, reference):
        harness = self
        response = self.responses.get(ip, {"login": OSError("offline")})
        if callable(response):
            response = response(self.state.rounds)

        class Client:
            last_connected_aps_valid = response.get("valid", True)

            async def call(self, method, default):
                harness.events.append((harness.state.rounds, ip, method, harness.now, reference))
                assert harness.saved[-1].rounds == harness.state.rounds
                if ip != CPE:
                    assert ip in harness.saved[-1].addresses
                harness.now += response.get(method + "_delay", 0)
                value = response.get(method, default)
                if isinstance(value, Exception):
                    raise value
                return value

            async def login(self):
                return await self.call("login", True)

            async def get_device_info(self):
                return await self.call("info", None)

            async def get_connected_aps(self):
                return await self.call("report", [])

        return Client()

    async def run(self, **changes):
        return await locate_ap(**(self.options | changes))


@pytest.fixture
def h():
    return Harness()


@pytest.mark.asyncio
@pytest.mark.parametrize("mac", [RADIO, ETHERNET, "021122334451", "02-11-22-33-44-50"])
async def test_either_saved_mac_selects_one_authenticated_candidate(h, mac):
    h.responses[CPE]["report"] = [row(mac=mac), row(mac=mac)]
    assert await h.run() == NEW
    assert h.state.rounds == 1
    assert h.state.addresses == {OLD, NEW}
    assert [(ip, ref) for _, ip, method, _, ref in h.events if method == "login"] == [
        (CPE, "saved-cpe-source"), (OLD, "saved-ap-source"), (NEW, "saved-ap-source"),
    ]
    assert len(h.saved) == 2
    assert h.saved[0].next_probe_at == 120
    assert h.saved[1].addresses == {OLD, NEW}


@pytest.mark.asyncio
@pytest.mark.parametrize("peers", [[], [SavedCPE(CPE, "", "", "")]])
async def test_old_address_passes_without_usable_saved_cpes(h, peers):
    h.responses[OLD] = {"info": AP_INFO}
    assert await h.run(peers=peers) == OLD
    assert all(event[1] == OLD for event in h.events)


@pytest.mark.asyncio
async def test_unchanged_address_passes_with_valid_empty_report(h):
    h.responses[OLD] = {"info": AP_INFO}
    h.responses[CPE]["report"] = []
    assert await h.run() == OLD


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_cpe", [
    {"login": OSError("offline")}, {"login": "Invalid credentials"},
    {"info": SimpleNamespace(serial="wrong", model="TNA-302")},
    {"info": SimpleNamespace(serial="cpe-serial", model="wrong")},
    {"info": SimpleNamespace(serial=None, model="TNA-302")},
    {"info": CPE_INFO, "valid": False, "report": [row()]},
    {"info": CPE_INFO, "report": {}},
])
async def test_untrusted_cpe_cannot_supply_evidence_or_accept_old_address(h, bad_cpe):
    h.responses[CPE] = bad_cpe
    h.responses[OLD] = {"info": AP_INFO}
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=125)
    assert NEW not in h.state.addresses
    assert not any(event[1] == NEW for event in h.events)
    assert h.now == 125


@pytest.mark.asyncio
async def test_read_all_cpes_before_any_ap_and_deduplicate_reports(h):
    second = SavedCPE("192.0.2.21", "cpe-serial", "TNA-302", "second-source")
    h.responses[second.ip] = {"info": CPE_INFO, "report": [row()]}
    assert await h.run(peers=[PEER, second]) == NEW
    logins = [event[1] for event in h.events if event[2] == "login"]
    assert logins == [CPE, second.ip, OLD, NEW]


@pytest.mark.asyncio
async def test_empty_then_returning_peers_keep_search_open(h):
    h.responses[CPE] = lambda turn: {"info": CPE_INFO, "report": [] if turn == 1 else [row()]}
    assert await h.run() == NEW
    assert h.state.rounds == 2
    assert h.sleeps == [90, 30]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    OSError("offline"), TimeoutError("temporary transport failure"),
    SimpleNamespace(serial=None, model="TNA-301"),
    SimpleNamespace(serial="ap-serial", model=None),
])
async def test_unready_reported_candidate_is_retained_and_retried(h, failure):
    h.responses[CPE] = lambda turn: {"info": CPE_INFO, "report": [row()] if turn == 1 else []}
    h.responses[NEW] = lambda turn: {"info": failure if turn == 1 else AP_INFO}
    assert await h.run() == NEW
    assert h.state.rounds == 2
    assert h.state.addresses == {OLD, NEW}


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [
    SimpleNamespace(serial="wrong", model="TNA-301"),
    SimpleNamespace(serial="ap-serial", model="wrong"),
    SimpleNamespace(serial=None, model="wrong"),
    SimpleNamespace(serial="wrong", model=None),
])
@pytest.mark.parametrize("ip", [OLD, NEW])
async def test_authenticated_wrong_identity_stops_at_any_address(h, identity, ip):
    h.responses[ip] = {"info": identity}
    with pytest.raises(APLocatorError, match="identity mismatch"):
        await h.run()
    assert h.state.rounds == 1


@pytest.mark.asyncio
async def test_two_verified_addresses_return_last_ip_first(h):
    h.responses[OLD] = {"info": AP_INFO}
    assert await h.run() == OLD
    assert [event[1] for event in h.events if event[2] == "info" and event[1] != CPE] == [OLD, NEW]


@pytest.mark.asyncio
@pytest.mark.parametrize("report", [[NEW, OTHER], [OTHER, NEW]])
async def test_two_alternatives_return_first_sorted_address_when_last_ip_is_dead(h, report):
    h.responses[CPE]["report"] = [row(ip) for ip in report]
    h.responses[OTHER] = {"info": AP_INFO}
    assert await h.run() == NEW


@pytest.mark.asyncio
async def test_three_verified_addresses_return_last_ip(h):
    h.responses[CPE]["report"] = [row(OTHER), row()]
    h.responses[OLD] = h.responses[OTHER] = {"info": AP_INFO}
    assert await h.run() == OLD
    assert [event[1] for event in h.events if event[2] == "info" and event[1] != CPE] == [
        OLD, NEW, OTHER,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [
    SimpleNamespace(serial="wrong", model="TNA-301"),
    SimpleNamespace(serial="ap-serial", model="wrong"),
])
async def test_two_matches_cannot_pass_with_a_conflicting_candidate(h, identity):
    h.responses[CPE]["report"] = [row(), row(OTHER)]
    h.responses[OLD] = {"info": AP_INFO}
    h.responses[OTHER] = {"info": identity}
    with pytest.raises(APLocatorError, match="identity mismatch"):
        await h.run()
    assert h.state.rounds == 1


@pytest.mark.asyncio
async def test_two_matches_cannot_pass_with_an_unready_reported_candidate(h):
    h.responses[CPE]["report"] = [row(), row(OTHER)]
    h.responses[OLD] = {"info": AP_INFO}
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=125)
    assert h.state.rounds == 2
    assert OTHER in h.state.addresses


@pytest.mark.asyncio
async def test_expiry_during_final_candidate_cannot_accept_earlier_matches(h):
    h.responses[CPE]["report"] = [row(), row(OTHER)]
    h.responses[OLD] = {"info": AP_INFO}
    h.responses[OTHER] = {"info": AP_INFO, "login_delay": 15}
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=100)
    assert h.now == 105
    assert not any(event[1:3] == (OTHER, "info") for event in h.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["224.0.0.1", "255.255.255.255", "0.0.0.0", "0.1.2.3", "127.0.0.1", "::1", "bad", None, 42])
async def test_invalid_non_unicast_and_unrelated_candidates_are_not_probed(h, ip):
    h.responses[CPE]["report"] = [row(ip), row(OTHER, "02:99:99:99:99:99"), row()]
    assert await h.run() == NEW
    assert h.state.addresses == {OLD, NEW}


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"deadline": float("nan")}, {"deadline": float("inf")}, {"deadline": 0},
    {"serial": ""}, {"model": None}, {"radio_mac": None},
    {"ethernet_mac": "bad"}, {"last_ip": None}, {"credential_ref": ""},
    {"state": SearchState(rounds=-1)}, {"state": SearchState(rounds=True)},
    {"state": SearchState(next_probe_at=float("nan"))},
    {"state": SearchState(addresses={None})},
    {"state": SearchState(rounds=1)},
])
async def test_invalid_state_stops_before_any_probe(h, changes):
    with pytest.raises(APLocatorError, match="Invalid AP locator state"):
        await h.run(**changes)
    assert h.events == []


@pytest.mark.asyncio
async def test_expired_deadline_stops_without_sleep_or_probe(h):
    h.now = 600
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run()
    assert h.events == h.sleeps == h.saved == []


@pytest.mark.asyncio
async def test_first_probe_delay_is_clipped_to_deadline(h):
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=20)
    assert h.sleeps == [20]
    assert h.events == []


@pytest.mark.asyncio
async def test_slow_round_consumes_time_without_catch_up_rounds(h):
    h.responses[CPE] = {"info": CPE_INFO, "report": [], "report_delay": 40}
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=175)
    starts = [event[3] for event in h.events if event[1:3] == (CPE, "login")]
    assert starts == [90, 130, 170]
    assert h.sleeps == [90]


@pytest.mark.asyncio
async def test_round_twenty_is_allowed_but_twenty_one_stops(h):
    h.state.rounds = 19
    h.state.addresses = {OLD}
    h.state.next_probe_at = 90
    h.responses[CPE]["report"] = []
    with pytest.raises(APLocatorError, match="round limit"):
        await h.run()
    assert h.state.rounds == 20
    assert {event[0] for event in h.events} == {20}


@pytest.mark.asyncio
async def test_round_limit_survives_another_call(h):
    h.state.rounds = 20
    h.state.addresses = {OLD}
    h.state.next_probe_at = 590
    with pytest.raises(APLocatorError, match="round limit"):
        await h.run()
    assert h.events == h.saved == h.sleeps == []


@pytest.mark.asyncio
async def test_saved_spacing_and_candidates_survive_another_call(h):
    h.now = 105
    h.state.rounds = 2
    h.state.next_probe_at = 140
    h.state.addresses = {OLD, NEW}
    h.responses[CPE]["report"] = []
    assert await h.run() == NEW
    assert h.sleeps == [35]
    assert h.state.rounds == 3


@pytest.mark.asyncio
async def test_thirty_third_cumulative_address_stops_before_ap_probes(h):
    h.state.addresses = {OLD, *(f"192.0.2.{n}" for n in range(40, 71))}
    with pytest.raises(APLocatorError, match="address limit"):
        await h.run()
    assert all(event[1] == CPE for event in h.events)
    assert len(h.state.addresses) == 33


@pytest.mark.asyncio
async def test_thirty_two_addresses_are_allowed(h):
    h.responses[CPE]["report"] = [row(f"192.0.2.{n}") for n in range(40, 71)]
    for ip in [f"192.0.2.{n}" for n in range(40, 71)]:
        h.responses[ip] = {"info": SimpleNamespace(serial=None, model=None)}
    h.responses[OLD] = {"info": AP_INFO}
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(deadline=95)
    assert len(h.state.addresses) == 32
    assert len({event[1] for event in h.events if event[1] != CPE}) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_checkpoint", [1, 2])
@pytest.mark.parametrize("failure", ["exception", "false"])
async def test_checkpoint_failure_blocks_following_probes(h, failed_checkpoint, failure):
    async def save(state):
        await h.checkpoint(state)
        if len(h.saved) == failed_checkpoint:
            if failure == "exception":
                raise OSError("synthetic persistence failure")
            return False

    with pytest.raises(APLocatorError, match="checkpoint failed"):
        await h.run(checkpoint=save)
    assert not h.events if failed_checkpoint == 1 else all(event[1] == CPE for event in h.events)


@pytest.mark.asyncio
async def test_external_cancellation_propagates(h):
    async def cancelled_sleep(duration):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await h.run(sleep=cancelled_sleep)
    assert h.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", ["checkpoint", "login", "info"])
async def test_whole_locator_deadline_cancels_a_stalled_operation(h, stalled):
    from unittest.mock import AsyncMock

    clock = asyncio.get_running_loop().time
    started = clock()
    cancelled = []

    async def stall(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(stalled)

    client = SimpleNamespace(login=AsyncMock(return_value=True), get_device_info=AsyncMock(return_value=AP_INFO))
    if stalled == "login":
        client.login.side_effect = stall
    elif stalled == "info":
        client.get_device_info.side_effect = stall
    with pytest.raises(APLocatorError, match="Device not found after update"):
        await h.run(
            clock=clock, reboot_started=started - 90, deadline=started + 0.03,
            peers=[], client_factory=lambda ip, ref: client,
            checkpoint=stall if stalled == "checkpoint" else h.checkpoint,
        )
    assert cancelled == [stalled]
    assert clock() - started < 1
