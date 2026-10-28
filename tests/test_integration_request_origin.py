"""Offline tests for shared live-integration request behavior."""

import httpx

from tests.integration.conftest import _set_request_origin


def test_state_changing_request_uses_configured_url_origin():
    request = httpx.Request("POST", "https://manager.example.test:8443/api/topology/refresh")

    _set_request_origin(request, "https://manager.example.test:8443/base/path")

    assert request.headers["Origin"] == "https://manager.example.test:8443"


def test_read_request_does_not_get_origin_header():
    request = httpx.Request("GET", "https://manager.example.test/api/topology")

    _set_request_origin(request, "https://manager.example.test")

    assert "Origin" not in request.headers
