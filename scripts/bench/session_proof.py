"""Count CPE logins and session reuse with the poller from the checked-out commit.

Run this only through run-readonly-bench.sh --mode session-proof. The wrapper
supplies the AP address and the device login in the environment.

The wrapper gives --repo as a fresh copy of the commit, so the database and
encryption key in its data/ directory are new. The script adds only the AP,
runs the commit's network poller for the set duration, and then prints one
line per device:

- login_ok and login_failed: calls to TachyonClient.login()
- reuse: session checks that kept a cached session (session_valid() == "ok")
- expired and unreachable: other session check results
- the time of each login

It does not start the web app, so it does not download firmware, check for
self-updates, or open the RADIUS port. Output uses labels, not addresses.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_ENV = ("SIXTYOPS_BENCH_AP_IP", "SIXTYOPS_BENCH_AP_USER", "SIXTYOPS_BENCH_AP_PASS")
POLL_INTERVAL_SECONDS = 30
SESSION_MAX_AGE_SECONDS = 600


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Recorder:
    """Record login and session-check calls per device IP."""

    def __init__(self, start: float, max_age_seconds: int | None = SESSION_MAX_AGE_SECONDS):
        self.start = start
        self.max_age_seconds = max_age_seconds
        self.logins: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        self.login_triggers: dict[str, list[str | None]] = defaultdict(list)
        self.pending_login_trigger: dict[str, str] = {}
        self.checks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.check_events: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self.link_uptime: dict[str, list[int | None]] = defaultdict(list)
        self.expected_cpes: set[str] = set()
        self.dropped_cpes: set[str] = set()
        self.cpe_poll_count = 0
        self.aged_cpes: set[str] = set()
        self.renewal_logins: set[str] = set()
        self.post_renewal_reuse: set[str] = set()
        self.logout_succeeded: set[str] = set()
        self.logout_failed: set[str] = set()
        self.post_logout_logins: set[str] = set()
        self.post_logout_poll_floor: dict[str, int] = {}
        self.post_logout_reuse: set[str] = set()
        self.bench_opened_sessions: dict[str, list[object]] = defaultdict(list)
        self.order: list[str] = []

    def _seen(self, ip: str):
        if ip not in self.order:
            self.order.append(ip)

    def login(self, ip: str, ok: bool, client=None):
        self._seen(ip)
        self.logins[ip].append((time.time(), ok))
        trigger = self.pending_login_trigger.pop(ip, None)
        self.login_triggers[ip].append(trigger)
        if ok and client is not None:
            self.bench_opened_sessions[ip].append(client)
        if ok and trigger == "age":
            self.renewal_logins.add(ip)
        if ok and trigger == "logout":
            self.post_logout_logins.add(ip)
            self.post_logout_poll_floor[ip] = self.cpe_poll_count

    def check(self, ip: str, state: str):
        self._seen(ip)
        self.checks[ip][state] += 1
        self.check_events[ip].append((time.time(), state))

    def cpe_poll(self, cpes):
        """Record the wireless uptime values from one successful AP poll."""
        self.cpe_poll_count += 1
        current = {}
        for cpe in cpes:
            ip = cpe.get("ip") if isinstance(cpe, dict) else getattr(cpe, "ip", None)
            uptime = cpe.get("link_uptime") if isinstance(cpe, dict) else getattr(cpe, "link_uptime", None)
            if not ip:
                continue
            try:
                uptime = int(uptime) if uptime is not None else None
            except (TypeError, ValueError):
                uptime = None
            if uptime is not None and uptime < 0:
                uptime = None
            current[ip] = uptime
            self.expected_cpes.add(ip)
            self._seen(ip)

        self.dropped_cpes.update(set(self.link_uptime) - set(current))
        for ip, uptime in current.items():
            self.link_uptime[ip].append(uptime)

    def owns_session(self, ip: str, client) -> bool:
        return any(opened is client for opened in self.bench_opened_sessions.get(ip, []))


def _uptime_reset_count(samples: list[int | None]) -> int:
    values = [value for value in samples if value is not None]
    return sum(current < previous for previous, current in zip(values, values[1:]))


def _age_cached_cpe_login(poller, cpe_ip: str, max_age_seconds: int = SESSION_MAX_AGE_SECONDS) -> bool:
    """Age one live cache entry in this bench process, without changing production code."""
    entry = poller._cpe_clients.get(cpe_ip)
    if not entry:
        return False
    client, _ = entry
    poller._cpe_clients[cpe_ip] = (client, time.time() - max_age_seconds - 1)
    return True


async def _logout_cpe_session(recorder: Recorder, ip: str, client) -> bool:
    """Invalidate this bench-owned cached token so the next poll logs in again."""
    # Cache entries can contain a raw client or a vendor driver, with or
    # without a timestamp. Only mutate the client whose login we recorded.
    if isinstance(client, tuple):
        client = client[0]
    client = getattr(client, "_client", client)
    if not recorder.owns_session(ip, client):
        return False
    if not getattr(client, "_token", None):
        return False
    client._token = None
    return True


class RunErrorHandler(logging.Handler):
    """Remember poll and SQLite failures without retaining their messages."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.errors: set[str] = set()

    def emit(self, record):
        try:
            message = record.getMessage().lower()
            exception = record.exc_info[1] if record.exc_info else None
            if isinstance(exception, sqlite3.Error) or "sqlite" in message or "database is locked" in message:
                self.errors.add("SQLite error during polling")
            if record.name == "updater.poller" and record.levelno >= logging.ERROR:
                self.errors.add("poll error during run")
        except Exception:
            self.errors.add("poll error during run")


