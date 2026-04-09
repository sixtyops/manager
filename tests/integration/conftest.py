"""Shared fixtures for integration tests against a live SixtyOps instance.

Usage:
    SIXTYOPS_TEST_URL=https://sixtyops-dev.infra.treehouse.mn pytest -m integration -v
"""

import os
import ssl

import httpx
import pytest

SIXTYOPS_TEST_URL = os.environ.get("SIXTYOPS_TEST_URL", "").rstrip("/")
SIXTYOPS_TEST_USER = os.environ.get("SIXTYOPS_TEST_USER", "admin")
SIXTYOPS_TEST_PASS = os.environ.get("SIXTYOPS_TEST_PASS", "admin")


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when SIXTYOPS_TEST_URL is not set."""
    if SIXTYOPS_TEST_URL:
        return
    skip = pytest.mark.skip(reason="SIXTYOPS_TEST_URL not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url():
    """Base URL for the SixtyOps instance under test."""
    if not SIXTYOPS_TEST_URL:
        pytest.skip("SIXTYOPS_TEST_URL not set")
    return SIXTYOPS_TEST_URL


@pytest.fixture(scope="session")
def session(base_url):
    """Authenticated httpx client with session cookie.

    Uses a permissive SSL context to accept self-signed certs on dev servers.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    client = httpx.Client(
        base_url=base_url,
        verify=ssl_ctx,
        timeout=30.0,
        follow_redirects=True,
    )

    # Login via form POST
    resp = client.post("/login", data={
        "username": SIXTYOPS_TEST_USER,
        "password": SIXTYOPS_TEST_PASS,
    })
    assert resp.status_code == 200, f"Login failed: {resp.status_code} {resp.text[:200]}"
    assert "session_id" in client.cookies, "No session cookie after login"

    yield client
    client.close()


@pytest.fixture(scope="session")
def topology(session):
    """Fetch the current topology once per session."""
    resp = session.get("/api/topology")
    assert resp.status_code == 200
    data = resp.json()
    assert "sites" in data
    return data


@pytest.fixture(scope="session")
def test_ap(topology):
    """Pick the first enabled AP from the topology."""
    for site in topology.get("sites", []):
        for ap in site.get("aps", []):
            if ap.get("enabled", True):
                return ap
    pytest.skip("No enabled APs found on the dev server")


@pytest.fixture(scope="session")
def test_switch(topology):
    """Pick the first enabled switch from the topology."""
    for site in topology.get("sites", []):
        for sw in site.get("switches", []):
            if sw.get("enabled", True):
                return sw
    pytest.skip("No enabled switches found on the dev server")
