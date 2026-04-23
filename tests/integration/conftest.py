"""Shared fixtures for integration tests against a live SixtyOps instance.

Blocking live-device tests require explicit test-account and test-device config.

Examples:
    SIXTYOPS_TEST_URL=https://sixtyops-dev.infra.treehouse.mn \
      SIXTYOPS_TEST_USER=dev-automation \
      SIXTYOPS_TEST_PASS=secret \
      SIXTYOPS_TEST_AP_IP=10.0.0.10 \
      SIXTYOPS_TEST_SWITCH_IP=10.0.0.20 \
      SIXTYOPS_TEST_FIRMWARE_AP_IP=10.0.0.11 \
      SIXTYOPS_TEST_CONFIG_AP_IP=10.0.0.12 \
      SIXTYOPS_TEST_RADIUS_AP_IP=10.0.0.13 \
      pytest -m "integration and dev_blocking" -v

    SIXTYOPS_TEST_URL=https://sixtyops-dev.infra.treehouse.mn \
      SIXTYOPS_TEST_USER=dev-automation \
      SIXTYOPS_TEST_PASS=secret \
      SIXTYOPS_TEST_OIDC_PROVIDER_URL=https://auth.example.com/application/o/sixtyops/ \
      SIXTYOPS_TEST_OIDC_CLIENT_ID=client-id \
      SIXTYOPS_TEST_OIDC_CLIENT_SECRET=client-secret \
      SIXTYOPS_TEST_OIDC_REDIRECT_URI=https://sixtyops-dev.infra.treehouse.mn/auth/oidc/callback \
      pytest -m "integration and dev_sso" -v
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
import ssl
import time
import uuid

import httpx
import pytest

SIXTYOPS_TEST_URL = os.environ.get("SIXTYOPS_TEST_URL", "").rstrip("/")
SIXTYOPS_TEST_USER = os.environ.get("SIXTYOPS_TEST_USER", "")
SIXTYOPS_TEST_PASS = os.environ.get("SIXTYOPS_TEST_PASS", "")

_DEVICE_ENV_VARS = {
    "test_ap_ip": "SIXTYOPS_TEST_AP_IP",
    "test_switch_ip": "SIXTYOPS_TEST_SWITCH_IP",
    "firmware_ap_ip": "SIXTYOPS_TEST_FIRMWARE_AP_IP",
    "config_ap_ip": "SIXTYOPS_TEST_CONFIG_AP_IP",
    "radius_ap_ip": "SIXTYOPS_TEST_RADIUS_AP_IP",
}

_OIDC_ENV_VARS = {
    "provider_url": "SIXTYOPS_TEST_OIDC_PROVIDER_URL",
    "client_id": "SIXTYOPS_TEST_OIDC_CLIENT_ID",
    "client_secret": "SIXTYOPS_TEST_OIDC_CLIENT_SECRET",
    "redirect_uri": "SIXTYOPS_TEST_OIDC_REDIRECT_URI",
    "allowed_group": "SIXTYOPS_TEST_OIDC_ALLOWED_GROUP",
    "admin_group": "SIXTYOPS_TEST_OIDC_ADMIN_GROUP",
}


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when the live dev URL is not configured."""
    if SIXTYOPS_TEST_URL:
        return
    skip = pytest.mark.skip(reason="SIXTYOPS_TEST_URL not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for this live integration test")
    return value


def _topology_from_session(session: httpx.Client) -> dict:
    resp = session.get("/api/topology")
    assert resp.status_code == 200
    data = resp.json()
    assert "sites" in data
    return data


def _iter_aps(topology: dict):
    for site in topology.get("sites", []):
        for ap in site.get("aps", []):
            yield ap
        for switch in site.get("switches", []):
            for ap in switch.get("aps", []) or []:
                yield ap


def _find_ap(topology: dict, ip: str) -> dict | None:
    for ap in _iter_aps(topology):
        if ap.get("ip") == ip:
            return ap
    return None


def _find_switch(topology: dict, ip: str) -> dict | None:
    for site in topology.get("sites", []):
        for switch in site.get("switches", []):
            if switch.get("ip") == ip:
                return switch
    return None


def _find_cpe(topology: dict, ip: str) -> dict | None:
    for ap in _iter_aps(topology):
        for cpe in ap.get("cpes", []):
            if cpe.get("ip") == ip:
                return cpe
    return None


def _request_with_retry(session: httpx.Client, method: str, url: str, *, attempts: int = 3, sleep_seconds: float = 1.0, **kwargs):
    response = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
        except httpx.TimeoutException:
            if attempt == attempts - 1:
                raise
            time.sleep(sleep_seconds)
            continue
        if response.status_code != 429 or attempt == attempts - 1:
            return response
        time.sleep(sleep_seconds)
    return response


@pytest.fixture(scope="session")
def base_url():
    """Base URL for the SixtyOps instance under test."""
    if not SIXTYOPS_TEST_URL:
        pytest.skip("SIXTYOPS_TEST_URL not set")
    return SIXTYOPS_TEST_URL


@pytest.fixture(scope="session")
def session(base_url):
    """Authenticated httpx client with session cookie."""
    username = _require_env("SIXTYOPS_TEST_USER")
    password = _require_env("SIXTYOPS_TEST_PASS")

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    client = httpx.Client(
        base_url=base_url,
        verify=ssl_ctx,
        timeout=60.0,
        follow_redirects=True,
    )

    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.status_code} {resp.text[:200]}"
    assert "session_id" in client.cookies, "No session cookie after login"

    yield client
    client.close()


