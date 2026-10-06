"""Test production startup with synthetic paths and mocked privilege syscalls."""

import importlib.util
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "container_user", ROOT / "scripts" / "container_user.py"
)
USER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(USER)


class Executed(Exception):
    pass


@pytest.mark.parametrize("socket_gid", [None, 0, 1500, 23719])
def test_user_drop_replaces_groups_before_exec(socket_gid):
    """Unknown socket GIDs work without a group database entry."""
    calls = []
    account = SimpleNamespace(pw_uid=1500, pw_gid=1500, pw_dir="/home/appuser")
    socket = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_gid=socket_gid)

    def execute(program, command, env):
        assert (program, command) == ("python", ["python", "-m", "updater.app"])
        assert env["HOME"] == "/home/appuser"
        calls.append("exec")
        raise Executed

    with patch.object(USER.pwd, "getpwnam", return_value=account), \
            patch.object(USER.os, "stat", return_value=socket,
                         side_effect=FileNotFoundError if socket_gid is None else None), \
            patch.object(USER.os, "setgroups", side_effect=lambda x: calls.append(("groups", x))), \
            patch.object(USER.os, "setgid", side_effect=lambda x: calls.append(("gid", x))), \
            patch.object(USER.os, "setuid", side_effect=lambda x: calls.append(("uid", x))), \
            patch.object(USER.os, "execvpe", side_effect=execute), \
            patch.dict(os.environ):
        with pytest.raises(Executed):
            USER.main(["python", "-m", "updater.app"])
    groups = sorted({1500} if socket_gid is None else {1500, socket_gid})
    assert calls == [("groups", groups), ("gid", 1500), ("uid", 1500), "exec"]


@pytest.mark.parametrize("failure", ["stat", "setgroups", "setgid", "setuid"])
def test_failed_socket_or_privilege_setup_never_executes(failure):
    account = SimpleNamespace(pw_uid=1500, pw_gid=1500, pw_dir="/home/appuser")
    socket = SimpleNamespace(st_mode=stat.S_IFSOCK, st_gid=23719)
    with patch.object(USER.pwd, "getpwnam", return_value=account), \
            patch.object(USER.os, "stat", return_value=socket) as read_socket, \
            patch.object(USER.os, "setgroups") as groups, \
            patch.object(USER.os, "setgid") as gid, \
            patch.object(USER.os, "setuid") as uid, \
            patch.object(USER.os, "execvpe") as execute:
        {"stat": read_socket, "setgroups": groups, "setgid": gid,
         "setuid": uid}[failure].side_effect = PermissionError("synthetic denial")
        with pytest.raises(PermissionError):
            USER.main(["python", "-m", "updater.app"])
        execute.assert_not_called()


def test_wrong_socket_type_and_missing_command_stop_startup():
    with patch.object(USER.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
            patch.object(USER.pwd, "getpwnam", return_value=SimpleNamespace(pw_gid=1500)), \
            patch.object(USER.os, "setgroups") as groups, \
            patch.object(USER.os, "execvpe") as execute:
        for command in ([], ["python"]):
            with pytest.raises(SystemExit):
                USER.main(command)
        groups.assert_not_called()
        execute.assert_not_called()


@pytest.mark.parametrize("fail_chown", [False, True])
def test_shell_bootstrap_preserves_key_or_stops_before_command(tmp_path, fail_chown):
    """Execute the production shell with only its fixed paths remapped."""
    ssh = tmp_path / "ssh"
    ssh.mkdir()
    key = ssh / "backup_key"
    key.write_text("synthetic key fixture, never a real credential\n")
    key.chmod(0o644)
    before = key.read_bytes()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    trace = tmp_path / "trace"
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, source in {
        "chown": "#!/bin/sh\nexit " + ("7" if fail_chown else "0") + "\n",
        "python3": '#!/bin/sh\nprintf "%s\\n" "$@" > "$TRACE"\n',
    }.items():
        path = tools / name
        path.write_text(source)
        path.chmod(0o755)
    shell = (ROOT / "entrypoint.sh").read_text()
    for original, replacement in (("/app/repo", repo), ("/app/.ssh", ssh)):
        shell = shell.replace(original, str(replacement))
    script = tmp_path / "entrypoint.sh"
    script.write_text(shell)
    env = {"PATH": str(tools) + ":/usr/bin:/bin", "TRACE": str(trace)}
    result = subprocess.run(["/bin/sh", str(script), "python", "-m", "updater.app"],
                            env=env, capture_output=True, text=True)
    assert key.read_bytes() == before
    if fail_chown:
        assert result.returncode == 7
        assert not trace.exists()
    else:
        assert result.returncode == 0
        assert stat.S_IMODE(ssh.stat().st_mode) == 0o700
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert trace.read_text().splitlines() == [
            "/container_user.py", "python", "-m", "updater.app"
        ]


def test_image_prepares_git_trust_without_runtime_config_writes():
    source = (ROOT / "Dockerfile").read_text()
    assert "RUN git config --system --add safe.directory /app/repo" in source
    assert "COPY scripts/container_user.py /container_user.py" in source
    shell = (ROOT / "entrypoint.sh").read_text()
    assert not any(command in shell for command in ("groupadd", "usermod", "git config"))
