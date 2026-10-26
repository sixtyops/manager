"""Live config coverage for add-only Users templates and restore."""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def _users(config: dict) -> list[dict]:
    users = config.get("system", {}).get("users")
    assert isinstance(users, list), "Config does not contain a Users list"
    return users


def _wait_for_config_push(session, job_id: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = session.get(f"/api/config-push/jobs/{job_id}")
        assert response.status_code == 200, response.text[:300]
        job = response.json()
        if job["done"]:
            assert job["failed"] == 0, f"Config push job {job_id} failed"
            assert job["success"] == 1, f"Config push job {job_id} did not succeed"
            return
        time.sleep(2)
    pytest.fail(f"Timed out waiting for config push job {job_id}")


def test_users_template_add_only_and_restore(
    session, config_ap, portal_credentials, request_with_retry, unique_name
):
    ip = config_ap["ip"]
    manager_username, _ = portal_credentials(ip)

    poll = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
    assert poll.status_code == 200, poll.text[:300]
    baseline_hash = poll.json()["config_hash"]

    latest = session.get(f"/api/configs/{ip}/latest")
    assert latest.status_code == 200, latest.text[:300]
    baseline = latest.json()
    baseline_id = baseline["id"]
    baseline_config = baseline["config_json"]
    baseline_users = _users(baseline_config)
    manager_entries = [user for user in baseline_users if user.get("username") == manager_username]
    assert len(manager_entries) == 1, "Manager login user is not unique in the baseline"
    manager_entry = manager_entries[0]

    test_username = unique_name("users-add-only")
    test_password = unique_name("SyntheticPass")
    template_id = None
    try:
        created = session.post("/api/config-templates", json={
            "name": unique_name("users-add-only-template"),
            "category": "users",
            "config_fragment": {
                "system": {
                    "users": [{"username": test_username, "password": test_password, "level": 1}],
                },
            },
            "description": "Integration test Users add-only template",
        })
        assert created.status_code == 200
        template_id = created.json()["id"]

        preview = session.post("/api/config-push/preview", json={
            "ip": ip,
            "template_ids": [template_id],
        })
        assert preview.status_code == 200
        preview_data = preview.json()
        preview_users = _users(preview_data["merged"])
        assert preview_users[:-1] == baseline_users
        assert preview_users[-1].get("username") == test_username
        assert next(user for user in preview_users if user.get("username") == manager_username) == manager_entry
        assert preview_data["changed"] is True

        started = session.post("/api/config-push", json={
            "template_ids": [template_id],
            "targets": [{"type": "ap", "ip": ip}],
        })
        assert started.status_code == 200
        _wait_for_config_push(session, started.json()["job_id"])

        readback = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
        assert readback.status_code == 200, readback.text[:300]
        fetched = session.get(f"/api/configs/{ip}/latest")
        assert fetched.status_code == 200, fetched.text[:300]
        actual_users = _users(fetched.json()["config_json"])
        assert actual_users[:-1] == baseline_users
        assert actual_users[-1].get("username") == test_username
        assert next(user for user in actual_users if user.get("username") == manager_username) == manager_entry
    finally:
        try:
            restored = session.post(
                f"/api/config-push/rollback/{ip}", json={"config_id": baseline_id}
            )
            assert restored.status_code == 200, restored.text[:300]
            verify = request_with_retry("POST", f"/api/configs/{ip}/poll", timeout=90.0)
            assert verify.status_code == 200, verify.text[:300]
            assert verify.json()["config_hash"] == baseline_hash
            restored_config = session.get(f"/api/configs/{ip}/latest")
            assert restored_config.status_code == 200, restored_config.text[:300]
            restored_users = _users(restored_config.json()["config_json"])
            assert restored_users == baseline_users
            assert next(user for user in restored_users if user.get("username") == manager_username) == manager_entry
        finally:
            if template_id is not None:
                session.delete(f"/api/config-templates/{template_id}")
