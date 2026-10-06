"""Render only the installer's service heredoc, without running the installer."""

import re
import subprocess
from pathlib import Path

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
CONTROLS = ("NoNewPrivileges", "PrivateTmp")


def _extract_service(source):
    matches = re.findall(
        r"^    cat > /etc/systemd/system/sixtyops\.service << EOF\n(.*?)^EOF$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert len(matches) == 1, "Expected one production service heredoc"
    body = matches[0]
    # Fail before shell execution if the fragment gains executable expansion.
    assert "`" not in body and "\\" not in body
    assert "$" not in body.replace("$INSTALL_DIR", "")
    assignments = re.findall(r"^INSTALL_DIR=.*$", source, re.MULTILINE)
    assert assignments == ['INSTALL_DIR="${SIXTYOPS_INSTALL_DIR:-/opt/sixtyops}"']
    return assignments[0], body


def _render_service(tmp_path, install_dir=None):
    assignment, body = _extract_service(INSTALLER.read_text())
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    if install_dir is not None:
        env["SIXTYOPS_INSTALL_DIR"] = install_dir
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c",
         assignment + "\ncat << EOF\n" + body + "EOF\n"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    unit = tmp_path / "sixtyops.service"
    unit.write_text(result.stdout)
    return unit.read_text()


def _sections(unit):
    sections = {}
    current = None
    for line in unit.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            assert current not in sections, "Duplicate section"
            sections[current] = []
        else:
            assert current is not None and "=" in line, "Invalid unit line"
            sections[current].append(tuple(line.split("=", 1)))
    return sections


def _assert_controls(unit):
    sections = _sections(unit)
    for control in CONTROLS:
        occurrences = [
            (section, value)
            for section, entries in sections.items()
            for key, value in entries if key == control
        ]
        assert occurrences == [("Service", "true")], control


@pytest.mark.parametrize("install_dir", [None, "/srv/sixtyops-test"])
def test_generated_service_security_and_compose_semantics(tmp_path, install_dir):
    unit = _render_service(tmp_path, install_dir)
    _assert_controls(unit)
    sections = _sections(unit)
    assert set(sections) == {"Unit", "Service", "Install"}
    assert sections["Unit"] == [
        ("Description", "SixtyOps Manager"),
        ("Requires", "docker.service"), ("After", "docker.service"),
    ]
    service = [(key, value) for key, value in sections["Service"]
               if key not in CONTROLS]
    compose = "/usr/bin/docker compose -f docker-compose.yml -f docker-compose.standalone.yml"
    assert service == [
        ("Type", "oneshot"), ("RemainAfterExit", "yes"),
        ("WorkingDirectory", install_dir or "/opt/sixtyops"),
        ("ExecStart", compose + " up -d"), ("ExecStop", compose + " down"),
    ]
    assert sections["Install"] == [("WantedBy", "multi-user.target")]


@pytest.mark.parametrize("control", CONTROLS)
@pytest.mark.parametrize("mutation", ["missing", "false", "duplicate", "misplaced"])
def test_security_assertions_reject_mutated_production_unit(tmp_path, control, mutation):
    unit = _render_service(tmp_path)
    directive = control + "=true\n"
    assert unit.count(directive) == 1
    if mutation == "missing":
        unit = unit.replace(directive, "")
    elif mutation == "false":
        unit = unit.replace(directive, control + "=false\n")
    elif mutation == "duplicate":
        unit = unit.replace(directive, directive * 2)
    else:
        unit = unit.replace(directive, "").replace("[Install]\n", "[Install]\n" + directive)
    with pytest.raises(AssertionError, match=control):
        _assert_controls(unit)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "expansion"])
def test_heredoc_extraction_fails_closed(mutation):
    source = INSTALLER.read_text()
    assignment, body = _extract_service(source)
    if mutation == "missing":
        source = source.replace("sixtyops.service << EOF", "another.service << EOF")
    elif mutation == "duplicate":
        source += "\n    cat > /etc/systemd/system/sixtyops.service << EOF\n" + body + "EOF\n"
    else:
        source = source.replace("Description=SixtyOps Manager", "Description=$(exit 1)")
    with pytest.raises(AssertionError):
        _extract_service(source)


def test_installer_bash_syntax_without_execution():
    subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-n", str(INSTALLER)],
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=True,
        capture_output=True, text=True,
    )
