"""Offline checks for exact-head switch reads and private output guards."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts/bench/run-readonly-bench.sh"
STAMP = "20261015T000000Z"
DRIVER = '''import json
import os
from pathlib import Path
from types import SimpleNamespace

class TachyonClient:
    def __init__(self, address, username, password):
        self.number = 0

    async def login(self):
        output = Path(os.environ["SIXTYOPS_BENCH_SUMMARY_DIR"])
        files = list(output.iterdir())
        assert len(files) == 2
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
        Path(os.environ["BENCH_CONTACT_MARKER"]).write_text("login\\n")
        print("PRIVATE_LOGIN_ADDRESS_AND_TOKEN")
        if os.environ.get("BENCH_CASE") == "login-fail":
            return "PRIVATE_LOGIN_ERROR"
        return True

    async def _curl(self, method, endpoint):
        with open(os.environ["BENCH_CONTACT_MARKER"], "a") as marker:
            marker.write(json.dumps([method, endpoint]) + "\\n")
        return (500 if os.environ.get("BENCH_CASE") == "http-fail" else 200), "PRIVATE_RESPONSE"

    async def get_device_info(self):
        self.number += 1
        case = os.environ.get("BENCH_CASE")
        if case == "exception":
            raise RuntimeError("PRIVATE_EXCEPTION_SERIAL_MAC")
        if case == "write":
            await self._curl("POST", "/reboot")
        if case == "wrong-get":
            await self._curl("GET", "/cgi.lua/config")
        for endpoint in ("/cgi.lua/status?type=system", "/cgi.lua/bootbank", "/cgi.lua/status?type=interfaces"):
            await self._curl("GET", endpoint)
        return SimpleNamespace(
            model="PRIVATE_MODEL" if case == "model" else "TNS-100",
            current_version="" if case == "empty-version" else str(self.number if case == "unstable" else 1),
            active_bank=1, bank1_version="PRIVATE_VERSION", bank2_version="PRIVATE_BACKUP_VERSION")
'''


@pytest.fixture
def bench(tmp_path):
    repo = tmp_path / "repo"
    driver = repo / "updater/vendors/tachyon/client.py"
    driver.parent.mkdir(parents=True)
    driver.write_text(DRIVER)
    base_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(command, cwd=repo, env=base_env, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    access = tmp_path / "access"
    access.write_text("TNS-100 IP: 192.0.2.12\nShared device username: bench-user\nShared device password: bench-password\n")
    access.chmod(0o600)
    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    marker = tmp_path / "contact"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    date = bin_dir / "date"
    date.write_text(f"#!/bin/sh\nprintf '%s\\n' {STAMP}\n")
    date.chmod(0o700)
    env = {**base_env, "PATH": str(bin_dir) + os.pathsep + base_env["PATH"],
           "SIXTYOPS_BENCH_ACCESS_FILE": str(access),
           "SIXTYOPS_BENCH_SUMMARY_DIR": str(output),
           "SIXTYOPS_BENCH_PYTHON": sys.executable,
           "BENCH_CONTACT_MARKER": str(marker)}
    return repo, head, access, output, marker, env


def run_bench(bench, *, head=None, mode="local-switch"):
    repo, actual, _, _, _, env = bench
    return subprocess.run([str(WRAPPER), head or actual, "--mode", mode],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=30)


def test_switch_uses_target_driver_twice_with_only_fixed_gets(bench):
    result = run_bench(bench)
    _, head, _, output, marker, _ = bench
    assert result.returncode == 0, result.stderr
    contacts = marker.read_text().splitlines()
    assert contacts[0] == "login"
    expected = [["GET", endpoint] for endpoint in (
        "/cgi.lua/status?type=system", "/cgi.lua/bootbank", "/cgi.lua/status?type=interfaces")]
    assert [json.loads(line) for line in contacts[1:]] == expected * 2
    expected_log = "".join(f"local-switch: {field}: PASS\n" for field in
                           ("login", "read_1", "read_2", "firmware_stable"))
    assert result.stdout == expected_log
    assert next(output.glob("*.log")).read_text() == expected_log
    assert next(output.glob("*.summary.txt")).read_text() == f"commit: {head}\n{expected_log}overall: PASS\n"
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir())


@pytest.mark.parametrize("failure", ["wrong-head", "dirty", "missing-input", "bad-access", "access-symlink",
                                     "unsafe-directory", "log-collision", "summary-collision", "bad-mode"])
def test_switch_refuses_before_device_contact(bench, failure):
    repo, head, access, output, marker, env = bench
    requested = head
    mode = "local-switch"
    if failure == "wrong-head":
        requested = "0" * 40
    elif failure == "dirty":
        (repo / "untracked").write_text("dirty")
    elif failure == "missing-input":
        access.write_text("Shared device username: bench-user\nShared device password: bench-password\n")
        env["SIXTYOPS_TEST_SWITCH_IP"] = "192.0.2.99"
    elif failure == "bad-access":
        access.chmod(0o644)
    elif failure == "access-symlink":
        link = access.with_name("access-link")
        link.symlink_to(access)
        env["SIXTYOPS_BENCH_ACCESS_FILE"] = str(link)
    elif failure == "unsafe-directory":
        output.chmod(0o755)
    elif failure.endswith("collision"):
        suffix = "log" if failure == "log-collision" else "summary.txt"
        (output / f"{STAMP}-{head[:12]}-local-switch.{suffix}").mkdir()
    elif failure == "bad-mode":
        mode = "switch-write"
    result = run_bench(bench, head=requested, mode=mode)
    assert result.returncode != 0
    assert not marker.exists()
    assert "bench-password" not in result.stdout + result.stderr


@pytest.mark.parametrize("case", ["login-fail", "exception", "model", "empty-version", "unstable", "write", "wrong-get", "http-fail"])
def test_switch_fails_closed_without_private_output(bench, case):
    _, _, _, output, marker, env = bench
    env["BENCH_CASE"] = case
    result = run_bench(bench)
    assert result.returncode == 1
    text = result.stdout + result.stderr + "".join(p.read_text() for p in output.iterdir())
    assert "PRIVATE" not in text
    assert "bench-password" not in text
    assert "overall: FAIL" in text
    assert all(line.startswith("local-switch: ") and line.endswith((": PASS", ": FAIL"))
               for line in result.stdout.splitlines())
    contacts = marker.read_text().splitlines()[1:]
    assert all(json.loads(line)[0] == "GET" for line in contacts)
    assert all(json.loads(line)[1] != "/cgi.lua/config" for line in contacts)


def test_switch_rejects_log_write_failure_after_preflight(bench):
    _, _, _, output, marker, env = bench
    tee = Path(env["PATH"].split(os.pathsep)[0]) / "tee"
    tee.write_text("#!/bin/sh\ncat\nexit 1\n")
    tee.chmod(0o700)

    result = run_bench(bench)

    # The fake driver checks the reserved files before its successful reads.
    assert marker.read_text().splitlines()[0] == "login"
    assert "local-switch: read_2: PASS" in result.stdout
    assert result.returncode == 1
    assert next(output.glob("*.log")).read_text() == ""
    summary = next(output.glob("*.summary.txt")).read_text()
    assert "overall: FAIL" in summary
    assert "overall: PASS" not in result.stdout + result.stderr + summary
