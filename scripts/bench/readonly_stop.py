"""Check one unresolved firmware stop against its AP serial."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_ENV = (
    "SIXTYOPS_TEST_URL",
    "SIXTYOPS_TEST_USER",
    "SIXTYOPS_TEST_PASS",
    "SIXTYOPS_BENCH_AP_IP",
    "SIXTYOPS_BENCH_AP_USER",
    "SIXTYOPS_BENCH_AP_PASS",
)
DEVICE_GETS = {
    "/cgi.lua/status?type=system",
    "/cgi.lua/bootbank",
    "/cgi.lua/status?type=interfaces",
}


async def _check(stop_id: int, results: dict[str, bool]) -> None:
    import httpx

    from updater.vendors.tachyon.client import TachyonClient

    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENV}
    if any(not value for value in values.values()):
        return
    manager_url = values["SIXTYOPS_TEST_URL"].rstrip("/")
    parsed_url = urlsplit(manager_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password:
        return
    ap_ip = str(ipaddress.ip_address(values["SIXTYOPS_BENCH_AP_IP"]))

    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    def set_login_origin(request):
        if request.method == "POST":
            request.headers["Origin"] = origin

    with httpx.Client(
        base_url=manager_url,
        verify=ssl_context,
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"request": [set_login_origin]},
    ) as session:
        login = session.post(
            "/login",
            data={"username": values["SIXTYOPS_TEST_USER"], "password": values["SIXTYOPS_TEST_PASS"]},
        )
        if login.status_code != 200 or "session_id" not in session.cookies:
            return
        results["manager_login"] = True

        response = session.get("/api/firmware-window-stops")
        if response.status_code != 200:
            return
        rows = response.json()
    if not isinstance(rows, list):
        return
    selected = [
        row for row in rows
        if isinstance(row, dict) and type(row.get("id")) is int and row["id"] == stop_id
    ]
    if len(selected) != 1:
        return
    expected_serial = selected[0].get("expected_serial")
    if not isinstance(expected_serial, str) or not expected_serial.strip():
        results["stop_selection"] = True
        return
    expected_serial = expected_serial.strip()
    results["stop_selection"] = True

    client = TachyonClient(ap_ip, values["SIXTYOPS_BENCH_AP_USER"], values["SIXTYOPS_BENCH_AP_PASS"])
    if await client.login() is not True:
        return
    results["ap_login"] = True
    driver_get = client._curl

    async def readonly_get(method: str, endpoint: str, *args, **kwargs):
        if method != "GET" or endpoint not in DEVICE_GETS or args or kwargs:
            raise ValueError("disallowed request")
        status, body = await driver_get(method, endpoint)
        if status != 200:
            raise ValueError("failed read")
        return status, body

    client._curl = readonly_get
    info = await client.get_device_info()
    actual_serial = getattr(info, "serial", None)
    if not isinstance(actual_serial, str) or not actual_serial.strip():
        return
    results["serial_match"] = actual_serial.strip() == expected_serial


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--stop-id", required=True, type=int)
    args = parser.parse_args()
    results = dict.fromkeys(("manager_login", "stop_selection", "ap_login", "serial_match"), False)
    if args.stop_id <= 0:
        _print_results(results)
        return 1
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            sys.path.insert(0, str(Path(args.repo).resolve()))
            asyncio.run(_check(args.stop_id, results))
        except BaseException:
            pass
    _print_results(results)
    return 0 if all(results.values()) else 1


def _print_results(results: dict[str, bool]) -> None:
    for field, passed in results.items():
        print(f"firmware-stop: {field}: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
