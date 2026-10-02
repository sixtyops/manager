"""Check release inputs with local shell fragments. No release actions run."""

import os
from pathlib import Path
import re
import subprocess

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
SOURCE = WORKFLOW.read_text()
JOBS = dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)",
                       SOURCE.split("jobs:\n", 1)[1], re.M | re.S))


def script(job: str, name: str) -> str:
    """Extract one named shell step and reject template expansion before execution."""
    step = JOBS[job].split(f"      - name: {name}\n", 1)[1]
    step = re.split(r"^      - ", step, maxsplit=1, flags=re.M)[0]
    body = step.split("        run: |\n", 1)[1]
    assert "${{" not in body
    return "\n".join(line[10:] for line in body.splitlines())


def run_script(body: str, directory: Path, **variables: str) -> subprocess.CompletedProcess:
    # Use a small environment. Inputs enter through env, never through shell source.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", body],
        cwd=directory, env={"PATH": os.defpath, **variables},
        capture_output=True, text=True, timeout=5,
    )


HOSTILE = [
    "", "--help", "refs/tags/v1.4.1", "main", "v1.4", "v1.4.1-dev",
    "v1.4.1-dev1-extra", "v1.4.1; touch injected", 'v1.4.1"; touch injected #',
    "$(touch injected)", "`touch injected`", "v1.4.1\nname=injected",
    "v1.4.1\r", "v1.4.1/../../main", "v1.4.1 dev1", "v１.4.1",
]


@pytest.mark.parametrize("job", ["test", "release", "docker-push"])
@pytest.mark.parametrize("event", ["workflow_dispatch", "push"])
@pytest.mark.parametrize("tag", HOSTILE)
def test_hostile_tag_is_rejected(job, event, tag, tmp_path):
    result = run_script(script(job, "Validate release tag"), tmp_path,
                        RELEASE_EVENT=event, RELEASE_TAG=tag)
    assert result.returncode == 1
    assert tag not in result.stdout if tag else True
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize("job", ["test", "release", "docker-push"])
@pytest.mark.parametrize("event,tag,accepted", [
    ("workflow_dispatch", "v1.3.1", True),
    ("workflow_dispatch", "v1.4.1", True),
    ("workflow_dispatch", "v12.34.56", True),
    ("push", "v1.3.1-dev1", True),
    ("push", "v1.3.1-dev23", True),
    ("push", "v1.4.1-dev5", True),
    ("push", "v12.34.56-dev789", True),
    ("workflow_dispatch", "v1.4.1-dev5", False),
    ("push", "v1.4.1", False),
    ("pull_request", "v1.4.1", False),
])
def test_supported_tags_and_events(job, event, tag, accepted, tmp_path):
    result = run_script(script(job, "Validate release tag"), tmp_path,
                        RELEASE_EVENT=event, RELEASE_TAG=tag)
    assert (result.returncode == 0) == accepted


@pytest.mark.parametrize("confirmation", ["RELEASE", "release", "", "RELEASE\n", *HOSTILE])
def test_exact_confirmation_required(confirmation, tmp_path):
    result = run_script(script("validate-dispatch", "Validate confirmation"), tmp_path,
                        RELEASE_CONFIRM=confirmation)
    assert (result.returncode == 0) == (confirmation == "RELEASE")
    assert not (tmp_path / "injected").exists()


def test_validation_precedes_every_checkout_and_shell_has_no_templates():
    assert "RELEASE_CONFIRM: ${{ inputs.confirm }}" in SOURCE
    assert "RELEASE_TAG: ${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name }}" in SOURCE
    for job in ("test", "release", "docker-push"):
        steps = JOBS[job].split("    steps:\n", 1)[1]
        assert steps.startswith("      - name: Validate release tag\n")
        assert steps.count("uses: actions/checkout@v4") == 1
        assert "ref: refs/tags/${{ env.RELEASE_TAG }}" in steps
        assert "continue-on-error" not in steps
    for body in re.findall(r"^        run: \|\n((?:^          .*\n|^\n)*)", SOURCE, re.M):
        assert "${{" not in body


