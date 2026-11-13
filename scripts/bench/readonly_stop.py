"""Check one unresolved firmware stop against its AP serial."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import stat
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_ENV = (
    "SIXTYOPS_TEST_URL",
    "SIXTYOPS_TEST_CA_FILE",
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
_WRITABLE_BITS = stat.S_IWGRP | stat.S_IWOTH
_STICKY_BIT = stat.S_ISVTX
_MAX_CA_BYTES = 16 * 1024 * 1024


def _check_ca_stat(info: os.stat_result, *, directory: bool) -> None:
    allowed_owners = {0, os.geteuid()}
    if info.st_uid not in allowed_owners:
        raise OSError("unsafe CA path owner")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("invalid CA path component")
        if info.st_mode & _WRITABLE_BITS and not (
            info.st_uid == 0 and info.st_mode & _STICKY_BIT
        ):
            raise OSError("unsafe CA path permissions")
    elif not stat.S_ISREG(info.st_mode) or info.st_mode & _WRITABLE_BITS:
        raise OSError("unsafe CA file permissions")


def _read_verified_ca(path: Path, checkout: Path) -> bytes:
    if not path.is_absolute() or not checkout.is_absolute():
        raise OSError("CA paths must be absolute")
    normalized = Path(os.path.normpath(path))
    repo_root = Path(os.path.realpath(checkout))
    if normalized == repo_root or repo_root in normalized.parents:
        raise OSError("CA file must be outside checkout")
    parts = normalized.parts[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("invalid CA path")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    dir_flags = flags | os.O_DIRECTORY
    descriptors = []
    try:
        current_fd = os.open("/", dir_flags)
        descriptors.append(current_fd)
        _check_ca_stat(os.fstat(current_fd), directory=True)
        for component in parts[:-1]:
            current_fd = os.open(component, dir_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
            _check_ca_stat(os.fstat(current_fd), directory=True)

        ca_fd = os.open(parts[-1], flags, dir_fd=current_fd)
        descriptors.append(ca_fd)
        before = os.fstat(ca_fd)
        _check_ca_stat(before, directory=False)
        chunks = []
        total = 0
        while True:
            chunk = os.read(ca_fd, min(65536, _MAX_CA_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CA_BYTES:
                raise OSError("CA file is too large")
        after = os.fstat(ca_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise OSError("CA file changed while reading")
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _manager_ssl_context(ca_path: Path, checkout: Path) -> ssl.SSLContext:
    ca_bytes = _read_verified_ca(ca_path, checkout)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=ca_bytes.decode("ascii"))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


async def _check(stop_id: int, results: dict[str, bool], checkout: Path) -> None:
    import httpx

    from updater.vendors.tachyon.client import TachyonClient

    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENV}
    if any(not value for value in values.values()):
        return
    manager_url = values["SIXTYOPS_TEST_URL"].rstrip("/")
    parsed_url = urlsplit(manager_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "sixtyops-dev.infra.treehouse.mn"
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        return
    ap_ip = str(ipaddress.ip_address(values["SIXTYOPS_BENCH_AP_IP"]))

    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    ssl_context = _manager_ssl_context(Path(values["SIXTYOPS_TEST_CA_FILE"]), checkout)

    def set_login_origin(request):
        if request.method == "POST":
            request.headers["Origin"] = origin

    with httpx.Client(
        base_url=manager_url,
        verify=ssl_context,
        timeout=30.0,
        follow_redirects=False,
        event_hooks={"request": [set_login_origin]},
    ) as session:
        login = session.post(
            "/login",
            data={"username": values["SIXTYOPS_TEST_USER"], "password": values["SIXTYOPS_TEST_PASS"]},
        )
        location = urlsplit(login.headers.get("location", ""))
        if (
            login.status_code != 303
            or location.scheme
            or location.netloc
            or location.path not in ("/", "/setup")
            or location.query
            or location.fragment
            or "session_id" not in session.cookies
        ):
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
    parser.add_argument("--checkout", required=True)
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
            asyncio.run(_check(args.stop_id, results, Path(args.checkout)))
        except BaseException:
            pass
    _print_results(results)
    return 0 if all(results.values()) else 1


def _print_results(results: dict[str, bool]) -> None:
    for field, passed in results.items():
        print(f"firmware-stop: {field}: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
