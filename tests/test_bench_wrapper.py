"""Static and refusal checks for the read-only bench wrapper.

These tests never load real credentials and never contact a device.
"""

import re
import subprocess
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "bench" / "run-readonly-bench.sh"
READONLY_TESTS = REPO / "scripts" / "bench" / "readonly-tests.txt"
DESELECT_TESTS = REPO / "scripts" / "bench" / "readonly-deselect.txt"

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


def _read_list(path):
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


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
    allowlist = _read_list(READONLY_TESTS)

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
    assert PORTAL_TEST in _read_list(DESELECT_TESTS)


def test_bench_and_release_share_the_readonly_allowlist():
    wrapper = WRAPPER.read_text()
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text()
    assert '"$bench_dir/readonly-tests.txt"' in wrapper
    assert '"$bench_dir/readonly-deselect.txt"' in wrapper
    assert "scripts/bench/readonly-tests.txt" in workflow
    assert "scripts/bench/readonly-deselect.txt" in workflow


def test_wrapper_has_no_tracing_or_environment_dump():
    code = [
        line for line in WRAPPER.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code:
        assert not re.search(r"\bset\s+-[a-z]*x", line), line
        assert not re.search(r"\b(printenv|declare\s+-p|export\s+-p|typeset\s+-p)\b", line), line
        assert not re.search(r"(^|[|;&]\s*)env\s*($|[|;&])", line.strip()), line


def test_wrapper_extracts_archive_from_file_not_pipe():
    source = WRAPPER.read_text()

    assert 'git archive HEAD > "$archive_file"' in source
    assert 'tar -x -f "$archive_file" -C "$code_dir"' in source
    assert 'git archive HEAD | tar' not in source


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
    assert "value is too short" in output
    assert "== lane:" not in output
    assert "q7" not in output
    assert "bench-user" not in output


def _make_bench_repo(tmp_path):
    base_env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README").write_text("bench\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "README"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(cmd, cwd=repo, env=base_env, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=base_env,
                          capture_output=True, text=True, check=True).stdout.strip()
    return base_env, repo, head


def _access_file(path, contents, mode=0o600):
    path.write_text(contents)
    path.chmod(mode)
    return path


def _run_local_poll(tmp_path, *, manager=None, ap=None, legacy=None, python_stub=None, env_extra=None):
    base_env, repo, head = _make_bench_repo(tmp_path)
    calls = tmp_path / "calls"
    stub = python_stub or (tmp_path / "python")
    if python_stub is None:
        stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$BENCH_CALLS_FILE\"\n"
                        "case \"$1\" in *redact.py) cat; exit 0 ;; esac\n"
                        "case \"$1\" in *readonly_poll.py) echo 'readonly-poll: DONE' ;; esac\nexit 0\n")
        stub.chmod(0o755)
    env = {**base_env, "SIXTYOPS_BENCH_PYTHON": str(stub), "BENCH_CALLS_FILE": str(calls),
           "SIXTYOPS_BENCH_SUMMARY_DIR": str(tmp_path / "out")}
    if manager is not None:
        env["SIXTYOPS_BENCH_MANAGER_ACCESS_FILE"] = str(manager)
    if ap is not None:
        env["SIXTYOPS_BENCH_AP_ACCESS_FILE"] = str(ap)
    if legacy is not None:
        env["SIXTYOPS_BENCH_ACCESS_FILE"] = str(legacy)
    if env_extra is not None:
        env.update(env_extra)
    result = subprocess.run([str(WRAPPER), head, "--mode", "local-poll"], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=60)
    return result, calls


def test_wrapper_accepts_separate_ap_source_and_does_not_source_values(tmp_path):
    marker = tmp_path / "must-not-exist"
    ap = _access_file(tmp_path / "ap.env",
                      f"AP IP: 192.0.2.20\nShared device username: synthetic-user\n"
                      f"Shared device password: $(touch {marker})\n")

    result, calls = _run_local_poll(tmp_path, ap=ap)

    assert result.returncode == 0, result.stderr
    assert "readonly_poll.py" in calls.read_text()
    assert not marker.exists()
    assert "synthetic-user" not in result.stdout + result.stderr


def test_wrapper_keeps_legacy_single_file_for_ap_mode(tmp_path):
    legacy = _access_file(tmp_path / "legacy.env",
                          "AP IP: 192.0.2.20\nShared device username: synthetic-user\n"
                          "Shared device password: synthetic-password\n")

    result, calls = _run_local_poll(tmp_path, legacy=legacy)

    assert result.returncode == 0, result.stderr
    assert "readonly_poll.py" in calls.read_text()


