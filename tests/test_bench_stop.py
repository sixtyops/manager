"""Mocked checks for the read-only firmware-stop bench mode."""

import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts/bench/readonly_stop.py"
DRIVER = '''
import os
from types import SimpleNamespace

class TachyonClient:
    def __init__(self, address, username, password):
        assert address == "192.0.2.10"
        assert username == "device-user"
        assert password == "PRIVATE_DEVICE_PASSWORD"

    async def login(self):
        print("PRIVATE_AP_LOGIN")
        return os.environ.get("BENCH_CASE") != "ap-login-fail"

    async def _curl(self, method, endpoint):
        with open(os.environ["BENCH_CONTACT_MARKER"], "a") as marker:
            marker.write(f"device:{method}:{endpoint}\\n")
        case = os.environ.get("BENCH_CASE")
        if case == "read-fail":
            raise RuntimeError("PRIVATE_DEVICE_EXCEPTION")
        if method != "GET":
            raise RuntimeError("PRIVATE_DEVICE_WRITE")
        return 200, "PRIVATE_DEVICE_BODY"

    async def get_device_info(self):
        for endpoint in ("/cgi.lua/status?type=system", "/cgi.lua/bootbank", "/cgi.lua/status?type=interfaces"):
            await self._curl("GET", endpoint)
        case = os.environ.get("BENCH_CASE")
        serial = None if case == "missing-ap-serial" else "SYNTHETIC-SERIAL" if case == "match" else "OTHER-SERIAL"
        return SimpleNamespace(serial=serial)
'''
HTTPX = '''
import json
import os
import ssl

class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body == "invalid-json":
            raise ValueError("PRIVATE_JSON_EXCEPTION")
        return self._body

class Request:
    def __init__(self, method):
        self.method = method
        self.headers = {}

class Client:
    def __init__(self, **kwargs):
        assert kwargs["base_url"] == "https://sixtyops-dev.infra.treehouse.mn"
        self.cookies = {}
        self.hooks = kwargs["event_hooks"]["request"]
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert kwargs["verify"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["verify"].check_hostname is True
        assert kwargs["verify"].get_ca_certs()
        assert kwargs["follow_redirects"] is False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, path, data):
        assert data == {"username": "bench-user", "password": "PRIVATE_MANAGER_PASSWORD"}
        request = Request("POST")
        for hook in self.hooks:
            hook(request)
        with open(os.environ["BENCH_CONTACT_MARKER"], "a") as marker:
            marker.write(f"manager:POST:{path}:{request.headers.get('Origin', '')}\\n")
        print("PRIVATE_MANAGER_LOGIN")
        case = os.environ.get("BENCH_CASE")
        if case not in ("manager-login-fail", "external-login-redirect", "unknown-login-redirect"):
            self.cookies["session_id"] = "PRIVATE_SESSION"
            return Response(303, headers={"location": "/"})
        if case == "external-login-redirect":
            return Response(303, headers={"location": "https://other.example/"})
        if case == "unknown-login-redirect":
            return Response(303, headers={"location": "/login"})
        return Response(401)

    def get(self, path):
        with open(os.environ["BENCH_CONTACT_MARKER"], "a") as marker:
            marker.write(f"manager:GET:{path}\\n")
        print("PRIVATE_MANAGER_RESPONSE")
        case = os.environ.get("BENCH_CASE")
        if case == "api-fail":
            return Response(500)
        if case == "invalid-json":
            return Response(200, "invalid-json")
        if case == "missing-stop":
            rows = [{"id": 78, "expected_serial": "SYNTHETIC-SERIAL"}]
        elif case == "ambiguous-stop":
            rows = [{"id": 77, "expected_serial": "SYNTHETIC-SERIAL"}] * 2
        elif case == "missing-expected-serial":
            rows = [{"id": 77, "expected_serial": ""}]
        else:
            rows = [{"id": 77, "expected_serial": "SYNTHETIC-SERIAL"}]
        return Response(200, rows)
'''


@pytest.fixture
def bench_stop(tmp_path):
    repo = tmp_path / "target"
    (repo / "updater/vendors/tachyon").mkdir(parents=True)
    (repo / "updater/vendors/tachyon/client.py").write_text(DRIVER)
    (repo / "httpx.py").write_text(HTTPX)
    marker = tmp_path / "contact"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SIXTYOPS_TEST_URL": "https://sixtyops-dev.infra.treehouse.mn",
        "SIXTYOPS_TEST_CA_FILE": str(tmp_path / "manager-ca.pem"),
        "SIXTYOPS_TEST_USER": "bench-user",
        "SIXTYOPS_TEST_PASS": "PRIVATE_MANAGER_PASSWORD",
        "SIXTYOPS_BENCH_AP_IP": "192.0.2.10",
        "SIXTYOPS_BENCH_AP_USER": "device-user",
        "SIXTYOPS_BENCH_AP_PASS": "PRIVATE_DEVICE_PASSWORD",
        "BENCH_CONTACT_MARKER": str(marker),
    }
    shutil.copyfile(ssl.get_default_verify_paths().cafile, env["SIXTYOPS_TEST_CA_FILE"])
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, head, marker, env


