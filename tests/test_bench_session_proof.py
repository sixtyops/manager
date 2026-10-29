"""Unit tests for the sanitized read-only bench session proof."""

import logging
import time
from types import SimpleNamespace

import pytest

from scripts.bench.session_proof import (
    Recorder,
    RunErrorHandler,
    _age_cached_cpe_login,
    _evaluate,
    _logout_cpe_session,
)


def _passing_recorder(monkeypatch):
    monkeypatch.setenv("SIXTYOPS_BENCH_AP_IP", "192.0.2.1")
    recorder = Recorder(0)
    recorder.order.extend(["192.0.2.1", "192.0.2.2"])
    recorder.logins["192.0.2.2"] = [(0, True), (30, True), (90, True)]
    recorder.login_triggers["192.0.2.2"] = [None, "age", "logout"]
    recorder.checks["192.0.2.2"]["ok"] = 3
    recorder.check_events["192.0.2.2"] = [
        (30, "ok"), (31, "poll_ok"), (90, "ok"), (91, "poll_ok"),
        (150, "ok"), (151, "poll_ok"), (180, "ok"), (181, "poll_ok"),
    ]
    recorder.aged_cpes.add("192.0.2.2")
    recorder.renewal_logins.add("192.0.2.2")
    recorder.post_renewal_reuse.add("192.0.2.2")
    recorder.logout_succeeded.add("192.0.2.2")
    recorder.post_logout_logins.add("192.0.2.2")
    recorder.post_logout_poll_floor["192.0.2.2"] = 3
    recorder.post_logout_reuse.add("192.0.2.2")
    recorder.cpe_poll([{"ip": "192.0.2.2", "link_uptime": 1000}])
    recorder.cpe_poll([{"ip": "192.0.2.2", "link_uptime": 1060}])
    recorder.cpe_poll([{"ip": "192.0.2.2", "link_uptime": 1120}])
    recorder.cpe_poll([{"ip": "192.0.2.2", "link_uptime": 1180}])
    return recorder


def test_clean_run_passes_and_reports_link_uptime(monkeypatch, capsys):
    recorder = _passing_recorder(monkeypatch)

    assert _evaluate(recorder, set(), has_session_valid=True) == []
    from scripts.bench.session_proof import _report

    _report(recorder, 1200, has_session_valid=True)
    output = capsys.readouterr().out
    assert "link device=cpe-1 uptime_start_s=1000 uptime_end_s=1180 resets=0" in output
    assert "192.0.2.2" not in output


def test_link_uptime_reset_fails_and_reports_reset_count(monkeypatch, capsys):
    recorder = _passing_recorder(monkeypatch)
    recorder.cpe_poll([{"ip": "192.0.2.2", "link_uptime": 20}])

    assert "wireless link uptime reset for cpe-1" in _evaluate(recorder, set(), True)

    from scripts.bench.session_proof import _report

    _report(recorder, 1200, has_session_valid=True)
    output = capsys.readouterr().out
    assert "link device=cpe-1 uptime_start_s=1000 uptime_end_s=20 resets=1" in output


def test_cpe_drop_from_ap_poll_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.cpe_poll([])

    assert "CPE dropped off AP for cpe-1" in _evaluate(recorder, set(), True)


def test_age_helper_moves_cached_login_past_ttl():
    client = object()
    poller = SimpleNamespace(_cpe_clients={"192.0.2.2": (client, time.time())})

    assert _age_cached_cpe_login(poller, "192.0.2.2")
    cached_client, logged_in_at = poller._cpe_clients["192.0.2.2"]
    assert cached_client is client
    assert time.time() - logged_in_at > 600


@pytest.mark.asyncio
async def test_logout_helper_uses_authenticated_device_logout():
    class Client:
        async def _curl(self, method, endpoint):
            self.request = (method, endpoint)
            return 204, ""

    client = Client()

    assert await _logout_cpe_session(client)
    assert client.request == ("DELETE", "/cgi.lua/apiv1/login")


@pytest.mark.parametrize(
    ("stage", "failure"),
    [
        ("aged_cpes", "cached CPE login was not aged"),
        ("renewal_logins", "no re-login after the 600-second login-age limit"),
        ("post_renewal_reuse", "no reuse after re-login"),
        ("logout_succeeded", "CPE session logout failed"),
        ("post_logout_logins", "no re-login after CPE logout"),
        ("post_logout_reuse", "polling did not continue after CPE logout"),
    ],
)
def test_missing_recovery_step_fails(monkeypatch, stage, failure):
    recorder = _passing_recorder(monkeypatch)
    getattr(recorder, stage).discard("192.0.2.2")

    assert any(failure in reason for reason in _evaluate(recorder, set(), True))


def test_poll_error_fails_with_safe_reason():
    handler = RunErrorHandler()
    handler.emit(logging.LogRecord("updater.poller", logging.ERROR, "poller.py", 1,
                                   "Error polling 192.0.2.2: private detail", (), None))

    assert handler.errors == {"poll error during run"}


def test_sqlite_error_fails_with_safe_reason():
    handler = RunErrorHandler()
    handler.emit(logging.LogRecord("updater.database", logging.WARNING, "database.py", 1,
                                   "SQLite database is locked", (), None))

    assert handler.errors == {"SQLite error during polling"}


def test_no_successful_cpe_poll_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.check_events["192.0.2.2"] = [(60, "ok"), (660, "ok")]

    assert "no successful CPE poll" in _evaluate(recorder, set(), True)


def test_no_session_reuse_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.checks["192.0.2.2"]["ok"] = 0

    assert "no session reuse for cpe-1" in _evaluate(recorder, set(), True)


def test_no_relogin_after_age_limit_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.renewal_logins.discard("192.0.2.2")

    assert "no re-login after the 600-second login-age limit for cpe-1" in _evaluate(recorder, set(), True)


def test_no_reuse_after_relogin_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.post_renewal_reuse.discard("192.0.2.2")

    assert "no reuse after re-login for cpe-1" in _evaluate(recorder, set(), True)


def test_login_gap_shorter_than_age_limit_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.logins["192.0.2.2"] = [(0, True), (599, True)]
    recorder.login_triggers["192.0.2.2"] = [None, None]

    assert "login spacing too tight for cpe-1" in _evaluate(recorder, set(), True)
