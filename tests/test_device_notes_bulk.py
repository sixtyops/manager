"""Tests for device notes and bulk operations."""

from unittest.mock import patch

import pytest


class TestDeviceNotes:
    """Test device notes functionality."""

    def test_update_ap_with_notes(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute(
                "INSERT INTO access_points (ip, username, password) VALUES (?, ?, ?)",
                ("10.0.0.1", "admin", "pass")
            )
        resp = authed_client.put("/api/aps/10.0.0.1", data={
            "notes": "Tower A - north sector",
        })
        assert resp.status_code == 200

    def test_update_switch_with_notes(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute(
                "INSERT INTO switches (ip, username, password) VALUES (?, ?, ?)",
                ("10.0.1.1", "admin", "pass")
            )
        resp = authed_client.put("/api/switches/10.0.1.1", data={
            "notes": "Core switch - rack 2",
        })
        assert resp.status_code == 200

    def test_notes_stored_in_db(self, memory_db):
        with memory_db as conn:
            conn.execute(
                "INSERT INTO access_points (ip, username, password) VALUES (?, ?, ?)",
                ("10.0.0.1", "admin", "pass")
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import upsert_access_point, get_access_point
            upsert_access_point("10.0.0.1", "admin", "pass", None, notes="test note")
            ap = get_access_point("10.0.0.1")
            assert ap["notes"] == "test note"


class TestBulkEnable:
    def test_bulk_enable(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.1', 'a', 'p', 0)")
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.2', 'a', 'p', 0)")
        resp = authed_client.post("/api/devices/bulk-enable", json={
            "device_type": "ap",
            "ips": ["10.0.0.1", "10.0.0.2"],
        })
        assert resp.status_code == 200
        assert resp.json()["affected"] == 2

    def test_bulk_enable_no_ips(self, authed_client):
        resp = authed_client.post("/api/devices/bulk-enable", json={
            "device_type": "ap",
            "ips": [],
        })
        assert resp.status_code == 400

    def test_bulk_enable_invalid_type(self, authed_client):
        resp = authed_client.post("/api/devices/bulk-enable", json={
            "device_type": "router",
            "ips": ["10.0.0.1"],
        })
        assert resp.status_code == 400


class TestBulkDisable:
    def test_bulk_disable(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.1', 'a', 'p', 1)")
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.2', 'a', 'p', 1)")
        resp = authed_client.post("/api/devices/bulk-disable", json={
            "device_type": "ap",
            "ips": ["10.0.0.1", "10.0.0.2"],
        })
        assert resp.status_code == 200
        assert resp.json()["affected"] == 2

    def test_viewer_cannot_bulk_disable(self, viewer_client):
        resp = viewer_client.post("/api/devices/bulk-disable", json={
            "device_type": "ap",
            "ips": ["10.0.0.1"],
        })
        assert resp.status_code == 403


class TestBulkDelete:
    def test_bulk_delete(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO access_points (ip, username, password) VALUES ('10.0.0.1', 'a', 'p')")
            conn.execute("INSERT INTO access_points (ip, username, password) VALUES ('10.0.0.2', 'a', 'p')")
        resp = authed_client.post("/api/devices/bulk-delete", json={
            "device_type": "ap",
            "ips": ["10.0.0.1", "10.0.0.2"],
        })
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2

    def test_operator_cannot_bulk_delete(self, operator_client):
        resp = operator_client.post("/api/devices/bulk-delete", json={
            "device_type": "ap",
            "ips": ["10.0.0.1"],
        })
        assert resp.status_code == 403

    def test_viewer_cannot_bulk_delete(self, viewer_client):
        resp = viewer_client.post("/api/devices/bulk-delete", json={
            "device_type": "ap",
            "ips": ["10.0.0.1"],
        })
        assert resp.status_code == 403


class TestBulkMove:
    def test_bulk_move_to_site(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO tower_sites (id, name) VALUES (1, 'Site A')")
            conn.execute("INSERT INTO access_points (ip, username, password) VALUES ('10.0.0.1', 'a', 'p')")
            conn.execute("INSERT INTO access_points (ip, username, password) VALUES ('10.0.0.2', 'a', 'p')")
        resp = authed_client.post("/api/devices/bulk-move", json={
            "device_type": "ap",
            "ips": ["10.0.0.1", "10.0.0.2"],
            "site_id": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["affected"] == 2

    def test_bulk_move_switches(self, authed_client, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO switches (ip, username, password) VALUES ('10.0.1.1', 'a', 'p')")
        resp = authed_client.post("/api/devices/bulk-move", json={
            "device_type": "switch",
            "ips": ["10.0.1.1"],
            "site_id": None,
        })
        assert resp.status_code == 200


class TestBulkDBFunctions:
    def test_bulk_set_enabled(self, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.1', 'a', 'p', 1)")
            conn.execute("INSERT INTO access_points (ip, username, password, enabled) VALUES ('10.0.0.2', 'a', 'p', 1)")
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import bulk_set_enabled
            count = bulk_set_enabled("ap", ["10.0.0.1", "10.0.0.2"], False)
            assert count == 2

    def test_bulk_delete_devices_with_cpes(self, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO access_points (ip, username, password) VALUES ('10.0.0.1', 'a', 'p')")
            conn.execute("INSERT INTO cpe_cache (ap_ip, ip, mac) VALUES ('10.0.0.1', '10.0.0.100', 'aa:bb:cc:dd:ee:ff')")
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import bulk_delete_devices
            count = bulk_delete_devices("ap", ["10.0.0.1"])
            assert count == 1
            # CPE should also be deleted
            with memory_db as conn:
                cpes = conn.execute("SELECT * FROM cpe_cache WHERE ap_ip = '10.0.0.1'").fetchall()
                assert len(cpes) == 0

    def test_bulk_move_to_site(self, memory_db):
        with memory_db as conn:
            conn.execute("INSERT INTO tower_sites (id, name) VALUES (5, 'New Site')")
            conn.execute("INSERT INTO switches (ip, username, password) VALUES ('10.0.1.1', 'a', 'p')")
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import bulk_move_to_site
            count = bulk_move_to_site("switch", ["10.0.1.1"], 5)
            assert count == 1