def run_helper(bench_stop, case="match", stop_id="77"):
    repo, _, _, env = bench_stop
    return subprocess.run(
        [sys.executable, str(HELPER), "--repo", str(repo), "--checkout", str(repo), "--stop-id", stop_id],
        env={**env, "BENCH_CASE": case},
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_stop_check_matches_serial_with_authenticated_reads_only(bench_stop):
    result = run_helper(bench_stop)
    _, _, marker, _ = bench_stop

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "firmware-stop: manager_login: PASS",
        "firmware-stop: stop_selection: PASS",
        "firmware-stop: ap_login: PASS",
        "firmware-stop: serial_match: PASS",
    ]
    calls = marker.read_text().splitlines()
    assert calls[:2] == [
        "manager:POST:/login:https://sixtyops-dev.infra.treehouse.mn",
        "manager:GET:/api/firmware-window-stops",
    ]
    assert calls[2:] == [
        "device:GET:/cgi.lua/status?type=system",
        "device:GET:/cgi.lua/bootbank",
        "device:GET:/cgi.lua/status?type=interfaces",
    ]


@pytest.mark.parametrize("case", [
    "manager-login-fail", "external-login-redirect", "unknown-login-redirect",
    "api-fail", "invalid-json", "missing-stop",
    "ambiguous-stop", "missing-expected-serial", "ap-login-fail",
    "read-fail", "missing-ap-serial", "mismatch",
])
def test_stop_check_fails_closed_without_private_output(bench_stop, case):
    result = run_helper(bench_stop, case)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert all(line.startswith("firmware-stop: ") and line.endswith((": PASS", ": FAIL"))
               for line in result.stdout.splitlines())
    assert "PRIVATE" not in output
    assert "SYNTHETIC-SERIAL" not in output
    assert "OTHER-SERIAL" not in output


def test_helper_does_not_read_access_file_or_accept_serial_argument():
    source = HELPER.read_text()

    assert "SIXTYOPS_BENCH_ACCESS_FILE" not in source
    assert 'add_argument("--serial"' not in source


@pytest.mark.parametrize("ca_state", ["missing", "invalid"])
def test_manager_ca_failure_is_closed_before_contact(bench_stop, ca_state):
    repo, _, marker, env = bench_stop
    invalid_ca = Path(env["SIXTYOPS_TEST_CA_FILE"])
    if ca_state == "missing":
        invalid_ca.unlink()
    else:
        invalid_ca.write_text("not a certificate")

    result = subprocess.run(
        [sys.executable, str(HELPER), "--repo", str(repo), "--checkout", str(repo), "--stop-id", "77"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "manager_login: FAIL" in result.stdout
    assert not marker.exists()
    assert str(invalid_ca) not in result.stdout + result.stderr


def test_manager_ca_inside_checkout_is_rejected(bench_stop):
    repo, _, marker, env = bench_stop
    repo_ca = repo / "manager-ca.pem"
    shutil.copyfile(env["SIXTYOPS_TEST_CA_FILE"], repo_ca)

    result = subprocess.run(
        [sys.executable, str(HELPER), "--repo", str(repo), "--checkout", str(repo), "--stop-id", "77"],
        env={**env, "SIXTYOPS_TEST_CA_FILE": str(repo_ca)},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "manager_login: FAIL" in result.stdout
    assert not marker.exists()


def test_external_manager_login_redirect_does_not_forward_credentials(bench_stop):
    result = run_helper(bench_stop, "external-login-redirect")
    _, _, marker, _ = bench_stop

    assert result.returncode == 1
    assert marker.read_text().splitlines() == [
        "manager:POST:/login:https://sixtyops-dev.infra.treehouse.mn",
    ]
    assert "PRIVATE_MANAGER_PASSWORD" not in result.stdout + result.stderr


def test_wrapper_reads_private_access_file_and_dispatches_stop_id(bench_stop, tmp_path):
    repo, head, marker, _ = bench_stop
    access = tmp_path / "bench-access"
    access.write_text(
        "Manager URL: https://sixtyops-dev.infra.treehouse.mn\n"
        f"Manager CA file: {bench_stop[3]['SIXTYOPS_TEST_CA_FILE']}\n"
        "Manager username: bench-user\nManager password: PRIVATE_MANAGER_PASSWORD\n"
        "AP IP: 192.0.2.10\nShared device username: device-user\n"
        "Shared device password: PRIVATE_DEVICE_PASSWORD\n"
    )
    access.chmod(0o600)
    summary = tmp_path / "summary"
    summary.mkdir(mode=0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    date = bin_dir / "date"
    date.write_text("#!/bin/sh\nprintf '20261113T000000Z\\n'\n")
    date.chmod(0o700)
    result = subprocess.run(
        [str(REPO / "scripts/bench/run-readonly-bench.sh"), head,
         "--mode", "firmware-stop", "--stop-id", "77"],
        cwd=repo,
        env={
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "HOME": str(tmp_path),
            "SIXTYOPS_BENCH_ACCESS_FILE": str(access),
            "SIXTYOPS_BENCH_SUMMARY_DIR": str(summary),
            "SIXTYOPS_BENCH_PYTHON": sys.executable,
            "SIXTYOPS_TEST_URL": "https://wrong.example",
            "SIXTYOPS_TEST_CA_FILE": "/wrong/inherited-ca.pem",
            "SIXTYOPS_BENCH_AP_IP": "192.0.2.99",
            "BENCH_CONTACT_MARKER": str(marker),
            "BENCH_CASE": "match",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "PRIVATE_" not in result.stdout + result.stderr
    assert "SYNTHETIC-SERIAL" not in result.stdout + result.stderr
    assert "firmware-stop: serial_match: PASS" in result.stdout
    assert "firmware-stop: stop_selection: PASS" in result.stdout
    assert marker.read_text().splitlines()[:2] == [
        "manager:POST:/login:https://sixtyops-dev.infra.treehouse.mn",
        "manager:GET:/api/firmware-window-stops",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in summary.iterdir())


def test_missing_expected_serial_keeps_selection_failed(bench_stop):
    result = run_helper(bench_stop, "missing-expected-serial")

    assert result.returncode == 1
    assert "firmware-stop: stop_selection: FAIL" in result.stdout
