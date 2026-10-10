"""Static and refusal checks for the read-only bench wrapper.

These tests never load real credentials and never contact a device.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "bench" / "run-readonly-bench.sh"

# Integration files that change devices or Manager state. None of them may
# be in the wrapper allowlist.
WRITE_TESTS = {
    "tests/integration/test_firmware.py",
    "tests/integration/test_config_push.py",
    "tests/integration/test_config_backup_restore.py",
    "tests/integration/test_radius_live.py",
    "tests/integration/test_live_crud.py",
    "tests/integration/test_sso_live.py",
    "tests/integration/test_manager_backup.py",
}

# The only POST targets an allowlisted test may call. Each starts a poll
# that the Manager scheduler also runs.
ALLOWED_POSTS = {
    "/api/aps/{ip}/poll",
    "/api/switches/{ip}/poll",
    "/api/configs/{ip}/poll",
    "/api/topology/refresh",
}

PORTAL_TEST = "tests/integration/test_system_surface.py::test_device_portal_surfaces_ap_switch_and_cpe"


def _bash_array(name):
    source = WRAPPER.read_text()
    match = re.search(rf"^{name}=\(\n(.*?)^\)$", source, re.MULTILINE | re.DOTALL)
    assert match, f"{name} array not found in the wrapper"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def _run(sha, tmp_path):
    marker = tmp_path / "sourced"
    access = tmp_path / "access.env"
    access.write_text(f"Manager password: do-not-print\n$(touch '{marker}')\n")
    access.chmod(0o600)
    result = subprocess.run(
        [str(WRAPPER), sha],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path),
             "SIXTYOPS_BENCH_ACCESS_FILE": str(access),
             "SIXTYOPS_BENCH_SUMMARY_DIR": str(tmp_path / "out")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, marker, access


def test_wrapper_refuses_wrong_sha_before_loading_access_file(tmp_path):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    wrong = head[:-1] + ("0" if head[-1] != "0" else "1")

    result, marker, access = _run(wrong, tmp_path)

    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert f"not {wrong}" in result.stderr
    assert not marker.exists(), "the access file was loaded before the SHA check"
    output = result.stdout + result.stderr
    assert "do-not-print" not in output
    assert str(access) not in output
    assert not (tmp_path / "out").exists()


def test_wrapper_refuses_short_sha(tmp_path):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()

    result, marker, _ = _run(head[:12], tmp_path)

    assert result.returncode == 2
    assert "full 40-character" in result.stderr
    assert not marker.exists()


def test_allowlist_has_no_write_test():
    allowlist = _bash_array("READONLY_TESTS")

    assert allowlist, "the allowlist is empty"
    assert not set(allowlist) & WRITE_TESTS
    for path in allowlist:
        assert (REPO / path).is_file(), f"{path} does not exist"
        source = (REPO / path).read_text()
        for verb in ("put", "delete", "patch"):
            assert not re.search(rf"\.{verb}\(", source), f"{path} calls .{verb}()"
            assert f'"{verb.upper()}"' not in source, f"{path} sends {verb.upper()}"
        posts = re.findall(r'\.post\(\s*f?"([^"]+)"', source)
        posts += re.findall(r'"POST",\s*f?"([^"]+)"', source)
        post_calls = len(re.findall(r"\.post\(", source)) + source.count('"POST"')
        assert len(posts) == post_calls, f"{path} has a POST with a target this test cannot read"
        for target in posts:
            assert target in ALLOWED_POSTS, f"{path} posts to {target}"


def test_portal_credential_test_is_deselected():
    assert PORTAL_TEST in _bash_array("DESELECT")


def test_wrapper_has_no_tracing_or_environment_dump():
    code = [
        line for line in WRAPPER.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code:
        assert not re.search(r"\bset\s+-[a-z]*x", line), line
        assert not re.search(r"\b(printenv|declare\s+-p|export\s+-p|typeset\s+-p)\b", line), line
        assert not re.search(r"(^|[|;&]\s*)env\s*($|[|;&])", line.strip()), line


def test_wrapper_parses_access_file_and_never_sources_it():
    source = WRAPPER.read_text()
    assert not re.search(r"^\s*(\.|source)\s+\"?\$access_file", source, re.MULTILINE)
    assert 'done < "$access_file"' in source


def test_wrapper_refuses_unknown_mode(tmp_path):
    result = subprocess.run(
        [str(WRAPPER), "0" * 40, "--mode", "firmware"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "unknown mode" in result.stderr


def test_wrapper_refuses_short_access_value(tmp_path):
    # redact.py does not replace values shorter than 3 characters, so the
    # wrapper must stop before it runs a mode.
    base_env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README").write_text("bench\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "README"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "init"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(cmd, cwd=repo, env=base_env, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=base_env,
                          capture_output=True, text=True, check=True).stdout.strip()
    access = tmp_path / "access.env"
    access.write_text("Manager URL: https://sixtyops-dev.infra.treehouse.mn\n"
                      "Manager username: bench-user\nManager password: q7\n")
    access.chmod(0o600)
    # A stub Python passes the dependency check. The refusal must come first,
    # so no lane runs with this stub.
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    result = subprocess.run(
        [str(WRAPPER), head],
        cwd=repo,
        env={**base_env, "SIXTYOPS_BENCH_ACCESS_FILE": str(access),
             "SIXTYOPS_BENCH_SUMMARY_DIR": str(tmp_path / "out"),
             "SIXTYOPS_BENCH_PYTHON": str(stub)},
        capture_output=True, text=True, timeout=60,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "shorter than 3 characters" in output
    assert "== lane:" not in output
    assert "q7" not in output
    assert "bench-user" not in output
