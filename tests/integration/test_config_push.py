"""Config push integration tests — preview, safe push, compliance."""

import json
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


@pytest.fixture
def _ensure_config_snapshot(config_ap, request_with_retry):
    """Make sure the test AP has at least one config snapshot."""
    ip = config_ap["ip"]
    resp = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
    assert resp.status_code == 200, resp.text[:300]


def test_config_push_preview(session, config_ap, _ensure_config_snapshot):
    """Preview a no-op template merge and verify the response structure."""
    ip = config_ap["ip"]

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
        templates = session.get("/api/config-templates").json()["templates"]
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


def test_config_push_noop_apply(session, config_ap, _ensure_config_snapshot):
    """Apply a no-op config template to the dedicated config AP."""
    ip = config_ap["ip"]
    template_name = "__integration_test_noop_apply"

    create_resp = session.post("/api/config-templates", json={
        "name": template_name,
        "category": "system",
        "config_fragment": json.dumps({}),
        "description": "Integration test — no-op config push",
    })
    if create_resp.status_code == 200:
        template_id = create_resp.json()["id"]
    else:
        templates = session.get("/api/config-templates").json()["templates"]
        template = next((t for t in templates if t["name"] == template_name), None)
        assert template, "Could not create or find no-op config template"
        template_id = template["id"]

    try:
        start_resp = session.post("/api/config-push", json={
            "template_ids": [template_id],
            "targets": [{"type": "ap", "ip": ip}],
        })
        assert start_resp.status_code == 200, start_resp.text[:300]
        job_id = start_resp.json()["job_id"]

        deadline = time.time() + 180
        while time.time() < deadline:
            status_resp = session.get(f"/api/config-push/jobs/{job_id}")
            assert status_resp.status_code == 200
            job = status_resp.json()
            if job["done"]:
                if job["failed"] != 0:
                    history_resp = session.get("/api/device-history", params={"ip": ip})
                    assert history_resp.status_code == 200
                    history = history_resp.json()["history"]
                    matching = next((entry for entry in history if entry.get("job_id") == job_id), None)
                    error_detail = matching.get("error") if matching else "unknown error"
                    pytest.fail(f"Config push job {job_id} failed for {ip}: {error_detail}")
                assert job["success"] == 1
                return
            time.sleep(2)
        pytest.fail(f"Timed out waiting for config push job {job_id}")
    finally:
        session.delete(f"/api/config-templates/{template_id}")
