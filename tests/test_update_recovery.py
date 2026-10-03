"""Execute generated recovery scripts with isolated fake commands only."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from updater import release_checker as rc

OLD_ID = "sha256:" + "a" * 64
NEW_ID = "sha256:" + "b" * 64
IMAGE = "synthetic:latest"
PIN = "sixtyops-manager-rollback:" + "a" * 64
RECOVERY = (OLD_ID, IMAGE, PIN)

# Every command in the generated shell resolves here. Only /bin/sh and this
# private Python interpreter execute; no Docker daemon, git or sockets are used.
FAKE_COMMAND = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
p = Path(os.environ['RECOVERY_STATE'])
s = json.loads(p.read_text())
s['events'].append([name] + args)
code, output = 0, ''
if name == 'docker':
    if args[0] == 'image' and args[1] == 'inspect':
        output = s['aliases'].get(args[-1], '')
        code = 0 if output else 1
    elif args[0] == 'compose':
        if 'build' in args:
            s['aliases']['synthetic:latest'] = s['new_id']
            code = 42 if s['case'] == 'build-fail' else 0
        elif 'up' in args:
            s['ups'] += 1
            if (s['case'] == 'swap-fail' and s['ups'] == 1 or
                s['case'] == 'recovery-command-fail' and s['ups'] == 2):
                s['container'] = 'missing'
                code = 43
            else:
                s['container'] = s['aliases']['synthetic:latest']
        else:
            raise RuntimeError('Unexpected compose command')
    elif args[0] == 'inspect':
        assert 'Health.Status' in args[1]
        s['checks'] += 1
        output = 'healthy' if (s['case'] == 'healthy' or
                 s['container'] == s['old_id'] and s['case'] != 'recovery-health-fail') else 'unhealthy'
    elif args[0] == 'tag':
        s['aliases'][args[2]] = s['aliases'].get(args[1], args[1])
    elif args[0] == 'rmi':
        s['aliases'].pop(args[1], None)
    else:
        raise RuntimeError('Unexpected docker command')
elif name == 'git':
    if 'checkout' in args:
        code = 44 if s['case'] == 'source-restore-fail' else 0
        if code == 0:
            s['ref'] = args[-1]
    else:
        assert 'config' in args
elif name == 'seq':
    output = '\n'.join(str(i) for i in range(int(args[0]), int(args[1]) + 1))
else:
    assert name in ('sleep', 'apk', 'rm'), name
p.write_text(json.dumps(s))
if output:
    print(output)
sys.exit(code)
'''


def execute_watchdog(tmp_path, shape, case):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name in ("docker", "git", "apk", "seq", "sleep", "rm"):
        executable = fakebin / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        executable.chmod(0o700)
    state_path = tmp_path / "state.json"
    state = {"case": case, "old_id": OLD_ID, "new_id": NEW_ID,
             "aliases": {IMAGE: OLD_ID if shape == "source" else NEW_ID, PIN: OLD_ID},
             "container": OLD_ID, "ref": "new-ref", "ups": 0, "checks": 0, "events": []}
    if case == "missing-image":
        state["aliases"].pop(PIN)
    state_path.write_text(json.dumps(state))
    script = (rc._build_watchdog_script(str(tmp_path), "previous-ref", False, RECOVERY)
              if shape == "source" else
              rc._build_appliance_watchdog_script(str(tmp_path), False, RECOVERY))
    path = tmp_path / "watchdog.sh"
    path.write_text(script)
    result = subprocess.run(["/bin/sh", str(path)], cwd=tmp_path,
                            env={"PATH": str(fakebin), "RECOVERY_STATE": str(state_path),
                                 "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=15)
    return result, json.loads(state_path.read_text())


@pytest.mark.parametrize("shape", ["source", "appliance"])
@pytest.mark.parametrize("case", ["healthy", "unhealthy", "swap-fail", "missing-image",
                                  "recovery-command-fail", "recovery-health-fail"])
def test_generated_watchdog_outcomes(tmp_path, shape, case):
    result, state = execute_watchdog(tmp_path, shape, case)
    if case == "healthy":
        assert result.returncode == 0
        assert state["container"] == NEW_ID
        assert state["checks"] == 1
        assert PIN not in state["aliases"]
        assert "Update successful" in result.stdout
    elif case == "missing-image":
        assert result.returncode == 1
        assert state["ups"] == state["checks"] == 0
        assert state["container"] == OLD_ID
        assert "Refusing update" in result.stdout
    elif case in ("swap-fail", "unhealthy"):
        assert result.returncode == 1
        assert state["container"] == OLD_ID
        assert state["aliases"][PIN] == OLD_ID
        assert state["checks"] == (1 if case == "swap-fail" else 19)
        assert "Rollback successful" in result.stdout
    else:
        assert result.returncode == 2
        assert state["aliases"][PIN] == OLD_ID
        assert "ERROR: Rollback" in result.stdout
        assert "Rollback successful" not in result.stdout
        assert state["checks"] == (18 if case == "recovery-command-fail" else 30)
    if shape == "source" and case != "healthy":
        assert state["ref"] == "previous-ref"
    if shape == "appliance":
        assert not any(event[0] in ("git", "apk") for event in state["events"])
    # The retained alias never points to the rebuilt image, even when build
    # replaces the same service tag used by the old running container.
    tags = [event for event in state["events"] if event[:2] == ["docker", "tag"]]
    assert all(event[2] == PIN for event in tags)


@pytest.mark.parametrize("case", ["build-fail", "source-restore-fail"])
def test_source_build_and_restore_failures(tmp_path, case):
    # Force the build failure separately from source-restoration failure.
    if case == "source-restore-fail":
        result, state = execute_watchdog(tmp_path, "source", case)
        assert result.returncode == 2
        assert "Rollback command failed" in result.stdout
    else:
        result, state = execute_watchdog(tmp_path, "source", case)
        assert result.returncode == 3
        assert state["ups"] == 0
        assert state["checks"] == 0
        assert state["ref"] == "previous-ref"
        assert "Build failed" in result.stdout
        assert state["container"] == OLD_ID
        assert state["aliases"][IMAGE] == OLD_ID
    assert state["aliases"][PIN] == OLD_ID


@pytest.mark.parametrize("health,identity,inspect_code,tag_code", [
    ("healthy", OLD_ID, 0, 0), ("unhealthy", OLD_ID, 0, 0),
    ("healthy", "synthetic:latest", 0, 0), ("healthy", OLD_ID, 1, 0),
    ("healthy", OLD_ID, 0, 1),
])
def test_retain_immutable_image(monkeypatch, health, identity, inspect_code, tag_code):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, inspect_code if cmd[1] == "inspect" else tag_code,
                                           f"{identity} {IMAGE} {health}", "")
    monkeypatch.setattr(rc.subprocess, "run", run)
    result = rc._retain_recovery_image()
    valid = health == "healthy" and identity == OLD_ID and inspect_code == tag_code == 0
    assert result == (RECOVERY if valid else None)
    if len(calls) == 2:
        assert calls[1] == ["docker", "tag", OLD_ID, PIN]
    else:
        assert not valid


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,failure", [
    (shape, failure) for shape in ("source", "appliance")
    for failure in ("launch", "launch-exception", "image", "path", "restore", "checkout-timeout")
    if shape == "source" or failure in ("launch", "launch-exception", "image")
])
async def test_failed_initiation_is_guarded(tmp_path, monkeypatch, shape, failure):
    settings = {"autoupdate_available_version": "1.4.1-dev5"}
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default="": settings.get(key, default)
    db.set_settings.side_effect = lambda values: settings.update(values)
    monkeypatch.setattr(rc, "db", db)
    (tmp_path / "updater").mkdir()
    (tmp_path / "updater/__init__.py").write_text('__version__ = "1.4.1-dev5"')
    monkeypatch.setattr(rc, "_is_safe_to_update", lambda: (True, ""))
    monkeypatch.setattr(rc, "_docker_socket_available", lambda: True)
    monkeypatch.setattr(rc, "_get_repo_dir", lambda: tmp_path)
    monkeypatch.setattr(rc, "_get_compose_dir", lambda: tmp_path)
    monkeypatch.setattr(rc, "_get_host_repo_path", lambda: None if failure == "path" else str(tmp_path))
    monkeypatch.setattr(rc, "_verify_tag_signature", lambda *args: (True, ""))
    monkeypatch.setattr(rc, "APPLIANCE_MODE", shape == "appliance")
    calls = []
    def retain():
        calls.append(["retain"])
        return None if failure == "image" else RECOVERY
    monkeypatch.setattr(rc, "_retain_recovery_image", retain)
    def launch(*args):
        if failure == "launch-exception":
            raise OSError("Synthetic launch error")
        return False
    monkeypatch.setattr(rc, "_launch_watchdog", launch)
    monkeypatch.setattr(rc, "_launch_appliance_watchdog", launch)
    def run(cmd, **kwargs):
        calls.append(cmd)
        if failure == "checkout-timeout" and cmd[-1] == "v1.4.1-dev5":
            raise subprocess.TimeoutExpired(cmd, 30)
        code = 1 if failure == "restore" and cmd[-1] == "previous-ref" else 0
        return subprocess.CompletedProcess(cmd, code, "previous-ref" if "rev-parse" in cmd else "", "")
    monkeypatch.setattr(rc.subprocess, "run", run)
    popen = MagicMock(side_effect=AssertionError("Unguarded subprocess"))
    monkeypatch.setattr(rc.subprocess, "Popen", popen)
    result = await rc.apply_update()
    assert result["success"] is False
    assert result["action"] != "started"
    popen.assert_not_called()
    if failure == "launch-exception":
        assert result["launch_uncertain"]
        assert settings["autoupdate_pending_version"] == "1.4.1-dev5"
        assert settings["autoupdate_launch_uncertain"] == "true"
        assert not any(cmd[-1] == "previous-ref" and "checkout" in cmd for cmd in calls)
        return
    assert not settings.get("autoupdate_pending_version")
    assert not settings.get("autoupdate_pending_at")
    if failure in ("image", "path"):
        assert not any("checkout" in cmd or "pull" in cmd for cmd in calls)
    else:
        assert calls.index(["retain"]) < next(i for i, cmd in enumerate(calls)
                                              if "pull" in cmd or "checkout" in cmd)
        if shape == "source":
            assert calls[-1][-2:] == ["checkout", "previous-ref"]
    if failure == "restore":
        assert "source recovery failed" in result["message"]


@pytest.mark.parametrize("shape", ["source", "appliance"])
@pytest.mark.parametrize("outcome", [0, 1, "timeout"])
def test_watchdog_launcher_results(tmp_path, monkeypatch, shape, outcome):
    monkeypatch.setattr(rc, "db", MagicMock())
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "run" and outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, outcome if cmd[1] == "run" else 0,
                                           "c" * 64, "synthetic launch failure")

    monkeypatch.setattr(rc.subprocess, "run", run)
    launched = (rc._launch_watchdog(tmp_path, str(tmp_path), "previous-ref", False, RECOVERY)
                if shape == "source" else
                rc._launch_appliance_watchdog(tmp_path, False, RECOVERY))
    assert launched is (True if outcome == 0 else None)
    assert all(cmd[:2] in (["docker", "inspect"], ["docker", "run"]) for cmd in calls)
    script = (tmp_path / ".update-watchdog.sh").read_text()
    assert OLD_ID in script and PIN in script


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["source", "appliance"])
@pytest.mark.parametrize("outcome", ["timeout", "lost-ack", "acknowledged"])
@pytest.mark.parametrize("observation", ["running", "absent", "error", "exited"])
async def test_delayed_daemon_launch_preserves_state(tmp_path, monkeypatch, shape, outcome, observation):
    """A late accepted request may mutate after an absent/failed observation."""
    from datetime import datetime, timedelta

    settings = {"autoupdate_available_version": "1.4.1-dev5"}
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default="": settings.get(key, default)
    db.set_settings.side_effect = lambda values: settings.update(values)
    monkeypatch.setattr(rc, "db", db)
    (tmp_path / "updater").mkdir()
    (tmp_path / "updater/__init__.py").write_text('__version__ = "1.4.1-dev5"')
    for name, value in [("_is_safe_to_update", lambda: (True, "")),
                        ("_docker_socket_available", lambda: True),
                        ("_get_repo_dir", lambda: tmp_path), ("_get_compose_dir", lambda: tmp_path),
                        ("_get_host_repo_path", lambda: str(tmp_path)),
                        ("_verify_tag_signature", lambda *args: (True, "")),
                        ("APPLIANCE_MODE", shape == "appliance")]:
        monkeypatch.setattr(rc, name, value)
    daemon = {"accepted": False, "source": "previous-ref", "events": []}

    def run(cmd, **kwargs):
        daemon["events"].append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            if "{{.State.Status}}" in cmd:
                if observation == "error":
                    raise subprocess.TimeoutExpired(cmd, 10)
                return subprocess.CompletedProcess(cmd, 1 if observation == "absent" else 0,
                                                   observation, "")
            return subprocess.CompletedProcess(cmd, 0, f"{OLD_ID} {IMAGE} healthy", "")
        if cmd[:2] == ["docker", "run"]:
            daemon["accepted"] = True
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0 if outcome == "acknowledged" else 1,
                                               "c" * 64, "lost acknowledgement")
        if cmd[0] == "git" and "checkout" in cmd:
            daemon["source"] = cmd[-1]
        assert cmd[0] in ("docker", "git")
        assert cmd[:2] != ["docker", "rm"]
        return subprocess.CompletedProcess(cmd, 0, "previous-ref" if "rev-parse" in cmd else "", "")

    # Save the real shell runner before patching subprocess globally.
    real_run = subprocess.run
    real_popen = subprocess.Popen
    monkeypatch.setattr(rc.subprocess, "run", run)
    monkeypatch.setattr(rc.subprocess, "Popen", MagicMock(side_effect=AssertionError("Direct fallback")))
    result = await rc.apply_update()
    assert daemon["accepted"]
    assert settings["autoupdate_pending_version"] == "1.4.1-dev5"
    assert daemon["source"] == ("v1.4.1-dev5" if shape == "source" else "previous-ref")
    if outcome == "acknowledged":
        assert result["success"] and result["action"] == "started"
        assert not settings["autoupdate_launch_uncertain"]
        return
    assert not result["success"] and result["launch_uncertain"]
    assert result["action"] == "blocked"
    assert settings["autoupdate_launch_uncertain"] == "true"
    if shape == "source":
        assert settings["autoupdate_rollback_ref"] == "previous-ref"
    events = list(daemon["events"])
    retry = await rc.apply_update()
    assert retry["launch_uncertain"] and daemon["events"] == events
    # Old startup cleanup must not discard uncertainty, even after its timeout
    # or when the requested version is already running.
    settings["autoupdate_pending_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(rc, "__version__", "1.4.1-dev5")
    await rc.verify_update_on_startup()
    assert settings["autoupdate_pending_version"] == "1.4.1-dev5"
    assert settings["autoupdate_launch_uncertain"] == "true"
    # Execute the actual saved script later with a fake-only PATH. This proves
    # that the retained state remains coherent when the delayed daemon swaps.
    state_path = tmp_path / "late-state.json"
    state = {"case": "healthy", "old_id": OLD_ID, "new_id": NEW_ID,
             "aliases": {IMAGE: OLD_ID if shape == "source" else NEW_ID, PIN: OLD_ID},
             "container": OLD_ID, "ref": daemon["source"], "ups": 0, "checks": 0, "events": []}
    state_path.write_text(json.dumps(state))
    fakebin = tmp_path / "late-bin"
    fakebin.mkdir()
    for name in ("docker", "git", "apk", "seq", "sleep", "rm"):
        executable = fakebin / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        executable.chmod(0o700)
    monkeypatch.setattr(rc.subprocess, "Popen", real_popen)
    late = real_run(["/bin/sh", str(tmp_path / ".update-watchdog.sh")], cwd=tmp_path,
                    env={"PATH": str(fakebin), "RECOVERY_STATE": str(state_path),
                         "PYTHONDONTWRITEBYTECODE": "1"},
                    capture_output=True, text=True, timeout=15)
    assert late.returncode == 0, late.stdout + late.stderr
    state = json.loads(state_path.read_text())
    assert state["container"] == NEW_ID and state["ups"] == 1
    assert settings["autoupdate_launch_uncertain"] == "true"


@pytest.mark.parametrize("shape", ["source", "appliance"])
def test_prelaunch_failure_is_known_not_started(tmp_path, monkeypatch, shape):
    monkeypatch.setattr(Path, "write_text", MagicMock(side_effect=OSError("Synthetic write failure")))
    run = MagicMock(side_effect=AssertionError("No daemon request allowed"))
    monkeypatch.setattr(rc.subprocess, "run", run)
    result = (rc._launch_watchdog(tmp_path, str(tmp_path), "previous-ref", False, RECOVERY)
              if shape == "source" else rc._launch_appliance_watchdog(tmp_path, False, RECOVERY))
    assert result is False
    run.assert_not_called()


WATCHDOG_ID = "c" * 64
PRIOR_REF = "d" * 40


@pytest.fixture
def terminal_pending(tmp_path, monkeypatch):
    settings = {"autoupdate_pending_version": "1.4.1-dev5",
                "autoupdate_pending_at": "2000-01-01T00:00:00",
                "autoupdate_launch_uncertain": "",
                "autoupdate_watchdog_id": WATCHDOG_ID,
                "autoupdate_prior_image_id": OLD_ID,
                "autoupdate_rollback_ref": PRIOR_REF}
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default="": settings.get(key, default)
    db.set_settings.side_effect = lambda values: settings.update(values)
    monkeypatch.setattr(rc, "db", db)
    monkeypatch.setattr(rc, "_get_repo_dir", lambda: tmp_path)
    proof = {"daemon": {"Id": WATCHDOG_ID, "State": {
        "Status": "exited", "Running": False, "ExitCode": 3}},
        "runtime": f"{OLD_ID} healthy", "source": PRIOR_REF,
        "inspect_code": 0, "remove_code": 0, "remaining": "", "list_code": 0}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        output, code = "", 0
        if cmd[:2] == ["docker", "inspect"] and cmd[-1] == WATCHDOG_ID:
            output, code = json.dumps(proof["daemon"]), proof["inspect_code"]
        elif cmd[:2] == ["docker", "inspect"] and cmd[-1] == "sixtyops-management":
            output = proof["runtime"]
        elif cmd[0] == "git":
            assert cmd[-2:] == ["rev-parse", "HEAD"]
            output = proof["source"]
        elif cmd[:2] == ["docker", "rm"]:
            assert cmd == ["docker", "rm", WATCHDOG_ID]
            code = proof["remove_code"]
        elif cmd[:2] == ["docker", "ps"]:
            output, code = proof["remaining"], proof["list_code"]
        else:
            raise AssertionError(f"Unexpected command: {cmd}")
        return subprocess.CompletedProcess(cmd, code, output, "")

    return settings, proof, calls, run


@pytest.mark.asyncio
async def test_acknowledged_build_failure_reconciles_and_retries(tmp_path, monkeypatch, terminal_pending):
    from unittest.mock import patch
    settings, proof, calls, terminal_run = terminal_pending
    settings.clear()
    settings["autoupdate_available_version"] = "1.4.1-dev5"
    (tmp_path / "updater").mkdir()
    (tmp_path / "updater/__init__.py").write_text('__version__ = "1.4.1-dev5"')
    monkeypatch.setattr(rc, "_is_safe_to_update", lambda: (True, ""))
    monkeypatch.setattr(rc, "_docker_socket_available", lambda: True)
    monkeypatch.setattr(rc, "_get_host_repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(rc, "_verify_tag_signature", lambda *args: (True, ""))
    monkeypatch.setattr(rc, "APPLIANCE_MODE", False)
    launches = []

    def initiate(cmd, **kwargs):
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, f"{OLD_ID} {IMAGE} healthy", "")
        if cmd[:2] == ["docker", "run"]:
            assert "--rm" not in cmd
            launches.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, WATCHDOG_ID, "")
        assert cmd[0] == "git" or cmd[:2] == ["docker", "tag"]
        return subprocess.CompletedProcess(cmd, 0, PRIOR_REF if "rev-parse" in cmd else "", "")

    with patch.object(rc.subprocess, "run", side_effect=initiate):
        first = await rc.apply_update()
    assert first["action"] == "started" and settings["autoupdate_watchdog_id"] == WATCHDOG_ID
    execution = tmp_path / "execution"
    execution.mkdir()
    result, state = execute_watchdog(execution, "source", "build-fail")
    assert result.returncode == proof["daemon"]["State"]["ExitCode"] == 3
    assert state["ups"] == 0 and state["container"] == OLD_ID
    assert state["ref"] == "previous-ref" and state["aliases"][PIN] == OLD_ID
    with patch.object(rc.subprocess, "run", side_effect=terminal_run):
        status = rc.ReleaseChecker(MagicMock()).get_update_status()
    assert not status.get("blocked_reason") and not settings["autoupdate_pending_version"]
    assert calls[-2:] == [["docker", "rm", WATCHDOG_ID],
                         ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"]]
    with patch.object(rc.subprocess, "run", side_effect=initiate):
        retry = await rc.apply_update()
    assert retry["action"] == "started" and len(launches) == 2


@pytest.mark.parametrize("failure", ["absent", "malformed", "wrong-id", "running", "dead",
    "bad-exit", "boolean-exit", "restore-failed", "runtime-image", "runtime-health",
    "source", "bad-ref", "remove", "cleanup-readback", "list-error", "list-malformed",
    "uncertain", "inspect-timeout"])
def test_terminal_proof_failures_retain_fence(monkeypatch, terminal_pending, failure):
    settings, proof, calls, run = terminal_pending
    state = proof["daemon"]["State"]
    if failure == "absent": proof["inspect_code"] = 1
    elif failure == "malformed": proof["daemon"] = []
    elif failure == "wrong-id": proof["daemon"]["Id"] = "e" * 64
    elif failure == "running": state.update(Status="running", Running=True)
    elif failure == "dead": state["Status"] = "dead"
    elif failure == "bad-exit": state["ExitCode"] = 99
    elif failure == "boolean-exit": state["ExitCode"] = True
    elif failure == "restore-failed": state["ExitCode"] = 2
    elif failure == "runtime-image": proof["runtime"] = f"{NEW_ID} healthy"
    elif failure == "runtime-health": proof["runtime"] = f"{OLD_ID} unhealthy"
    elif failure == "source": proof["source"] = "e" * 40
    elif failure == "bad-ref": settings["autoupdate_rollback_ref"] = "malformed"
    elif failure == "remove": proof["remove_code"] = 1
    elif failure == "cleanup-readback": proof["remaining"] = WATCHDOG_ID
    elif failure == "list-error": proof["list_code"] = 1
    elif failure == "list-malformed": proof["remaining"] = "invalid-id"
    elif failure == "uncertain": settings["autoupdate_launch_uncertain"] = "true"
    elif failure == "inspect-timeout":
        def run(cmd, **kwargs): raise subprocess.TimeoutExpired(cmd, 10)
    monkeypatch.setattr(rc.subprocess, "run", run)
    assert rc._pending_update_block() is not None
    assert settings["autoupdate_pending_version"] == "1.4.1-dev5"
    assert settings["autoupdate_watchdog_id"] == WATCHDOG_ID
    assert not any(cmd[:2] == ["docker", "run"] for cmd in calls)
    if failure not in ("remove", "cleanup-readback", "list-error", "list-malformed"):
        assert not any(cmd[:2] == ["docker", "rm"] for cmd in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1, 3])
async def test_terminal_startup_controls(monkeypatch, terminal_pending, code):
    settings, proof, calls, run = terminal_pending
    proof["daemon"]["State"]["ExitCode"] = code
    if code == 0:
        monkeypatch.setattr(rc, "__version__", settings["autoupdate_pending_version"])
        proof["runtime"] = f"{NEW_ID} healthy"
    monkeypatch.setattr(rc.subprocess, "run", run)
    from unittest.mock import AsyncMock
    broadcast = AsyncMock()
    await rc.verify_update_on_startup(broadcast)
    assert broadcast.call_args.args[0]["type"] == {0: "update_completed", 1: "update_rolled_back", 3: "update_failed"}[code]
    assert not settings["autoupdate_pending_version"]


@pytest.mark.asyncio
async def test_startup_does_not_clear_aged_running_daemon(monkeypatch, terminal_pending):
    settings, proof, calls, run = terminal_pending
    proof["daemon"]["State"].update(Status="running", Running=True)
    monkeypatch.setattr(rc, "__version__", settings["autoupdate_pending_version"])
    monkeypatch.setattr(rc.subprocess, "run", run)
    await rc.verify_update_on_startup()
    assert settings["autoupdate_pending_version"]


@pytest.mark.asyncio
async def test_reconciliation_serializes_against_retry(monkeypatch, terminal_pending):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    settings, proof, calls, run = terminal_pending
    entered, finish = threading.Event(), threading.Event()
    def delayed(cmd, **kwargs):
        if cmd[:2] == ["docker", "inspect"] and cmd[-1] == WATCHDOG_ID:
            entered.set()
            assert finish.wait(5)
        return run(cmd, **kwargs)
    monkeypatch.setattr(rc.subprocess, "run", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reconciliation = pool.submit(rc._pending_update_block)
        assert entered.wait(5)
        try:
            retry = await rc.apply_update()
            assert retry["action"] == "blocked"
            assert rc._pending_update_block() is not None
            assert len(calls) == 0
        finally:
            finish.set()
        assert reconciliation.result(timeout=5) is None
    assert not settings["autoupdate_pending_version"]


@pytest.mark.asyncio
async def test_two_initiations_cannot_overlap(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import asyncio
    import threading
    entered, finish = threading.Event(), threading.Event()
    calls = []
    async def initiation():
        calls.append("initiate")
        entered.set()
        assert finish.wait(5)
        return {"success": True, "action": "started"}
    monkeypatch.setattr(rc, "_apply_update_locked", initiation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(lambda: asyncio.run(rc.apply_update()))
        assert entered.wait(5)
        try:
            second = await rc.apply_update()
            assert second["action"] == "blocked" and calls == ["initiate"]
        finally:
            finish.set()
        assert first.result(timeout=5)["action"] == "started"
    # The lock is released after completion, not after a time delay.
    assert rc._update_lock.acquire(blocking=False)
    rc._update_lock.release()


@pytest.mark.parametrize("shape", ["source", "appliance"])
def test_malformed_acknowledgement_is_uncertain(tmp_path, monkeypatch, shape):
    db = MagicMock()
    monkeypatch.setattr(rc, "db", db)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "invalid-container-id", "")
    monkeypatch.setattr(rc.subprocess, "run", run)
    result = (rc._launch_watchdog(tmp_path, str(tmp_path), PRIOR_REF, False, RECOVERY)
              if shape == "source" else rc._launch_appliance_watchdog(tmp_path, False, RECOVERY))
    assert result is None
    db.set_settings.assert_not_called()
    assert calls[-1][:2] == ["docker", "inspect"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1, 3])
async def test_status_broadcasts_terminal_result_after_startup(monkeypatch, terminal_pending, code):
    import asyncio
    settings, proof, calls, run = terminal_pending
    delivered = asyncio.Event()
    messages = []
    async def broadcast(event):
        messages.append(event)
        delivered.set()
    monkeypatch.setattr(rc, "_checker", rc.ReleaseChecker(broadcast))
    if code == 0:
        monkeypatch.setattr(rc, "__version__", settings["autoupdate_pending_version"])
        proof["runtime"] = f"{NEW_ID} healthy"
    proof["daemon"]["State"].update(Status="running", Running=True, ExitCode=code)
    monkeypatch.setattr(rc.subprocess, "run", run)
    await rc.verify_update_on_startup(broadcast)
    assert not delivered.is_set() and settings["autoupdate_pending_version"]
    proof["daemon"]["State"].update(Status="exited", Running=False)
    assert rc._pending_update_block() is None
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert messages[0]["type"] == {0: "update_completed", 1: "update_rolled_back", 3: "update_failed"}[code]
    assert rc._pending_update_block() is None
    assert len(messages) == 1
