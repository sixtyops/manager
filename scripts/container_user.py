#!/usr/bin/env python3
"""Run the container command as appuser without writing account files."""

import os
import pwd
import stat
import sys


def main(command: list[str]) -> None:
    if not command:
        raise SystemExit("Container command is required")
    user = pwd.getpwnam("appuser")
    groups = {user.pw_gid}
    try:
        socket = os.stat("/var/run/docker.sock")
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(socket.st_mode):
            raise SystemExit("Docker socket mount is not a socket")
        groups.add(socket.st_gid)

    # Replace inherited groups before dropping the primary GID and UID.
    # Any failure stops startup before the application command can run.
    os.setgroups(sorted(groups))
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.environ["HOME"] = user.pw_dir
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main(sys.argv[1:])
