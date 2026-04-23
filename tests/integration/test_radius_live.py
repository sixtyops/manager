"""Blocking live-dev coverage for RADIUS APIs and targeted rollout."""

from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def _radius_template_payload(name: str, advertised_address: str, auth_port: int, shared_secret: str) -> dict:
    return {
        "name": name,
        "category": "radius",
        "config_fragment": {
            "system": {
                "auth": {
                    "method": "radius",
                    "radius": {
                        "auth_server1": advertised_address,
                        "auth_port": auth_port,
                        "auth_secret": shared_secret,
                    },
                },
            },
        },
        "form_data": {
            "method": "radius",
            "server": advertised_address,
            "port": str(auth_port),
            "secret": shared_secret,
        },
        "description": "Live integration radius template",
        "enabled": True,
    }


def _wait_for_radius_rollout(session, rollout_id: int, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = session.get("/api/auth/radius/rollout")
        assert resp.status_code == 200
        rollout = resp.json()["rollout"]
        if rollout and rollout["id"] == rollout_id and rollout["status"] in ("completed", "paused", "cancelled"):
            return rollout
        time.sleep(2)
    pytest.fail(f"Timed out waiting for RADIUS rollout {rollout_id}")


def test_radius_config_user_client_and_defaults_roundtrip(session, unique_name):
    radius_config = session.get("/api/auth/radius").json()
    assert "enabled" in radius_config

    status_resp = session.get("/api/auth/radius/status")
    assert status_resp.status_code == 200

    stats_resp = session.get("/api/auth/radius/stats")
    assert stats_resp.status_code == 200

    user_name = unique_name("radius-user")
    client_name = unique_name("radius-client")
    created_user = None
    created_client = None

    auth_config = session.get("/api/auth/config").json()
    original_defaults = dict(auth_config["device_defaults"])

    try:
        create_user = session.post("/api/auth/radius/users", json={
            "username": user_name,
            "password": "RadiusPass123!",
            "description": "Live integration user",
        })
        assert create_user.status_code == 200
        created_user = create_user.json()["id"]

        update_user = session.put(f"/api/auth/radius/users/{created_user}", json={
            "description": "Updated live integration user",
            "enabled": True,
        })
        assert update_user.status_code == 200

        list_users = session.get("/api/auth/radius/users")
        assert list_users.status_code == 200
        assert any(user["username"] == user_name for user in list_users.json()["users"])

        create_client = session.post("/api/auth/radius/clients", json={
            "client_spec": "127.0.20.0/24",
            "shortname": client_name,
            "enabled": True,
        })
        assert create_client.status_code == 200
        created_client = create_client.json()["id"]

        update_client = session.put(f"/api/auth/radius/clients/{created_client}", json={
            "client_spec": "127.0.20.0/24",
            "shortname": f"{client_name}-updated",
            "enabled": False,
        })
        assert update_client.status_code == 200

        list_clients = session.get("/api/auth/radius/clients")
        assert list_clients.status_code == 200
        assert any(client["id"] == created_client for client in list_clients.json()["clients"])

        new_defaults = {
            "enabled": not bool(original_defaults["enabled"]),
            "username": original_defaults["username"] or "live-default-user",
        }
        defaults_resp = session.put("/api/auth/device-defaults", json=new_defaults)
        assert defaults_resp.status_code == 200

        updated_auth_config = session.get("/api/auth/config").json()
        assert updated_auth_config["device_defaults"]["enabled"] == new_defaults["enabled"]
        assert updated_auth_config["device_defaults"]["username"] == new_defaults["username"]
    finally:
        session.put("/api/auth/device-defaults", json={
            "enabled": bool(original_defaults["enabled"]),
            "username": original_defaults["username"],
        })
        if created_client is not None:
            session.delete(f"/api/auth/radius/clients/{created_client}")
        if created_user is not None:
            session.delete(f"/api/auth/radius/users/{created_user}")


def test_targeted_radius_rollout_and_restore(session, radius_ap, portal_credentials, request_with_retry, unique_name):
    active = session.get("/api/auth/radius/rollout").json()["rollout"]
    if active and active["status"] in ("active", "paused"):
        pytest.skip("A RADIUS rollout is already active on the shared dev host")

    radius_ip = radius_ap["ip"]
    original_username, original_password = portal_credentials(radius_ip)

    poll_resp = request_with_retry("POST", f"/api/configs/{radius_ip}/poll", timeout=90.0)
    assert poll_resp.status_code == 200, poll_resp.text[:300]
    original_hash = poll_resp.json()["config_hash"]

    latest_resp = session.get(f"/api/configs/{radius_ip}/latest")
    assert latest_resp.status_code == 200
    original_config_id = latest_resp.json()["id"]

    original_radius_config = session.get("/api/auth/radius").json()
    templates_resp = session.get("/api/config-templates")
    assert templates_resp.status_code == 200
    all_templates = templates_resp.json()["templates"]
    radius_templates = sorted(
        (template for template in all_templates if template["category"] == "radius"),
        key=lambda template: template["id"],
    )
    existing_template = radius_templates[0] if radius_templates else None
    temp_user_id = None
    created_template_id = None

    advertised_address = (
        original_radius_config.get("advertised_address")
        or original_radius_config.get("detected_ip")
        or "127.0.0.1"
    )
    auth_port = int(original_radius_config.get("auth_port") or 1812)
    shared_secret = original_radius_config.get("shared_secret") or "LiveRadiusSecret1!"
    template_payload = _radius_template_payload(
        existing_template["name"] if existing_template else unique_name("radius-template"),
        advertised_address,
        auth_port,
        shared_secret,
    )

    try:
        create_user = session.post("/api/auth/radius/users", json={
            "username": unique_name("radius-mgmt"),
            "password": "RadiusPass123!",
            "description": "Live targeted rollout user",
        })
        assert create_user.status_code == 200
        temp_user_id = create_user.json()["id"]

        update_radius = session.put("/api/auth/radius", json={
            "enabled": True,
            "host": advertised_address,
            "port": auth_port,
            "secret": shared_secret,
            "auth_mode": original_radius_config.get("auth_mode") or "local",
            "client_mode": original_radius_config.get("client_mode") or "restricted",
        })
        assert update_radius.status_code == 200, update_radius.text[:300]

        if existing_template:
            update_template = session.put(f"/api/config-templates/{existing_template['id']}", json=template_payload)
            assert update_template.status_code == 200, update_template.text[:300]
        else:
            create_template = session.post("/api/config-templates", json=template_payload)
            assert create_template.status_code == 200, create_template.text[:300]
            created_template_id = create_template.json()["id"]

        start_rollout = session.post("/api/auth/radius/rollout/start", json={"target_ips": [radius_ip]})
        assert start_rollout.status_code == 200, start_rollout.text[:300]
        rollout = start_rollout.json()["rollout"]
        final_rollout = _wait_for_radius_rollout(session, rollout["id"], timeout=300)
        unexpected_devices = [device for device in final_rollout["devices"] if device["ip"] != radius_ip]
        assert not unexpected_devices, (
            f"Targeted RADIUS rollout for {radius_ip} included out-of-scope devices: "
            f"{unexpected_devices}"
        )
        assert final_rollout["status"] == "completed", final_rollout
        progress = final_rollout["progress"]
        assert progress["updated"] >= 1
        assert any(device["ip"] == radius_ip and device["status"] == "updated" for device in final_rollout["devices"])

        rollback = session.post(f"/api/config-push/rollback/{radius_ip}", json={"config_id": original_config_id})
        assert rollback.status_code == 200, rollback.text[:300]

        restore_creds = session.put(f"/api/aps/{radius_ip}", data={
            "username": original_username,
            "password": original_password,
        })
        assert restore_creds.status_code == 200, restore_creds.text[:300]

        verify_poll = request_with_retry("POST", f"/api/configs/{radius_ip}/poll", timeout=90.0)
        assert verify_poll.status_code == 200, verify_poll.text[:300]
        assert verify_poll.json()["config_hash"] == original_hash
    finally:
        current_rollout = session.get("/api/auth/radius/rollout").json()["rollout"]
        if current_rollout and current_rollout["status"] in ("active", "paused"):
            session.post(f"/api/auth/radius/rollout/{current_rollout['id']}/cancel")

        session.put("/api/auth/radius", json={
            "enabled": original_radius_config["enabled"],
            "host": original_radius_config.get("advertised_address", ""),
            "port": original_radius_config.get("auth_port", 1812),
            "secret": original_radius_config.get("shared_secret", ""),
            "auth_mode": original_radius_config.get("auth_mode", "local"),
            "client_mode": original_radius_config.get("client_mode", "restricted"),
            "ldap_url": original_radius_config.get("ldap_url", ""),
            "ldap_bind_dn": original_radius_config.get("ldap_bind_dn", ""),
            "ldap_base_dn": original_radius_config.get("ldap_base_dn", ""),
            "ldap_user_filter": original_radius_config.get("ldap_user_filter", ""),
        })

        if existing_template:
            session.put(f"/api/config-templates/{existing_template['id']}", json={
                "name": existing_template["name"],
                "category": existing_template["category"],
                "config_fragment": existing_template["config_fragment"],
                "form_data": existing_template.get("form_data"),
                "description": existing_template.get("description"),
                "enabled": bool(existing_template.get("enabled", True)),
                "scope": existing_template.get("scope", "global"),
                "site_id": existing_template.get("site_id"),
                "device_types": existing_template.get("device_types"),
            })
        elif created_template_id is not None:
            session.delete(f"/api/config-templates/{created_template_id}")

        if temp_user_id is not None:
            session.delete(f"/api/auth/radius/users/{temp_user_id}")
