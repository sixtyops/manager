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

    def test_docs_page_accessible(self):
        """Swagger UI should be accessible without auth."""
        from updater.app import app
        client = TestClient(app)
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_page_accessible(self):
        """ReDoc should be accessible without auth."""
        from updater.app import app
        client = TestClient(app)
        resp = client.get("/redoc")
        assert resp.status_code == 200

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
