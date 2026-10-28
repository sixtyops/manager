"""Offline tests for release validator API route checks."""

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi.routing import APIRoute

from updater.app import app


def _load_validator_module():
    requests_stub = ModuleType("requests")
    requests_stub.Response = type("Response", (), {})
    requests_stub.Session = type("Session", (), {})
    urllib3_stub = ModuleType("urllib3")
    urllib3_stub.disable_warnings = lambda warning: None
    urllib3_stub.exceptions = SimpleNamespace(InsecureRequestWarning=RuntimeWarning)
    with patch.dict(sys.modules, {"requests": requests_stub, "urllib3": urllib3_stub}):
        return importlib.import_module("scripts.validate_release")


def test_analytics_checks_match_current_get_routes() -> None:
    registered_get_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in (route.methods or set())
    }
    requested_paths: list[str] = []

    class FakeResponse:
        status_code: int
        url: str

    validate_release = _load_validator_module()
    validator = validate_release.ReleaseValidator("http://manager.test")

    def fake_request(method: str, url: str, **kwargs: object) -> FakeResponse:
        from urllib.parse import urlparse

        path = urlparse(url).path
        requested_paths.append(path)
        response = FakeResponse()
        response.status_code = 200 if method == "GET" and path in registered_get_paths else 404
        response.url = url
        return response

    validator.session.request = fake_request  # type: ignore[method-assign]
    result = validate_release.TestResult()

    validator.test_analytics(result)

    assert set(requested_paths) <= registered_get_paths
    assert len(requested_paths) == 4
    assert result.passed == len(requested_paths)
    assert result.failed == 0


def test_validator_refuses_password_argument(monkeypatch) -> None:
    validate_release = _load_validator_module()
    monkeypatch.setattr(
        sys, "argv",
        ["validate_release.py", "--host", "https://manager.test", "--password", "synthetic"],
    )
    monkeypatch.setenv("RELEASE_VALIDATION_PASSWORD", "synthetic")

    try:
        validate_release.main()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("password command-line argument was accepted")


def test_documented_password_file_usage_keeps_password_out_of_argv() -> None:
    sop = Path("docs/release-sop.md").read_text(encoding="utf-8")
    validator_command = next(
        line for line in sop.splitlines()
        if line.startswith("python scripts/validate_release.py")
    )

    assert "--password" not in validator_command
    assert "RELEASE_VALIDATION_PASSWORD_FILE" in sop
    assert "--host" not in validator_command
    assert "--username" not in validator_command


def test_release_workflow_passes_secret_settings_only_through_environment() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Validate development instance\n", 1)[1]
    env_block, run_block = step.split("        run: |\n", 1)
    run_block = run_block.split("\n      - name:", 1)[0]

    secret_bindings = {
        "RELEASE_VALIDATION_HOST": "SIXTYOPS_TEST_URL",
        "RELEASE_VALIDATION_USERNAME": "SIXTYOPS_TEST_USER",
        "RELEASE_VALIDATION_PASSWORD": "SIXTYOPS_TEST_PASS",
        "RELEASE_VALIDATION_DEVICE_IP": "SIXTYOPS_TEST_AP_IP",
        "RELEASE_VALIDATION_SWITCH_IP": "SIXTYOPS_TEST_SWITCH_IP",
        "RELEASE_VALIDATION_SKIP": "SIXTYOPS_RELEASE_VALIDATION_SKIP",
    }
    for env_name, secret_name in secret_bindings.items():
        assert f"{env_name}: ${{{{ secrets.{secret_name} }}}}" in env_block

    assert "${{ secrets." not in run_block
    assert 'python3 scripts/validate_release.py\n' in run_block


def test_cli_reads_release_settings_from_environment_without_printing_password(monkeypatch, capsys) -> None:
    validate_release = _load_validator_module()

    captured: dict[str, object] = {}

    class FakeValidator:
        CATEGORIES = validate_release.ReleaseValidator.CATEGORIES

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> bool:
            return True

    monkeypatch.setenv("RELEASE_VALIDATION_HOST", "https://dev.example.test")
    monkeypatch.setenv("RELEASE_VALIDATION_USERNAME", "release-check")
    monkeypatch.setenv("RELEASE_VALIDATION_PASSWORD", "test-password-value")
    monkeypatch.setenv("RELEASE_VALIDATION_DEVICE_IP", "192.0.2.10")
    monkeypatch.setenv("RELEASE_VALIDATION_SWITCH_IP", "192.0.2.11")
    monkeypatch.setenv("RELEASE_VALIDATION_SKIP", "radius,backup")
    monkeypatch.setattr(validate_release, "ReleaseValidator", FakeValidator)
    monkeypatch.setattr(sys, "argv", ["validate_release.py"])

    try:
        validate_release.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert captured["host"] == "https://dev.example.test"
    assert captured["username"] == "release-check"
    assert captured["password"] == "test-password-value"
    assert captured["device_ip"] == "192.0.2.10"
    assert captured["switch_ip"] == "192.0.2.11"
    assert captured["skip"] == {"radius", "backup"}
    assert "test-password-value" not in capsys.readouterr().out
