"""Non-blocking live-dev coverage for SSO/OIDC configuration."""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_sso]


def test_oidc_config_and_login_affordance(session, base_url, oidc_test_config):
    update_resp = session.put("/api/auth/oidc", json=oidc_test_config)
    assert update_resp.status_code == 200, update_resp.text[:300]

    get_resp = session.get("/api/auth/oidc")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["enabled"] is True
    assert data["provider_url"] == oidc_test_config["provider_url"]
    assert data["client_id"] == oidc_test_config["client_id"]
    assert data["redirect_uri"] == oidc_test_config["redirect_uri"]
    assert data["allowed_group"] == oidc_test_config["allowed_group"]
    assert data["admin_group"] == oidc_test_config["admin_group"]

    discovery_resp = session.post("/api/auth/test-oidc")
    assert discovery_resp.status_code == 200
    assert discovery_resp.json()["success"] is True

    fresh = httpx.Client(base_url=base_url, verify=False, timeout=30.0, follow_redirects=False)
    try:
        login_page = fresh.get("/login")
        assert login_page.status_code == 200
        assert "Sign in with SSO" in login_page.text

        oidc_login = fresh.get("/auth/oidc/login")
        assert oidc_login.status_code in (302, 307)
        assert oidc_login.headers["location"] != "/login"
    finally:
        fresh.close()
