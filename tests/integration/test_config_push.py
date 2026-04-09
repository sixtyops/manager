"""Config push integration tests — preview, safe push, compliance."""

import json

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def _ensure_config_snapshot(session, test_ap):
    """Make sure the test AP has at least one config snapshot."""
    ip = test_ap["ip"]
    session.post(f"/api/configs/{ip}/poll")


def test_config_push_preview(session, test_ap, _ensure_config_snapshot):
    """Preview a no-op template merge and verify the response structure."""
    ip = test_ap["ip"]

    # Create a temporary no-op template (empty fragment)
    create_resp = session.post("/api/config-templates", json={
        "name": "__integration_test_noop",
        "category": "system",
        "config_fragment": json.dumps({}),
        "description": "Integration test — no-op template",
    })
    # May already exist from a previous run
    if create_resp.status_code == 200:
        template_id = create_resp.json()["id"]
    else:
        # List templates and find ours
        templates = session.get("/api/config-templates").json()
        t = next((t for t in templates if t["name"] == "__integration_test_noop"), None)
        assert t, "Could not create or find test template"
        template_id = t["id"]

    try:
        resp = session.post("/api/config-push/preview", json={
            "ip": ip,
            "template_ids": [template_id],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "merged" in data
        assert "original" in data
        assert "diff_lines" in data
        # No-op template should produce no diff
        assert data["changed"] is False
    finally:
        # Clean up test template
        session.delete(f"/api/config-templates/{template_id}")


def test_config_compliance_check(session):
    """Config compliance endpoint should return without error."""
    resp = session.get("/api/config-compliance")
    assert resp.status_code == 200
