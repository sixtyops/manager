"""Exercise the dev release command with local command stubs."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-dev.sh"
SHA = "a" * 40
TAG = "v1.4.1-dev6"


def release_run(tmp_path: Path, *, main_sha: str = SHA, ci: bool = True,
                version: str = "1.4.1-dev6", tag_exists: bool = False,
                verify: bool = True, yes: bool = True,
                ls_remote_error: bool = False):
    """Run the script with deterministic git, gh, and gpg stubs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    git = r'''#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$COMMAND_LOG"
case "$1" in
  fetch) exit 0 ;;
  rev-parse) printf '%s\n' "$MAIN_SHA" ;;
  show-ref) [[ "$TAG_EXISTS" == true ]] ;;
  ls-remote)
    if [[ "$LS_REMOTE_ERROR" == true ]]; then exit 128; fi
    if [[ "$TAG_EXISTS" == true ]]; then
      printf 'object refs/tags/v1.4.1-dev6\n'
      exit 0
    fi
    exit 2
    ;;
  show)
    if [[ "$2" == *updater/__init__.py ]]; then
      printf '__version__ = "%s"\n' "$VERSION"
    else
      printf '<html>install</html>\n'
    fi
    ;;
  describe) printf 'v1.4.1-dev5\n' ;;
  log) printf 'abc123 change\n' ;;
  tag) gpg; exit 0 ;;
  *)
    if [[ "$*" == *"verify-tag "* ]]; then
      [[ "$VERIFY_TAG" == true ]]
    else
      exit 0
    fi
    ;;
esac
'''
    gh = r'''#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$COMMAND_LOG"
case "$1" in
  run)
    if [[ "$2" == list ]]; then
      if [[ "$*" == *'--workflow CI '* ]]; then
        [[ "$CI_PASSED" == true ]] && printf '123\n'
      elif [[ "$*" == *"headBranch == \"$RELEASE_TAG\""* && "$*" == *"fromdateiso8601"* ]]; then
        printf '456\n'
      else
        printf '455\n'
      fi
    fi
    ;;
  *) exit 0 ;;
esac
exit 0
'''
    gpg = '#!/usr/bin/env bash\nprintf "gpg\\n" >> "$COMMAND_LOG"\n'
    for name, source in {"git": git, "gh": gh, "gpg": gpg}.items():
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o700)

    environment = {
        "PATH": f"{bin_dir}:{os.defpath}",
        "COMMAND_LOG": str(log),
        "MAIN_SHA": main_sha,
        "CI_PASSED": str(ci).lower(),
        "VERSION": version,
        "RELEASE_TAG": f"v{version}",
        "TAG_EXISTS": str(tag_exists).lower(),
        "LS_REMOTE_ERROR": str(ls_remote_error).lower(),
        "VERIFY_TAG": str(verify).lower(),
    }
    args = ["bash", str(SCRIPT), SHA]
    if yes:
        args.append("--yes")
    result = subprocess.run(args, cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=10)
    return result, log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(("case", "expected"), [
    ("wrong_head", "not the current origin/main head"),
    ("ci", "CI has not passed"),
    ("version", "does not have a -devN version"),
    ("tag", "already exists"),
    ("verify", "Signature verification failed"),
])
def test_refuses_invalid_release_state(case, expected, tmp_path):
    options = {
        "wrong_head": {"main_sha": "b" * 40},
        "ci": {"ci": False},
        "version": {"version": "1.4.1"},
        "tag": {"tag_exists": True},
        "verify": {"verify": False},
    }[case]
    result, commands = release_run(tmp_path, **options)
    assert result.returncode == 1
    assert expected in result.stderr
    assert not any(line.startswith("git push ") for line in commands)


def test_happy_path_signs_verifies_and_pushes_tag_by_name(tmp_path):
    result, commands = release_run(tmp_path)
    assert result.returncode == 0, result.stderr

    signing = next(i for i, line in enumerate(commands) if " tag -s " in line)
    verifying = next(i for i, line in enumerate(commands) if " verify-tag " in line)
    pushing = next(i for i, line in enumerate(commands) if line.startswith("git push "))
    assert signing < verifying < pushing
    assert commands[signing] == (
        "git -c gpg.format=openpgp -c "
        "user.signingkey=E2C19E86B24A87283068AEFAF2D6F70883960DCE "
        f"tag -s {TAG} -m {TAG} origin/main"
    )
    assert commands[verifying] == f"git -c gpg.format=openpgp verify-tag {TAG}"
    assert commands[pushing] == f"git push origin refs/tags/{TAG}"
    assert "gh run watch --exit-status 456" in commands


def test_remote_tag_lookup_error_stops_before_signing(tmp_path):
    result, commands = release_run(tmp_path, ls_remote_error=True)

    assert result.returncode == 1
    assert "Could not check whether tag" in result.stderr
    assert "gpg" not in commands
    assert not any(" tag -s " in line for line in commands)


def test_release_run_for_another_tag_is_ignored(tmp_path):
    result, commands = release_run(tmp_path)

    assert result.returncode == 0, result.stderr
    release_lookup = next(line for line in commands if "--workflow Release" in line)
    assert f'headBranch == "{TAG}"' in release_lookup
    assert "fromdateiso8601" in release_lookup
    assert "gh run watch --exit-status 456" in commands
    assert "gh run watch --exit-status 455" not in commands
