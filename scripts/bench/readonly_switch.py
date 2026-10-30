"""Read one lab switch twice with the exact commit's driver.

Run through run-readonly-bench.sh --mode local-switch. The wrapper checks
private output before login. This reader emits only fixed result fields.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import sys
from pathlib import Path

READ_ENDPOINTS = (
    "/cgi.lua/status?type=system",
    "/cgi.lua/bootbank",
    "/cgi.lua/status?type=interfaces",
)
REQUIRED_ENV = (
    "SIXTYOPS_TEST_SWITCH_IP",
    "SIXTYOPS_BENCH_AP_USER",
    "SIXTYOPS_BENCH_AP_PASS",
)


async def _read(results: dict[str, bool]) -> None:
    from updater.vendors.tachyon.client import TachyonClient

    if any(not os.environ.get(name, "").strip() for name in REQUIRED_ENV):
        return
    address = str(ipaddress.ip_address(os.environ[REQUIRED_ENV[0]].strip()))
    client = TachyonClient(address, os.environ[REQUIRED_ENV[1]], os.environ[REQUIRED_ENV[2]])
    if await client.login() is not True:
        return
    results["login"] = True
    driver_get = client._curl
    calls: list[str] = []

    async def readonly_get(method: str, endpoint: str, *args, **kwargs):
        if method != "GET" or endpoint not in READ_ENDPOINTS or args or kwargs:
            raise ValueError("disallowed request")
        status, body = await driver_get(method, endpoint)
        if status != 200:
            raise ValueError("failed read")
        calls.append(endpoint)
        return status, body

    # Keep the target driver's parser and transport. Admit fixed GETs only.
    client._curl = readonly_get
    versions = []
    for number in (1, 2):
        calls.clear()
        info = await client.get_device_info()
        valid = (
            calls == list(READ_ENDPOINTS)
            and isinstance(info.model, str)
            and info.model.strip().lower() == "tns-100"
            and isinstance(info.current_version, str)
            and bool(info.current_version.strip())
        )
        results[f"read_{number}"] = valid
        if not valid:
            return
        versions.append((info.current_version, info.active_bank, info.bank1_version, info.bank2_version))
    results["firmware_stable"] = versions[0] == versions[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    results = dict.fromkeys(("login", "read_1", "read_2", "firmware_stable"), False)
    # Driver imports, logs, and failures must not expose device data.
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            sys.path.insert(0, str(Path(args.repo).resolve()))
            asyncio.run(_read(results))
        except BaseException:
            pass
    for field, passed in results.items():
        print(f"local-switch: {field}: {'PASS' if passed else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