def test_integration_uses_both_separate_sources_with_synthetic_values(tmp_path):
    base_env, repo, head = _make_bench_repo(tmp_path)
    manager = _access_file(tmp_path / "manager.env",
                            "Manager URL: https://sixtyops-dev.infra.treehouse.mn\n"
                            "Manager username: synthetic-manager\n"
                            "Manager password: synthetic-password\n")
    ap = _access_file(tmp_path / "ap.env",
                      "AP IP: 192.0.2.20\nTNS-100 IP: 192.0.2.21\n")
    calls = tmp_path / "calls"
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BENCH_CALLS_FILE\"\n"
                    "case \"$1\" in *redact.py) cat ;; esac\nexit 0\n")
    stub.chmod(0o755)
    result = subprocess.run(
        [str(WRAPPER), head, "--mode", "integration"], cwd=repo,
        env={**base_env, "SIXTYOPS_BENCH_MANAGER_ACCESS_FILE": str(manager),
             "SIXTYOPS_BENCH_AP_ACCESS_FILE": str(ap),
             "SIXTYOPS_BENCH_PYTHON": str(stub), "BENCH_CALLS_FILE": str(calls),
             "SIXTYOPS_BENCH_SUMMARY_DIR": str(tmp_path / "out")},
        capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "-m pytest" in calls.read_text()
    assert "synthetic-manager" not in result.stdout + result.stderr
    assert "synthetic-password" not in result.stdout + result.stderr


def test_integration_requires_both_sources_before_pytest(tmp_path):
    base_env, repo, head = _make_bench_repo(tmp_path)
    ap = _access_file(tmp_path / "ap.env", "AP IP: 192.0.2.20\nTNS-100 IP: 192.0.2.21\n")
    calls = tmp_path / "calls"
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BENCH_CALLS_FILE\"\nexit 0\n")
    stub.chmod(0o755)
    result = subprocess.run(
        [str(WRAPPER), head, "--mode", "integration"], cwd=repo,
        env={**base_env, "SIXTYOPS_BENCH_AP_ACCESS_FILE": str(ap),
             "SIXTYOPS_BENCH_PYTHON": str(stub), "BENCH_CALLS_FILE": str(calls)},
        capture_output=True, text=True, timeout=60)

    assert result.returncode == 2
    assert "Manager and AP access sources are required" in result.stdout + result.stderr
    assert not calls.exists() or "-m pytest" not in calls.read_text()


@pytest.mark.parametrize("contents", [
    "AP IP: 192.0.2.20\nAP IP: 192.0.2.21\n",
    "Manager URL: https://example.invalid\n",
])
def test_wrapper_rejects_duplicate_and_wrong_source_keys_before_lane(tmp_path, contents):
    ap = _access_file(tmp_path / "ap.env", contents)

    result, calls = _run_local_poll(tmp_path, ap=ap)

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "REFUSED" in output
    assert "== lane:" not in output
    assert not calls.exists() or "readonly_poll.py" not in calls.read_text()
    assert "example.invalid" not in output


