"""Tests for API routes (authenticated)."""

import pytest


class TestSitesAPI:
    def test_create_site(self, authed_client):
        resp = authed_client.post("/api/sites", data={"name": "Tower A", "location": "Hilltop"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Tower A"

    def test_list_sites(self, authed_client):
        authed_client.post("/api/sites", data={"name": "Tower B"})
        resp = authed_client.get("/api/sites")
        assert resp.status_code == 200
        sites = resp.json()["sites"]
        assert any(s["name"] == "Tower B" for s in sites)

    def test_update_site(self, authed_client):
        create_resp = authed_client.post("/api/sites", data={"name": "Old Name"})
        site_id = create_resp.json()["id"]
        resp = authed_client.put(f"/api/sites/{site_id}", data={"name": "New Name"})
        assert resp.status_code == 200

    def test_delete_site(self, authed_client):
        create_resp = authed_client.post("/api/sites", data={"name": "ToDelete"})
        site_id = create_resp.json()["id"]
        resp = authed_client.delete(f"/api/sites/{site_id}")
        assert resp.status_code == 200

    def test_duplicate_name(self, authed_client):
        authed_client.post("/api/sites", data={"name": "Duplicate"})
        resp = authed_client.post("/api/sites", data={"name": "Duplicate"})
        assert resp.status_code == 400


class TestAPsAPI:
    def test_create_ap(self, authed_client):
        resp = authed_client.post("/api/aps", data={"ip": "10.0.0.1", "username": "root", "password": "pass"})
        assert resp.status_code == 200
        assert resp.json()["ip"] == "10.0.0.1"

    def test_list_aps(self, authed_client):
        authed_client.post("/api/aps", data={"ip": "10.0.0.2", "username": "root", "password": "pass"})
        resp = authed_client.get("/api/aps")
        assert resp.status_code == 200
        assert len(resp.json()["aps"]) >= 1

    def test_delete_ap(self, authed_client):
        authed_client.post("/api/aps", data={"ip": "10.0.0.3", "username": "root", "password": "pass"})
        resp = authed_client.delete("/api/aps/10.0.0.3")
        assert resp.status_code == 200


class TestSettingsAPI:
    def test_get_settings(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.status_code == 200
        assert "settings" in resp.json()

    def test_update_settings(self, authed_client):
        resp = authed_client.put("/api/settings", json={"schedule_enabled": "true"})
        assert resp.status_code == 200
        # Verify
        resp = authed_client.get("/api/settings")
        assert resp.json()["settings"]["schedule_enabled"] == "true"

    def test_save_settings_and_reevaluate(self, authed_client):
        resp = authed_client.post("/api/settings/save", json={
            "schedule_days": "mon,wed,fri",
            "schedule_start_hour": "2",
            "schedule_end_hour": "5",
            "parallel_updates": "4",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        # Verify settings persisted
        resp = authed_client.get("/api/settings")
        s = resp.json()["settings"]
        assert s["schedule_days"] == "mon,wed,fri"
        assert s["parallel_updates"] == "4"

    def test_save_settings_rejects_invalid_keys(self, authed_client):
        resp = authed_client.post("/api/settings/save", json={
            "admin_password_hash": "malicious",
        })
        assert resp.status_code == 400


class TestTopologyAPI:
    def test_get_topology(self, authed_client):
        resp = authed_client.get("/api/topology")
        assert resp.status_code == 200
        data = resp.json()
        assert "sites" in data or "total_aps" in data


class TestQuickAddAPI:
    def test_quick_add(self, authed_client):
        resp = authed_client.post("/api/quick-add", data={
            "ip": "10.0.0.5",
            "username": "root",
            "password": "pass",
            "site_name": "NewSite",
        })
        assert resp.status_code == 200
        assert resp.json()["ip"] == "10.0.0.5"
        assert resp.json()["site_id"] is not None


class TestFirmwareAPI:
    def test_list_firmware_files(self, authed_client):
        resp = authed_client.get("/api/firmware-files")
        assert resp.status_code == 200
        assert "files" in resp.json()