def _labels(order: list[str]) -> dict[str, str]:
    named = {
        os.environ.get("SIXTYOPS_BENCH_AP_IP", "").strip(): "AP",
        os.environ.get("SIXTYOPS_BENCH_SM_303L_IP", "").strip(): "303L-SM",
        os.environ.get("SIXTYOPS_BENCH_SM_303X_IP", "").strip(): "303X-SM",
    }
    named.pop("", None)
    labels, n = {}, 0
    for ip in order:
        if ip in named:
            labels[ip] = named[ip]
        else:
            n += 1
            labels[ip] = f"cpe-{n}"
    return labels


def _evaluate(recorder: Recorder, errors: set[str], has_session_valid: bool) -> list[str]:
    """Return fixed, safe failure reasons for the recorded CPE session events."""
    failures = sorted(errors)
    cpe_ips = [ip for ip in recorder.order if ip in recorder.expected_cpes]
    if not cpe_ips:
        failures.append("no CPE was probed")
        return failures

    if not any(any(state == "poll_ok" for _, state in recorder.check_events.get(ip, [])) for ip in cpe_ips):
        failures.append("no successful CPE poll")

    labels = _labels(recorder.order)
    for ip in cpe_ips:
        label = labels[ip]
        login_events = recorder.logins.get(ip, [])
        login_gaps = [b[0] - a[0] for a, b in zip(login_events, login_events[1:])]
        triggers = recorder.login_triggers.get(ip, [])
        if recorder.max_age_seconds is not None and any(
            gap < recorder.max_age_seconds and triggers[index + 1] is None
            for index, gap in enumerate(login_gaps)
        ):
            failures.append(f"login spacing too tight for {label}")
        if not has_session_valid or recorder.checks.get(ip, {}).get("ok", 0) == 0:
            failures.append(f"no session reuse for {label}")
        if recorder.max_age_seconds is not None:
            if ip not in recorder.aged_cpes:
                failures.append(f"cached CPE login was not aged for {label}")
            if ip not in recorder.renewal_logins:
                failures.append(f"no re-login after the {recorder.max_age_seconds}-second login-age limit for {label}")
            if ip not in recorder.post_renewal_reuse:
                failures.append(f"no reuse after re-login for {label}")
        if ip not in recorder.logout_succeeded:
            failures.append(f"CPE session logout failed for {label}")
        if ip not in recorder.post_logout_logins:
            failures.append(f"no re-login after CPE logout for {label}")
        if ip not in recorder.post_logout_reuse:
            failures.append(f"polling did not continue after CPE logout for {label}")
        samples = recorder.link_uptime.get(ip, [])
        if not samples or any(value is None for value in samples):
            failures.append(f"missing wireless link uptime for {label}")
        if _uptime_reset_count(samples):
            failures.append(f"wireless link uptime reset for {label}")
        if ip in recorder.dropped_cpes:
            failures.append(f"CPE dropped off AP for {label}")
    return failures


