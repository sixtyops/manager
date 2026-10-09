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
FETCH_OPTIONS = re.compile(
    r"^\s*,\s*\{(?P<options>.*?)\}\s*\)(?=\s*(?:;|\.))", re.S
)
FETCH_METHOD = re.compile(r"^\s*,\s*\{\s*method\s*:\s*(['\"])(GET|POST|PUT|DELETE|PATCH)\1")
COMMENT_OR_SPACE = r"(?:\s|/\*[\s\S]*?\*/|//[^\n]*)*"
METHOD_PROPERTY = re.compile(
    r"(?<![\w$])(?:method|['\"]method['\"])" + COMMENT_OR_SPACE + r"(?=:|,|})|"
    r"\[[^]\n]+\]\s*:|(?:get|set)\s+method\s*\(|__proto__\s*:"
)
BODY_PAYLOAD_SPREAD = re.compile(
    r"body\s*:\s*JSON\.stringify\(\{name,\s*category,\s*\.\.\.payload\}\)"
)
WINDOW_OPEN_START = re.compile(r"\bwindow\.open\s*\(")
WINDOW_OPEN_ARGUMENT = re.compile(
    r"\bwindow\.open\s*\(\s*(?P<url>`[^`\n]*`|'[^'\n]*'|\"[^\"\n]*\")\s*,"
)
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

    The template uses quoted URLs, one encoded filename suffix, and one
    conditional that selects the AP or switch route. This test scans only
    `monitor.html`. It does not scan linked JavaScript files. Extend it if UI
    API calls move to those files. Other API text is not a request:
    `startsWith('/api/')` checks response scope, while comments name routes.
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
        boundary = re.match(r"\s*([,)])", tail)
        assert boundary, (
            "Unknown fetch URL tail at template line "
            f"{source.count(chr(10), 0, fetch.start()) + 1}"
        )
        if boundary.group(1) == ")":
            method = "GET"
        else:
            options_match = FETCH_OPTIONS.match(tail)
            assert options_match, (
                "Unknown fetch options form at template line "
                f"{source.count(chr(10), 0, fetch.start()) + 1}"
            )
            options = options_match.group("options")
            method_properties = list(METHOD_PROPERTY.finditer(options))
            safe_body_spreads = list(BODY_PAYLOAD_SPREAD.finditer(options))
            assert len(safe_body_spreads) <= 1, "Fetch options use an unknown body spread"
            options_without_body_spread = BODY_PAYLOAD_SPREAD.sub("", options)
            assert "..." not in options_without_body_spread, (
                "Fetch options use an unsupported spread"
            )
            if method_properties:
                method_match = FETCH_METHOD.match(tail)
                assert method_match and len(method_properties) == 1, (
                    "Fetch options have an unsupported or duplicate method"
                )
                method = method_match.group(2)
            else:
                method = "GET"
        requests.update((method, path) for path in _request_path(expression, endpoint_values))

    # Browser navigations that request API paths: portal links, CSV export,
    # and config downloads. Their HTTP method is GET.
    window_opens = list(WINDOW_OPEN_START.finditer(source))
    for opened in window_opens:
        match = WINDOW_OPEN_ARGUMENT.match(source, opened.start())
        assert match, (
            "Unknown window.open URL tail at template line "
            f"{source.count(chr(10), 0, opened.start()) + 1}"
        )
        expression = match.group("url")
        request_spans.append(match.span("url"))
        requests.update(("GET", path) for path in _request_path(expression, endpoint_values))

    navigation_patterns = (
        re.compile(r"\bwindow\.location\.href\s*=\s*(?P<url>'/api/[^'\n]*'|\"/api/[^\"\n]*\")"),
        re.compile(r"\bhref=(?P<quote>['\"])(?P<path>/api/[^'\"]+)(?P=quote)"),
    )
    for pattern in navigation_patterns:
        for match in pattern.finditer(source):
            if "path" in match.groupdict():
                expression = match.group("quote") + match.group("path") + match.group("quote")
                request_spans.append(match.span("path"))
            else:
                expression = match.group("url")
                request_spans.append(match.span("url"))
                assert re.match(r"\s*;", source[match.end():]), (
                    "Unknown window.location URL tail at template line "
                    f"{source.count(chr(10), 0, match.start()) + 1}"
                )
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
    """Match all 100 inline request pairs in source and rendered page to OpenAPI."""
    response = authed_client.get("/openapi.json")
    assert response.status_code == 200

    page = authed_client.get("/")
    assert page.status_code == 200
    source_requests = _ui_requests(TEMPLATE.read_text())
    rendered_requests = _ui_requests(page.text)
    assert len(source_requests) == 100
    assert rendered_requests == source_requests
    routes = _openapi_routes(response.json())
    _assert_requests_match_routes(source_requests, routes)


def test_ui_request_extractor_rejects_unknown_fetch_expression():
    source = TEMPLATE.read_text() + "\nfetch(requestUrl);\n"
    with pytest.raises(AssertionError, match="Unknown fetch URL form"):
        _ui_requests(source)


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (
            "fetch('/api/license')",
            "fetch('/api/license' + '/removed-test-route')",
            "Unknown fetch URL tail",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license'.replace('license', 'removed-test-route'))",
            "Unknown fetch URL tail",
        ),
        (
            "window.open(`/api/configs/${ip}/download/${configId}`, '_blank');",
            "window.open(`/api/configs/${ip}/download/${configId}` + '/removed-test-route', '_blank');",
            "Unknown window.open URL tail",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method: 'GET', method: 'POST' })",
            "duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method /* reason */: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method: 'GET', method /* reason */: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method // reason\n: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { 'method' /* reason */: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { 'method' // reason\n: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method: 'GET', 'method' /* reason */: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { headers: {}, method: 'POST' })",
            "unsupported or duplicate method",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method: 'GET', ...options })",
            "unsupported spread",
        ),
        (
            "fetch('/api/license')",
            "fetch('/api/license', { method: 'GET', ['method']: 'POST' })",
            "unsupported or duplicate method",
        ),
    ],
)
def test_real_template_mutations_fail_closed(before, after, message):
    source = TEMPLATE.read_text()
    assert source.count(before) == 1
    mutated = source.replace(before, after, 1)
    with pytest.raises(AssertionError, match=message):
        _ui_requests(mutated)


def test_real_window_open_path_suffix_fails_route_check():
    source = TEMPLATE.read_text()
    before = "window.open(`/api/configs/${ip}/download/${configId}`, '_blank');"
    after = "window.open(`/api/configs/${ip}/download/${configId}/removed-test-route`, '_blank');"
    assert source.count(before) == 1
    mutated = source.replace(before, after, 1)
    with pytest.raises(AssertionError, match="UI requests missing from OpenAPI"):
        _assert_requests_match_routes(_ui_requests(mutated), _openapi_routes(_schema_for_routes()))


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