@pytest.mark.parametrize("bad_path", ["symlink-file", "symlink-parent", "inside-repo", "directory"])
def test_wrapper_rejects_unsafe_access_source_path(tmp_path, bad_path):
    base_env, repo, head = _make_bench_repo(tmp_path)
    safe = _access_file(tmp_path / "safe.env", "AP IP: 192.0.2.20\n")
    if bad_path == "symlink-file":
        access = tmp_path / "alias.env"
        access.symlink_to(safe)
    elif bad_path == "symlink-parent":
        parent = tmp_path / "alias-dir"
        parent.symlink_to(tmp_path, target_is_directory=True)
        access = parent / safe.name
    elif bad_path == "inside-repo":
        access = repo / ".git" / "inside.env"
        access.write_text("AP IP: 192.0.2.20\n")
        access.chmod(0o600)
    else:
        access = tmp_path / "directory"
        access.mkdir()
    result = subprocess.run([str(WRAPPER), head, "--mode", "local-poll"], cwd=repo,
                            env={**base_env, "SIXTYOPS_BENCH_AP_ACCESS_FILE": str(access)},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert str(access) not in result.stdout + result.stderr


def test_wrapper_rejects_access_source_mode_other_than_600_or_400(tmp_path):
    ap = _access_file(tmp_path / "ap.env", "AP IP: 192.0.2.20\n", mode=0o640)

    result, calls = _run_local_poll(tmp_path, ap=ap)

    assert result.returncode == 2
    assert "mode must be 600 or 400" in result.stderr
    assert not calls.exists()


def test_wrapper_rejects_nonowned_source_when_chown_is_available(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("changing file ownership requires root")
    ap = _access_file(tmp_path / "ap.env", "AP IP: 192.0.2.20\n")
    os.chown(ap, 65534, 65534)

    result, calls = _run_local_poll(tmp_path, ap=ap)

    assert result.returncode == 2
    assert "not owned by the current user" in result.stderr
    assert not calls.exists()


def test_wrapper_fails_closed_on_missing_fields_before_lane(tmp_path):
    ap = _access_file(tmp_path / "ap.env", "AP IP: 192.0.2.20\n")

    result, calls = _run_local_poll(tmp_path, ap=ap)

    assert result.returncode == 2
    assert "required access fields are missing" in result.stdout + result.stderr
    assert "== lane:" not in result.stdout + result.stderr
    assert not calls.exists() or "readonly_poll.py" not in calls.read_text()


def test_inherited_ap_values_do_not_fill_missing_ap_file_fields(tmp_path):
    ap = _access_file(tmp_path / "ap.env", "AP IP: 192.0.2.20\n")
    inherited = {
        "SIXTYOPS_TEST_AP_IP": "198.51.100.20",
        "SIXTYOPS_TEST_CONFIG_AP_IP": "198.51.100.20",
        "SIXTYOPS_BENCH_AP_IP": "198.51.100.20",
        "SIXTYOPS_BENCH_AP_USER": "synthetic-inherited-user",
        "SIXTYOPS_BENCH_AP_PASS": "synthetic-inherited-password",
    }

    result, calls = _run_local_poll(tmp_path, ap=ap, env_extra=inherited)

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "required access fields are missing" in output
    assert "== lane:" not in output
    assert "synthetic-inherited" not in output
    assert not calls.exists() or "readonly_poll.py" not in calls.read_text()


def test_inherited_manager_values_do_not_fill_missing_manager_file_fields(tmp_path):
    base_env, repo, head = _make_bench_repo(tmp_path)
    manager = _access_file(tmp_path / "manager.env",
                            "Manager URL: https://sixtyops-dev.infra.treehouse.mn\n"
                            "Manager username: synthetic-manager\n")
    ap = _access_file(tmp_path / "ap.env",
                      "AP IP: 192.0.2.20\nTNS-100 IP: 192.0.2.21\n")
    calls = tmp_path / "calls"
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BENCH_CALLS_FILE\"\nexit 0\n")
    stub.chmod(0o755)
    result = subprocess.run(
        [str(WRAPPER), head, "--mode", "integration"], cwd=repo,
        env={**base_env, "SIXTYOPS_BENCH_MANAGER_ACCESS_FILE": str(manager),
             "SIXTYOPS_BENCH_AP_ACCESS_FILE": str(ap),
             "SIXTYOPS_TEST_PASS": "synthetic-inherited-password",
             "SIXTYOPS_BENCH_PYTHON": str(stub), "BENCH_CALLS_FILE": str(calls)},
        capture_output=True, text=True, timeout=60)

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "required access fields are missing" in output
    assert "synthetic-inherited-password" not in output
    assert not calls.exists() or "-m pytest" not in calls.read_text()


@pytest.mark.parametrize(("duration_args", "expected"), [([], "3"), (["--duration-min", "9"], "9")])
def test_session_proof_duration_defaults_short_and_keeps_override(tmp_path, duration_args, expected):
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
    access.write_text("AP IP: 192.0.2.20\nShared device username: bench-user\n"
                      "Shared device password: bench-password\n")
    access.chmod(0o600)
    calls = tmp_path / "python-calls.txt"
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BENCH_CALLS_FILE\"\n"
                    "case \"$1\" in\n"
                    "  *redact.py) cat ;;\n"
                    "  *session_proof.py) echo 'session-proof: DONE' ;;\n"
                    "esac\nexit 0\n")
    stub.chmod(0o755)

    result = subprocess.run(
        [str(WRAPPER), head, "--mode", "session-proof", *duration_args],
        cwd=repo,
        env={**base_env, "SIXTYOPS_BENCH_ACCESS_FILE": str(access),
             "SIXTYOPS_BENCH_SUMMARY_DIR": str(tmp_path / "out"),
             "SIXTYOPS_BENCH_PYTHON": str(stub), "BENCH_CALLS_FILE": str(calls)},
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    session_call = next(line for line in calls.read_text().splitlines() if "session_proof.py" in line)
    assert f"--duration-min {expected}" in session_call
