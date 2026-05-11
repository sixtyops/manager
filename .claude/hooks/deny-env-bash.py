#!/usr/bin/env python3
"""PreToolUse hook: block Bash commands that read .env / secret files.

Defense-in-depth for the Read/Edit deny rules in .claude/settings.json,
since file-tool deny rules do not cover Bash subprocesses (per
https://code.claude.com/docs/en/permissions).

Conservative by construction: only inspects Bash commands, falls open
on parse errors, and only blocks commands that explicitly reference
secret paths or run env-dumping commands. To disable, remove the hooks
block from .claude/settings.json.
"""
import json
import re
import sys


def deny(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return

    if data.get("tool_name") != "Bash":
        print(json.dumps({}))
        return

    cmd = data.get("tool_input", {}).get("command", "")

    if re.search(r'(?<![\w.])\.env(\.[\w.-]+)?(?!\w)', cmd):
        deny("Bash references a .env file; blocked by .claude/hooks/deny-env-bash.py.")

    if re.search(r'(?<![\w.])\.admin_password(?!\w)', cmd):
        deny("Bash references .admin_password; blocked by hook.")

    # Only match `env` / `printenv` at command-start positions (start of
    # string, after `;`, `|`, `&`, newline, backtick, or `$(`) so that
    # arguments and directory names like `python -m venv env` or `cd env`
    # are not mistaken for invocations of the env command.
    cmd_start = r'(?:^|[;|&\n`]|\$\()\s*'

    if re.search(cmd_start + r'printenv\b', cmd):
        deny("Bash 'printenv' is blocked (dumps environment).")

    for m in re.finditer(cmd_start + r'env\b(?:\s+([^\s|&;]+))?', cmd):
        first = m.group(1) or ""
        if "=" not in first:
            deny("Bash bare 'env' is blocked (dumps environment).")

    print(json.dumps({}))


if __name__ == "__main__":
    main()
