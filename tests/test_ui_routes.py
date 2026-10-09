"""Check that shipped Manager UI API calls use routes in the app schema."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TEMPLATE = Path(__file__).resolve().parents[1] / "updater/templates/monitor.html"
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

FETCH_START = re.compile(r"\bfetch\s*\(")
URL_ARGUMENT = re.compile(
    r"\bfetch\s*\(\s*(?P<url>(?:'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`)"
    r"(?:\s*\+\s*encodeURIComponent\([^()\n]*\))?)"
)
FETCH_METHOD = re.compile(r"^\s*,\s*\{\s*method\s*:\s*(['\"])(GET|POST|PUT|DELETE|PATCH)\1")
FETCH_SIGNAL_ONLY = re.compile(r"^\s*,\s*\{\s*signal\s*:\s*AbortSignal\.timeout\(\d+\)\s*\}")
ENDPOINT_CHOICE = re.compile(
    r"\bconst\s+endpoint\s*=\s*type\s*===\s*(['\"])ap\1\s*\?\s*"
    r"(['\"])(aps)\2\s*:\s*(['\"])(switches)\4\s*;"
)


def _api_route(path: str) -> str:
    """Keep literal segments and replace each route parameter with `{}`."""
    path = re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)
    return re.sub(r"\{[^}/]+\}", "{}", path)


def _request_path(expression: str, endpoint_values: tuple[str, ...]) -> set[str]:
    """Normalize only the template forms used by the shipped UI."""
    if expression[0] in "'\"`":
        quote = expression[0]
        url = expression[1:expression.rfind(quote)]
        has_encoded_suffix = expression.endswith(")") and "+ encodeURIComponent(" in expression
    else:
        raise AssertionError(f"Unknown UI request URL expression: {expression}")

    if has_encoded_suffix:
        url += "{}"
    url = url.split("?", 1)[0]
    choices = endpoint_values if "${endpoint}" in url else (None,)
    paths = set()
    for endpoint in choices:
        candidate = url.replace("${endpoint}", endpoint) if endpoint else url
        candidate = re.sub(r"\$\{[^{}]+\}", "{}", candidate)
        if not candidate.startswith("/api/"):
            raise AssertionError(f"UI request is not a recognized local API path: {candidate}")
        paths.add(_api_route(candidate))
    return paths


def _ui_requests(source: str) -> set[tuple[str, str]]:
    """Extract supported fetch and browser-navigation API requests.

    The shipped template uses literal/template URLs, one encoded string
    suffix, and one AP/switch ternary. Other API text is not a request:
    `startsWith('/api/')` checks response scope, while comments name routes
    for explanation. The test does not treat those strings as callers.
    """
    endpoint_match = ENDPOINT_CHOICE.search(source)
    assert endpoint_match, "Could not read the shipped AP/switch endpoint choice"
    endpoint_values = (endpoint_match.group(3), endpoint_match.group(5))
    requests: set[tuple[str, str]] = set()
    request_spans = []

    fetches = list(FETCH_START.finditer(source))
    for fetch in fetches:
        match = URL_ARGUMENT.match(source, fetch.start())
        assert match, (
            "Unknown fetch URL form at template line "
            f"{source.count(chr(10), 0, fetch.start()) + 1}"
        )
        expression = match.group("url")
        request_spans.append(match.span("url"))
        tail = source[match.end():]
        method_match = FETCH_METHOD.match(tail)
        if method_match:
            method = method_match.group(2)
        elif FETCH_SIGNAL_ONLY.match(tail) or not re.match(r"\s*,", tail):
            method = "GET"
        else:
            raise AssertionError(
                "Unknown fetch options form at template line "
                f"{source.count(chr(10), 0, fetch.start()) + 1}"
            )
        requests.update((method, path) for path in _request_path(expression, endpoint_values))

    # Browser navigations that request API paths: portal links, CSV export,
    # and config downloads. Their HTTP method is GET.
    navigation_patterns = (
        re.compile(r"\bwindow\.open\(\s*(?P<url>`[^`\n]*`|'[^'\n]*'|\"[^\"\n]*\")"),
        re.compile(r"\bwindow\.location\.href\s*=\s*(?P<url>'/api/[^'\n]*'|\"/api/[^\"\n]*\")"),
        re.compile(r"\bhref=(?P<quote>['\"])(?P<path>/api/[^'\"]+)(?P=quote)"),
    )
    for pattern in navigation_patterns:
        for match in pattern.finditer(source):
            if "url" in match.groupdict():
                expression = match.group("url")
                request_spans.append(match.span("url"))
            else:
                expression = match.group("quote") + match.group("path") + match.group("quote")
                request_spans.append(match.span("path"))
            requests.update(("GET", path) for path in _request_path(expression, endpoint_values))

    # Every API path string must belong to a recognized caller, a known
    # comment, or the session-expiry API-prefix check. This makes a new
    # caller form fail instead of disappearing from the comparison.
    non_request_spans = [match.span() for match in re.finditer(r"(?m)(?:^\s*//|;\s*//)[^\n]*", source)]
    non_request_spans += [
        match.span("path")
        for match in re.finditer(
            r"url\.pathname\.startsWith\((?P<quote>['\"])(?P<path>/api/)(?P=quote)\)", source
        )
    ]
    for reference in re.finditer(r"/api/", source):
        spans = request_spans + non_request_spans
        assert any(start <= reference.start() < end for start, end in spans), (
            "Unclassified /api/ text at template line "
            f"{source.count(chr(10), 0, reference.start()) + 1}"
        )

    return requests


def _openapi_routes(schema: dict) -> set[tuple[str, str]]:
    routes = set()
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for method in methods:
            if method.upper() in HTTP_METHODS:
                routes.add((method.upper(), _api_route(path)))
    return routes


def _assert_requests_match_routes(
    requests: set[tuple[str, str]], routes: set[tuple[str, str]]
) -> None:
    missing = sorted(requests - routes)
    assert not missing, "UI requests missing from OpenAPI: " + ", ".join(
        f"{method} {path}" for method, path in missing
    )


def test_ui_api_requests_match_openapi(authed_client: TestClient):
    """Every supported UI API request must match an OpenAPI path and method."""
    response = authed_client.get("/openapi.json")
    assert response.status_code == 200

    requests = _ui_requests(TEMPLATE.read_text())
    routes = _openapi_routes(response.json())
    assert requests
    _assert_requests_match_routes(requests, routes)


def test_ui_request_extractor_rejects_unknown_fetch_expression():
    source = TEMPLATE.read_text() + "\nfetch(requestUrl);\n"
    with pytest.raises(AssertionError, match="Unknown fetch URL form"):
        _ui_requests(source)


def test_ui_route_contract_accepts_valid_and_rejects_stale_or_wrong_method():
    """Prove the same check catches a stale path and an invalid method."""
    routes = _openapi_routes(_schema_for_routes())
    valid = ("GET", _api_route("/api/license"))
    assert valid in routes
    _assert_requests_match_routes({valid}, routes)

    with pytest.raises(AssertionError, match="UI requests missing from OpenAPI"):
        _assert_requests_match_routes({("GET", "/api/removed-test-route")}, routes)
    with pytest.raises(AssertionError, match="UI requests missing from OpenAPI"):
        _assert_requests_match_routes({("POST", "/api/license")}, routes)


def _schema_for_routes() -> dict:
    """Return the app's local schema without starting lifespan services."""
    from updater.app import app

    return app.openapi()
