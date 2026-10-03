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
        assert result.returncode == 1
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
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "run" and outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, outcome if cmd[1] == "run" else 0,
                                           "synthetic-watchdog", "synthetic launch failure")

    monkeypatch.setattr(rc.subprocess, "run", run)
    launched = (rc._launch_watchdog(tmp_path, str(tmp_path), "previous-ref", False, RECOVERY)
                if shape == "source" else
                rc._launch_appliance_watchdog(tmp_path, False, RECOVERY))
    assert launched is (outcome == 0)
    assert all(cmd[:2] in (["docker", "rm"], ["docker", "run"]) for cmd in calls)
    script = (tmp_path / ".update-watchdog.sh").read_text()
    assert OLD_ID in script and PIN in script