def _cpe_session_max_age(poller_module) -> int | None:
    """Require a declared policy or the legacy ten-minute cap marker."""
    policy = getattr(poller_module, "CPE_SESSION_POLICY", None)
    cap = getattr(poller_module, "_CLIENT_TTL_SECONDS", None)
    legacy_cap = type(cap) is int and cap == SESSION_MAX_AGE_SECONDS
    if policy == "login_age" or (not hasattr(poller_module, "CPE_SESSION_POLICY") and legacy_cap):
        if legacy_cap:
            return cap
        raise ValueError("capped CPE session policy lacks the 600-second cap marker")
    if policy == "credential_change":
        if hasattr(poller_module, "_CLIENT_TTL_SECONDS"):
            raise ValueError("conflicting CPE session policy markers")
        return None
    raise ValueError("missing or unknown CPE session policy marker")


def _instrument(recorder: Recorder):
    from updater import poller as poller_module
    from updater.poller import NetworkPoller
    from updater.vendors.tachyon.client import TachyonClient

    recorder.max_age_seconds = _cpe_session_max_age(poller_module)

    original_login = TachyonClient.login

    async def login(self):
        result = await original_login(self)
        recorder.login(self.ip, result is True, client=self)
        return result

    TachyonClient.login = login

    # session_valid() exists only on commits with CPE session reuse.
    original_valid = getattr(TachyonClient, "session_valid", None)
    if original_valid is not None:
        async def session_valid(self):
            state = await original_valid(self)
            recorder.check(self.ip, state)
            if state == "ok":
                ready_for_logout = recorder.max_age_seconds is None
                if self.ip in recorder.renewal_logins:
                    recorder.post_renewal_reuse.add(self.ip)
                    ready_for_logout = True
                if (
                    ready_for_logout
                    and self.ip in recorder.expected_cpes
                    and self.ip not in recorder.logout_succeeded
                    and self.ip not in recorder.logout_failed
                ):
                    if await _logout_cpe_session(recorder, self.ip, self):
                        recorder.logout_succeeded.add(self.ip)
                        recorder.pending_login_trigger[self.ip] = "logout"
                    else:
                        recorder.logout_failed.add(self.ip)
                elif (
                    self.ip in recorder.post_logout_logins
                    and recorder.cpe_poll_count > recorder.post_logout_poll_floor.get(self.ip, recorder.cpe_poll_count)
                ):
                    recorder.post_logout_reuse.add(self.ip)
            return state

        TachyonClient.session_valid = session_valid

    original_check_cpe_auth = NetworkPoller._check_cpe_auth
    original_get_connected_cpes = TachyonClient.get_connected_cpes

    async def get_connected_cpes(self):
        cpes = await original_get_connected_cpes(self)
        recorder.cpe_poll(cpes)
        return cpes

    TachyonClient.get_connected_cpes = get_connected_cpes

    async def check_cpe_auth(self, cpe_ip, username, password, *args, **kwargs):
        login_count = len(recorder.logins.get(cpe_ip, []))
        state = await original_check_cpe_auth(self, cpe_ip, username, password, *args, **kwargs)
        if state == "ok":
            recorder.check(cpe_ip, "poll_ok")
            logged_in_during_poll = len(recorder.logins.get(cpe_ip, [])) > login_count
            if (recorder.max_age_seconds is not None
                    and not logged_in_during_poll and cpe_ip not in recorder.aged_cpes):
                if _age_cached_cpe_login(self, cpe_ip, recorder.max_age_seconds):
                    recorder.aged_cpes.add(cpe_ip)
                    recorder.pending_login_trigger[cpe_ip] = "age"
        return state

    NetworkPoller._check_cpe_auth = check_cpe_auth
    return original_valid is not None


