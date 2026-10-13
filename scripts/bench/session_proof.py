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
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_ENV = ("SIXTYOPS_BENCH_AP_IP", "SIXTYOPS_BENCH_AP_USER", "SIXTYOPS_BENCH_AP_PASS")
POLL_INTERVAL_SECONDS = 60


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Recorder:
    """Record login and session-check calls per device IP."""

    def __init__(self, start: float):
        self.start = start
        self.logins: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        self.checks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.order: list[str] = []

    def _seen(self, ip: str):
        if ip not in self.order:
            self.order.append(ip)

    def login(self, ip: str, ok: bool):
        self._seen(ip)
        self.logins[ip].append((time.time(), ok))

    def check(self, ip: str, state: str):
        self._seen(ip)
        self.checks[ip][state] += 1


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


def _instrument(recorder: Recorder):
    from updater.vendors.tachyon.client import TachyonClient

    original_login = TachyonClient.login

    async def login(self):
        result = await original_login(self)
        recorder.login(self.ip, result is True)
        return result

    TachyonClient.login = login

    # session_valid() exists only on commits with CPE session reuse.
    original_valid = getattr(TachyonClient, "session_valid", None)
    if original_valid is not None:
        async def session_valid(self):
            state = await original_valid(self)
            recorder.check(self.ip, state)
            return state

        TachyonClient.session_valid = session_valid
    return original_valid is not None


def _report(recorder: Recorder, end: float, has_session_valid: bool):
    labels = _labels(recorder.order)
    print(f"session-proof: started={_utc(recorder.start)} finished={_utc(end)} "
          f"duration_s={int(end - recorder.start)} poll_interval_s={POLL_INTERVAL_SECONDS}")
    print(f"session-proof: commit_has_session_valid={'yes' if has_session_valid else 'no'}")
    print(f"session-proof: devices_seen={len(recorder.order)}")
    for ip in recorder.order:
        logins = recorder.logins.get(ip, [])
        checks = recorder.checks.get(ip, {})
        ok_times = [ts for ts, ok in logins if ok]
        gaps = [int(b - a) for a, b in zip(ok_times, ok_times[1:])]
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
    has_session_valid = _instrument(recorder)

    db.upsert_access_point(ap_ip, os.environ["SIXTYOPS_BENCH_AP_USER"], os.environ["SIXTYOPS_BENCH_AP_PASS"])

    poller = init_poller(None, poll_interval=POLL_INTERVAL_SECONDS)
    print(f"session-proof: polling for {duration_s}s")
    sys.stdout.flush()
    await poller.start()
    try:
        await asyncio.sleep(duration_s)
    finally:
        await poller.stop()

    _report(recorder, time.time(), has_session_valid)
    if not [ip for ip in recorder.order if ip != ap_ip]:
        print("session-proof: FAIL: no CPE was probed")
        return 1
    print("session-proof: DONE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="checkout under test")
    parser.add_argument("--duration-min", type=int, default=35)
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
