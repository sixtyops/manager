"""Tests for OpenAPI documentation."""

import pytest
from fastapi.testclient import TestClient


class TestOpenAPISchema:
    """Test OpenAPI schema generation."""

    def test_openapi_json_accessible(self, authed_client):
        resp = authed_client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["info"]["title"] == "SixtyOps Firmware Updater"
        assert "version" in schema["info"]
        assert "description" in schema["info"]

    def test_openapi_has_tags(self, authed_client):
        resp = authed_client.get("/openapi.json")
        schema = resp.json()
        tag_names = [t["name"] for t in schema.get("tags", [])]
        assert "devices" in tag_names
        assert "firmware" in tag_names
        assert "jobs" in tag_names
        assert "settings" in tag_names
        assert "analytics" in tag_names
        assert "auth" in tag_names

    def test_openapi_paths_have_tags(self, authed_client):
        resp = authed_client.get("/openapi.json")
        schema = resp.json()
        paths = schema.get("paths", {})
        # Spot-check a few endpoints
        ap_path = paths.get("/api/access-points", {})
        if "get" in ap_path:
            assert "devices" in ap_path["get"].get("tags", [])

        analytics_path = paths.get("/api/analytics/summary", {})
        if "get" in analytics_path:
            assert "analytics" in analytics_path["get"].get("tags", [])

    def test_docs_page_requires_auth(self, client):
        """Swagger UI is auth-gated; unauthenticated callers get redirected
        (HTML accept) or 401 (API accept). Avoids anonymously exposing the
        list of every API route."""
        resp = client.get("/docs", follow_redirects=False)
        assert resp.status_code in (401, 303)

    def test_docs_page_accessible_when_authed(self, authed_client):
        resp = authed_client.get("/docs")
        assert resp.status_code == 200
        # Should reference the locally-vendored Swagger UI assets, not
        # cdn.jsdelivr.net — content blockers were dropping the CDN
        # request and leaving the page blank.
        assert "/static/vendor/swagger-ui-bundle.js" in resp.text
        assert "/static/vendor/swagger-ui.css" in resp.text
        assert "jsdelivr" not in resp.text

    def test_redoc_page_requires_auth(self, client):
        resp = client.get("/redoc", follow_redirects=False)
        assert resp.status_code in (401, 303)

    def test_redoc_page_accessible_when_authed(self, authed_client):
        resp = authed_client.get("/redoc")
        assert resp.status_code == 200
        assert "/static/vendor/redoc.standalone.js" in resp.text
        assert "jsdelivr" not in resp.text

    def test_openapi_json_requires_auth(self, client):
        resp = client.get("/openapi.json", follow_redirects=False)
        assert resp.status_code in (401, 303)

    def test_all_api_routes_tagged(self, authed_client):
        """Verify that most /api/ routes have tags."""
        resp = authed_client.get("/openapi.json")
        schema = resp.json()
        untagged = []
        for path, methods in schema.get("paths", {}).items():
            if not path.startswith("/api/"):
                continue
            for method, details in methods.items():
                if method in ("get", "post", "put", "delete", "patch"):
                    if not details.get("tags"):
                        untagged.append(f"{method.upper()} {path}")
        # Allow a few untagged, but most should be tagged
        assert len(untagged) <= 5, f"Too many untagged API routes: {untagged}"

    def test_api_doc_matches_routes(self):
        """Guard `docs/api.md` against drifting from the real route table.

        Every in-schema HTTP route (and the /ws WebSocket) must have a matching
        ``### `METHOD /path` `` heading in docs/api.md, and every documented
        heading must correspond to a real route. Catches both undocumented
        endpoints and stale doc entries. The /docs, /redoc, /openapi.json meta
        endpoints are include_in_schema=False and excluded; the /static mount
        is not an APIRoute and is skipped.
        """
        import re
        from pathlib import Path

        from fastapi.routing import APIRoute
        from starlette.routing import WebSocketRoute

        from updater.app import app

        HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

        def normalize(path: str) -> str:
            # Strip FastAPI path converters: {filename:path} -> {filename}
            return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)

        # Documented endpoints from the Markdown reference.
        doc_path = Path(__file__).resolve().parent.parent / "docs" / "api.md"
        documented = set()
        for m in re.finditer(
            r"^###\s+`(GET|POST|PUT|DELETE|PATCH|WebSocket)\s+(\S+)`",
            doc_path.read_text(),
            re.MULTILINE,
        ):
            documented.add((m.group(1).upper(), normalize(m.group(2))))

        # Actual routes from the live app.
        actual = set()
        for route in app.routes:
            if isinstance(route, APIRoute):
                if not route.include_in_schema:
                    continue
                for method in (route.methods or set()) & HTTP_METHODS:
                    actual.add((method, normalize(route.path)))
            elif isinstance(route, WebSocketRoute):
                actual.add(("WEBSOCKET", normalize(route.path)))

        undocumented = sorted(f"{mtd} {p}" for mtd, p in actual - documented)
        stale = sorted(f"{mtd} {p}" for mtd, p in documented - actual)

        assert not undocumented, (
            "Routes missing from docs/api.md (document them, or set "
            "include_in_schema=False if intentionally internal):\n  "
            + "\n  ".join(undocumented)
        )
        assert not stale, (
            "docs/api.md documents routes that don't exist (fix or remove):\n  "
            + "\n  ".join(stale)
        )