def _report(recorder: Recorder, end: float, has_session_valid: bool):
    labels = _labels(recorder.order)
    print(f"session-proof: started={_utc(recorder.start)} finished={_utc(end)} "
          f"duration_s={int(end - recorder.start)} poll_interval_s={POLL_INTERVAL_SECONDS}")
    print(f"session-proof: commit_has_session_valid={'yes' if has_session_valid else 'no'}")
    print(f"session-proof: cpe_session_max_age_s={recorder.max_age_seconds if recorder.max_age_seconds is not None else 'none'}")
    print(f"session-proof: devices_seen={len(recorder.order)}")
    for ip in recorder.order:
        logins = recorder.logins.get(ip, [])
        checks = recorder.checks.get(ip, {})
        triggers = recorder.login_triggers.get(ip, [])
        successful = [
            (ts, trigger)
            for (ts, ok), trigger in zip(logins, triggers)
            if ok
        ]
        ok_times = [ts for ts, _ in successful]
        gaps = [
            int(successful[index + 1][0] - successful[index][0])
            for index in range(len(successful) - 1)
            if successful[index + 1][1] is None
        ]
        print(
            f"session-proof: device={labels[ip]} "
            f"login_ok={len(ok_times)} login_failed={len(logins) - len(ok_times)} "
            f"reuse={checks.get('ok', 0)} expired={checks.get('expired', 0)} "
            f"unreachable={checks.get('unreachable', 0)} "
            f"min_gap_between_logins_s={min(gaps) if gaps else 'n/a'}"
        )
        for i, (ts, ok) in enumerate(logins, 1):
            print(
                f"session-proof: login device={labels[ip]} n={i} utc={_utc(ts)} "
                f"t_plus_s={int(ts - recorder.start)} result={'ok' if ok else 'failed'}"
            )
        if ip != os.environ.get("SIXTYOPS_BENCH_AP_IP", "").strip():
            uptimes = recorder.link_uptime.get(ip, [])
            valid_uptimes = [value for value in uptimes if value is not None]
            print(
                f"session-proof: link device={labels[ip]} "
                f"uptime_start_s={uptimes[0] if uptimes and uptimes[0] is not None else 'n/a'} "
                f"uptime_end_s={uptimes[-1] if uptimes and uptimes[-1] is not None else 'n/a'} "
                f"resets={_uptime_reset_count(valid_uptimes)}"
            )
            renewal = 'yes' if ip in recorder.renewal_logins else 'no'
            renewal_reuse = 'yes' if ip in recorder.post_renewal_reuse else 'no'
            if recorder.max_age_seconds is None:
                renewal = renewal_reuse = 'not_applicable'
            print(
                f"session-proof: recovery device={labels[ip]} "
                f"forced_renewal={renewal} "
                f"reuse_after_forced_renewal={renewal_reuse} "
                f"logout_recovery={'yes' if ip in recorder.post_logout_logins else 'no'} "
                f"reuse_after_logout_recovery={'yes' if ip in recorder.post_logout_reuse else 'no'}"
            )


async def _run(duration_s: int) -> int:
    from updater import database as db
    from updater.poller import init_poller
    from updater.vendors import init_vendors

    ap_ip = os.environ["SIXTYOPS_BENCH_AP_IP"].strip()
    # A fresh database has no config templates and auto-enforce off, so the
    # poller does not push config. Stop if that is not true.
    if db.get_setting("config_auto_enforce", "false") == "true" or db.get_devices():
        print("session-proof: FAIL: the database is not fresh")
        return 1
    init_vendors()
    recorder = Recorder(time.time())
    try:
        has_session_valid = _instrument(recorder)
    except ValueError as error:
        print(f"session-proof: FAIL: {error}")
        return 1
    error_handler = RunErrorHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(error_handler)

    db.upsert_access_point(ap_ip, os.environ["SIXTYOPS_BENCH_AP_USER"], os.environ["SIXTYOPS_BENCH_AP_PASS"])

    poller = init_poller(None, poll_interval=POLL_INTERVAL_SECONDS)
    print(f"session-proof: polling for {duration_s}s")
    sys.stdout.flush()
    await poller.start()
    try:
        await asyncio.sleep(duration_s)
    finally:
        await poller.stop()
        root_logger.removeHandler(error_handler)

    _report(recorder, time.time(), has_session_valid)
    failures = _evaluate(recorder, error_handler.errors, has_session_valid)
    if failures:
        for failure in failures:
            print(f"session-proof: FAIL: {failure}")
        return 1
    print("session-proof: DONE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="checkout under test")
    parser.add_argument("--duration-min", type=int, default=3)
    args = parser.parse_args()

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        print(f"session-proof: FAIL: missing variables: {', '.join(missing)}")
        return 1
    if args.duration_min < 1:
        print("session-proof: FAIL: --duration-min must be 1 or more")
        return 1

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    sys.path.insert(0, str(Path(args.repo).resolve()))
    return asyncio.run(_run(args.duration_min * 60))


if __name__ == "__main__":
    sys.exit(main())