@pytest.fixture(scope="session")
def topology(session):
    """Initial topology snapshot for the live instance."""
    return _topology_from_session(session)


@pytest.fixture
def get_topology(session):
    """Fetch a fresh topology snapshot."""
    return lambda: _topology_from_session(session)


@pytest.fixture(scope="session")
def device_env():
    """Explicit live-device environment variables used by blocking tests."""
    return {key: os.environ.get(env_name, "").strip() for key, env_name in _DEVICE_ENV_VARS.items()}


@pytest.fixture(scope="session")
def test_ap(topology, device_env):
    """Dedicated AP for core device-polling and CPE lifecycle checks."""
    ip = device_env["test_ap_ip"] or _require_env("SIXTYOPS_TEST_AP_IP")
    ap = _find_ap(topology, ip)
    if not ap:
        pytest.skip(f"SIXTYOPS_TEST_AP_IP={ip} was not found in live topology")
    if not ap.get("enabled", True):
        pytest.skip(f"SIXTYOPS_TEST_AP_IP={ip} is disabled")
    if not ap.get("cpes"):
        pytest.skip(f"SIXTYOPS_TEST_AP_IP={ip} must have attached CPEs for deterministic coverage")
    return ap


@pytest.fixture(scope="session")
def test_switch(topology, device_env):
    """Dedicated switch for polling and portal coverage."""
    ip = device_env["test_switch_ip"] or _require_env("SIXTYOPS_TEST_SWITCH_IP")
    switch = _find_switch(topology, ip)
    if not switch:
        pytest.skip(f"SIXTYOPS_TEST_SWITCH_IP={ip} was not found in live topology")
    if not switch.get("enabled", True):
        pytest.skip(f"SIXTYOPS_TEST_SWITCH_IP={ip} is disabled")
    return switch


@pytest.fixture(scope="session")
def firmware_ap(topology, device_env):
    """Dedicated AP that can be upgraded and rolled back."""
    ip = device_env["firmware_ap_ip"] or _require_env("SIXTYOPS_TEST_FIRMWARE_AP_IP")
    ap = _find_ap(topology, ip)
    if not ap:
        pytest.skip(f"SIXTYOPS_TEST_FIRMWARE_AP_IP={ip} was not found in live topology")
    return ap


@pytest.fixture(scope="session")
def config_ap(topology, device_env):
    """Dedicated AP for config backup/push/rollback coverage."""
    ip = device_env["config_ap_ip"] or _require_env("SIXTYOPS_TEST_CONFIG_AP_IP")
    ap = _find_ap(topology, ip)
    if not ap:
        pytest.skip(f"SIXTYOPS_TEST_CONFIG_AP_IP={ip} was not found in live topology")
    return ap


@pytest.fixture(scope="session")
def radius_ap(topology, device_env):
    """Dedicated AP for targeted RADIUS rollout coverage."""
    ip = device_env["radius_ap_ip"] or _require_env("SIXTYOPS_TEST_RADIUS_AP_IP")
    ap = _find_ap(topology, ip)
    if not ap:
        pytest.skip(f"SIXTYOPS_TEST_RADIUS_AP_IP={ip} was not found in live topology")
    return ap


@pytest.fixture(scope="session")
def test_cpe(test_ap):
    """A connected CPE under the dedicated AP."""
    cpes = test_ap.get("cpes") or []
    if not cpes:
        pytest.skip("Dedicated test AP has no attached CPEs")
    return cpes[0]


@pytest.fixture
def unique_name():
    """Generate unique resource names for live CRUD tests."""
    def _build(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"
    return _build


@pytest.fixture
def portal_credentials(session):
    """Extract device credentials from the device portal HTML."""
    def _get(ip: str) -> tuple[str, str]:
        resp = _request_with_retry(session, "GET", f"/api/device-portal/{ip}")
        assert resp.status_code == 200
        match = re.search(r"name='([^']+)' value='([^']*)'>", resp.text)
        assert match, f"Could not find auto-login payload for {ip}"
        payload = html_module.unescape(match.group(1)) + html_module.unescape(match.group(2))
        data = json.loads(payload)
        return data["username"], data["password"]
    return _get


@pytest.fixture
def request_with_retry(session):
    """Issue a live API request and retry transient 429 responses."""
    def _call(method: str, url: str, **kwargs):
        return _request_with_retry(session, method, url, **kwargs)
    return _call


@pytest.fixture(scope="session")
def oidc_test_config():
    """Dedicated OIDC config for the separate non-blocking SSO lane."""
    provider_url = os.environ.get(_OIDC_ENV_VARS["provider_url"], "").strip()
    client_id = os.environ.get(_OIDC_ENV_VARS["client_id"], "").strip()
    client_secret = os.environ.get(_OIDC_ENV_VARS["client_secret"], "").strip()
    redirect_uri = os.environ.get(_OIDC_ENV_VARS["redirect_uri"], "").strip()
    if not all((provider_url, client_id, client_secret, redirect_uri)):
        pytest.skip(
            "SIXTYOPS_TEST_OIDC_PROVIDER_URL, SIXTYOPS_TEST_OIDC_CLIENT_ID, "
            "SIXTYOPS_TEST_OIDC_CLIENT_SECRET, and SIXTYOPS_TEST_OIDC_REDIRECT_URI "
            "are required for dev_sso tests"
        )
    return {
        "enabled": True,
        "provider_url": provider_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "allowed_group": os.environ.get(_OIDC_ENV_VARS["allowed_group"], "").strip(),
        "admin_group": os.environ.get(_OIDC_ENV_VARS["admin_group"], "").strip(),
        "scopes": "openid email profile groups",
    }
