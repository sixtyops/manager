"""Offline tests for release validator API route checks."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi.routing import APIRoute

from updater.app import app


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

    class FakeSession:
        pass

    requests_stub = ModuleType("requests")
    requests_stub.Response = FakeResponse
    requests_stub.Session = FakeSession
    urllib3_stub = ModuleType("urllib3")
    urllib3_stub.disable_warnings = lambda warning: None
    urllib3_stub.exceptions = SimpleNamespace(InsecureRequestWarning=RuntimeWarning)

    with patch.dict(sys.modules, {"requests": requests_stub, "urllib3": urllib3_stub}):
        validate_release = importlib.import_module("scripts.validate_release")
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
