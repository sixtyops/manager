"""Tests for device groups feature."""

import json

from updater import database as db


# ---------------------------------------------------------------------------
# Database layer tests
# ---------------------------------------------------------------------------

class TestDeviceGroupDB:
    def test_create_device_group(self, mock_db):
        gid = db.create_device_group("Site-A APs", "All APs at site A",
                                     json.dumps({"site_ids": [1], "device_type": "ap"}))
        assert gid > 0

    def test_list_device_groups(self, mock_db):
        db.create_device_group("Group B")
        db.create_device_group("Group A")
        groups = db.list_device_groups()
        assert len(groups) == 2
        # Ordered by name
        assert groups[0]["name"] == "Group A"

    def test_get_device_group(self, mock_db):
        gid = db.create_device_group("Test", "desc", '{"device_type":"ap"}')
        group = db.get_device_group(gid)
        assert group["name"] == "Test"
        assert group["description"] == "desc"

    def test_get_device_group_not_found(self, mock_db):
        assert db.get_device_group(999) is None

    def test_update_device_group(self, mock_db):
        gid = db.create_device_group("Old Name")
        ok = db.update_device_group(gid, name="New Name", description="updated")
        assert ok is True
        group = db.get_device_group(gid)
        assert group["name"] == "New Name"
        assert group["description"] == "updated"

    def test_update_device_group_not_found(self, mock_db):
        ok = db.update_device_group(999, name="x")
        assert ok is False

    def test_update_device_group_no_fields(self, mock_db):
        gid = db.create_device_group("X")
        ok = db.update_device_group(gid, bad_field="ignored")
        assert ok is False

    def test_delete_device_group(self, mock_db):
        gid = db.create_device_group("ToDelete")
        assert db.delete_device_group(gid) is True
        assert db.get_device_group(gid) is None

    def test_delete_device_group_not_found(self, mock_db):
        assert db.delete_device_group(999) is False

    def test_unique_name_constraint(self, mock_db):
        db.create_device_group("Unique")
        try:
            db.create_device_group("Unique")
            assert False, "Should have raised"
        except Exception:
            pass

    def test_resolve_empty_filter(self, mock_db):
        gid = db.create_device_group("Empty")
        ips = db.resolve_device_group(gid)
        assert ips == []

    def test_resolve_with_site_filter(self, mock_db):
        # Insert a site and AP
        mock_db.execute("INSERT INTO tower_sites (name) VALUES (?)", ("Site1",))
        site_id = mock_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        mock_db.execute(
            "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", site_id, "u", "p", 1),
        )
        # AP at different site
        mock_db.execute(
            "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.2", site_id + 100, "u", "p", 1),
        )
        mock_db.commit()

        gid = db.create_device_group(
            "Site1 APs", filter_json=json.dumps({"site_ids": [site_id], "device_type": "ap"})
        )
        ips = db.resolve_device_group(gid)
        assert ips == ["10.0.0.1"]

    def test_resolve_with_model_filter(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, model, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.3", "u", "p", "tna-303x", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, model, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.4", "u", "p", "tna-200", 1),
        )
        mock_db.commit()

        gid = db.create_device_group(
            "303x Only", filter_json=json.dumps({"models": ["tna-303x"], "device_type": "ap"})
        )
        ips = db.resolve_device_group(gid)
        assert ips == ["10.0.0.3"]

    def test_resolve_switches(self, mock_db):
        mock_db.execute(
            "INSERT INTO switches (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.1.1", "u", "p", 1),
        )
        mock_db.commit()

        gid = db.create_device_group(
            "All Switches", filter_json=json.dumps({"device_type": "switch"})
        )
        ips = db.resolve_device_group(gid)
        assert ips == ["10.0.1.1"]


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------

class TestDeviceGroupAPI:
    def test_list_empty(self, authed_client):
        resp = authed_client.get("/api/device-groups")
        assert resp.status_code == 200
        assert resp.json()["groups"] == []

    def test_create_group(self, authed_client):
        resp = authed_client.post("/api/device-groups", json={
            "name": "Test Group",
            "description": "A test",
            "filter_json": {"device_type": "ap", "site_ids": [1]},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Group"
        assert "id" in data

    def test_create_group_no_name(self, authed_client):
        resp = authed_client.post("/api/device-groups", json={"name": ""})
        assert resp.status_code == 400

    def test_create_duplicate_name(self, authed_client):
        authed_client.post("/api/device-groups", json={"name": "Dup"})
        resp = authed_client.post("/api/device-groups", json={"name": "Dup"})
        assert resp.status_code == 409

    def test_get_group(self, authed_client):
        create = authed_client.post("/api/device-groups", json={"name": "G1"})
        gid = create.json()["id"]
        resp = authed_client.get(f"/api/device-groups/{gid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "G1"

    def test_get_group_not_found(self, authed_client):
        resp = authed_client.get("/api/device-groups/999")
        assert resp.status_code == 404

    def test_update_group(self, authed_client):
        create = authed_client.post("/api/device-groups", json={"name": "Old"})
        gid = create.json()["id"]
        resp = authed_client.put(f"/api/device-groups/{gid}", json={"name": "New"})
        assert resp.status_code == 200

        get = authed_client.get(f"/api/device-groups/{gid}")
        assert get.json()["name"] == "New"

    def test_update_group_empty_body(self, authed_client):
        create = authed_client.post("/api/device-groups", json={"name": "X"})
        gid = create.json()["id"]
        resp = authed_client.put(f"/api/device-groups/{gid}", json={})
        assert resp.status_code == 400

    def test_update_group_not_found(self, authed_client):
        resp = authed_client.put("/api/device-groups/999", json={"name": "Y"})
        assert resp.status_code == 404

    def test_delete_group(self, authed_client):
        create = authed_client.post("/api/device-groups", json={"name": "Del"})
        gid = create.json()["id"]
        resp = authed_client.delete(f"/api/device-groups/{gid}")
        assert resp.status_code == 200
        assert authed_client.get(f"/api/device-groups/{gid}").status_code == 404

    def test_delete_group_not_found(self, authed_client):
        resp = authed_client.delete("/api/device-groups/999")
        assert resp.status_code == 404

    def test_resolve_group(self, authed_client, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.5", "u", "p", 1),
        )
        mock_db.commit()
        create = authed_client.post("/api/device-groups", json={
            "name": "All APs",
            "filter_json": {"device_type": "ap"},
        })
        gid = create.json()["id"]
        resp = authed_client.get(f"/api/device-groups/{gid}/resolve")
        assert resp.status_code == 200
        data = resp.json()
        assert "10.0.0.5" in data["device_ips"]
        assert data["count"] >= 1

    def test_resolve_not_found(self, authed_client):
        resp = authed_client.get("/api/device-groups/999/resolve")
        assert resp.status_code == 404

    def test_viewer_cannot_create(self, viewer_client):
        resp = viewer_client.post("/api/device-groups", json={"name": "Nope"})
        assert resp.status_code == 403

    def test_viewer_can_list(self, viewer_client):
        resp = viewer_client.get("/api/device-groups")
        assert resp.status_code == 200

    def test_viewer_cannot_delete(self, viewer_client):
        resp = viewer_client.delete("/api/device-groups/1")
        assert resp.status_code == 403