def test_job_and_permission_gates_are_retained():
    assert "    if: github.event_name == 'workflow_dispatch'" in JOBS["validate-dispatch"]
    assert "    needs: [validate-dispatch]" in JOBS["test"]
    assert "    if: always() && (needs.validate-dispatch.result == 'success' || needs.validate-dispatch.result == 'skipped')" in JOBS["test"]
    assert "    needs: [test]" in JOBS["release"]
    assert "    if: always() && needs.test.result == 'success'" in JOBS["release"]
    assert "    permissions:\n      contents: write\n" in JOBS["release"]
    assert "    needs: [release]" in JOBS["docker-push"]
    assert "    if: always() && needs.release.result == 'success'" in JOBS["docker-push"]
    assert "    permissions:\n      contents: read\n      packages: write\n" in JOBS["docker-push"]
    steps = JOBS["release"]
    assert steps.index("Verify release tag is signed") < steps.index("Determine release type") < steps.index("Create GitHub Release")
    assert "continue-on-error" not in steps
    assert "fetch-depth: 0" in steps


@pytest.mark.parametrize("tag,verify_status,expected", [
    ("v1.3.1-dev23", "1", 0),
    ("v1.4.1", "0", 0), ("v1.4.1", "1", 1),
    ("v1.4.1-dev5", "0", 0), ("v1.4.1-dev5", "1", 1),
])
def test_signing_gate_fails_closed_with_mock_commands(tag, verify_status, expected, tmp_path):
    # These fixed local stubs cannot fetch tags, import keys, or publish artifacts.
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "git": 'printf "%s\\n" "$*" >> "$CALL_LOG"\nif [ "$1" = "fetch" ]; then exit 0; fi\nexit "$VERIFY_STATUS"\n',
        "gpg": 'printf "gpg\\n" >> "$CALL_LOG"\n',
        "mktemp": 'mkdir "$KEY_DIR"\nprintf "%s\\n" "$KEY_DIR"\n',
    }.items():
        path = commands / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
    log = tmp_path / "calls"
    result = run_script(script("release", "Verify release tag is signed by a trusted key"),
                        tmp_path, PATH=f"{commands}:{os.defpath}", RELEASE_TAG=tag,
                        VERIFY_STATUS=verify_status, CALL_LOG=str(log),
                        KEY_DIR=str(tmp_path / "keys"))
    assert result.returncode == expected
    if tag.startswith("v1.3."):
        assert not log.exists()
    else:
        assert log.read_text().splitlines() == [
            f"fetch origin --force refs/tags/{tag}:refs/tags/{tag}",
            "gpg", f"-c gpg.format=openpgp verify-tag {tag}",
        ]


@pytest.mark.parametrize("event,tag,prerelease,name,floating", [
    ("workflow_dispatch", "v1.4.1", "false", "v1.4.1", "latest"),
    ("push", "v1.4.1-dev5", "true", "v1.4.1-dev5 (dev)", "dev"),
])
def test_release_and_image_outputs(event, tag, prerelease, name, floating, tmp_path):
    output = tmp_path / "output"
    variables = dict(RELEASE_EVENT=event, RELEASE_TAG=tag, GITHUB_OUTPUT=str(output))
    assert run_script(script("release", "Determine release type"), tmp_path, **variables).returncode == 0
    assert output.read_text() == f"is_prerelease={prerelease}\ntag={tag}\nname={name}\n"
    output.write_text("")
    assert run_script(script("docker-push", "Determine image tags"), tmp_path, **variables).returncode == 0
    assert output.read_text() == f"tags=ghcr.io/sixtyops/manager:{tag},ghcr.io/sixtyops/manager:{floating}\n"
    assert "body_path: ${{ steps.meta.outputs.is_prerelease == 'true' && '/tmp/release-notes.md' || '' }}" in SOURCE
    assert "generate_release_notes: ${{ steps.meta.outputs.is_prerelease == 'false' }}" in SOURCE
