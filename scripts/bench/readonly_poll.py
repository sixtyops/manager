"""Read-only poll of one lab AP with the driver from the checked-out commit.

Run this only through run-readonly-bench.sh. The wrapper supplies the AP
address and login in the environment. This script logs in, reads device
information and the connected CPE list, and prints a short summary.

It does not change the device. It does not print the address, the login,
the serial number, or MAC addresses.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REQUIRED_ENV = ("SIXTYOPS_BENCH_AP_IP", "SIXTYOPS_BENCH_AP_USER", "SIXTYOPS_BENCH_AP_PASS")


async def _poll() -> int:
    from updater.vendors.tachyon.client import TachyonClient

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        print(f"local-poll: SKIP: missing variables: {', '.join(missing)}")
        return 0

    client = TachyonClient(
        os.environ["SIXTYOPS_BENCH_AP_IP"].strip(),
        os.environ["SIXTYOPS_BENCH_AP_USER"],
        os.environ["SIXTYOPS_BENCH_AP_PASS"],
    )

    # login() returns True on success and an error string on failure.
    result = await client.login()
    if result is not True:
        print(f"local-poll: FAIL: login: {result}")
        return 1

    info = await client.get_device_info()
    cpes = await client.get_connected_cpes()

    print(f"local-poll: model={info.model or 'unknown'}")
    print(f"local-poll: current_version={info.current_version or 'unknown'}")
    print(f"local-poll: active_bank={info.active_bank if info.active_bank is not None else 'unknown'}")
    print(f"local-poll: bank1_version={info.bank1_version or 'unknown'}")
    print(f"local-poll: bank2_version={info.bank2_version or 'unknown'}")
    print(f"local-poll: connected_cpes={len(cpes)}")

    if not info.model or not info.current_version:
        print("local-poll: FAIL: device information is incomplete")
        return 1
    print("local-poll: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="checkout under test")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    return asyncio.run(_poll())


if __name__ == "__main__":
    sys.exit(main())
