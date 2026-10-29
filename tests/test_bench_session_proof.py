"""Unit tests for the sanitized read-only bench session proof."""

import logging

from scripts.bench.session_proof import Recorder, RunErrorHandler, _evaluate


def _passing_recorder(monkeypatch):
    monkeypatch.setenv("SIXTYOPS_BENCH_AP_IP", "192.0.2.1")
    recorder = Recorder(0)
    recorder.order.extend(["192.0.2.1", "192.0.2.2"])
    recorder.logins["192.0.2.2"] = [(0, True), (603, True)]
    recorder.checks["192.0.2.2"]["ok"] = 2
    recorder.check_events["192.0.2.2"] = [(60, "ok"), (61, "poll_ok"), (660, "ok"), (661, "poll_ok")]
    return recorder


def test_recorded_summary_shape_passes(monkeypatch):
    recorder = _passing_recorder(monkeypatch)

    assert _evaluate(recorder, set(), has_session_valid=True) == []


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
    recorder.logins["192.0.2.2"] = [(0, True)]

    assert "no re-login after the 600-second login-age limit for cpe-1" in _evaluate(recorder, set(), True)


def test_no_reuse_after_relogin_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.check_events["192.0.2.2"] = [(60, "ok"), (61, "poll_ok"), (661, "poll_ok")]

    assert "no reuse after re-login for cpe-1" in _evaluate(recorder, set(), True)


def test_login_gap_shorter_than_age_limit_fails(monkeypatch):
    recorder = _passing_recorder(monkeypatch)
    recorder.logins["192.0.2.2"] = [(0, True), (599, True)]

    assert "login spacing too tight for cpe-1" in _evaluate(recorder, set(), True)
