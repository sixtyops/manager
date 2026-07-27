"""Tests for v1.5.0 DHCP re-IP recovery: device identity, the locator ladder,
IP-change bookkeeping, and the manual Change-IP endpoint.

The identity/locator/change_device_ip tests need the real schema + triggers, so
they init a temp on-disk DB rather than the hand-rolled in-memory fixture.
"""

import asyncio

import pytest

from updater import database as db
from updater import device_locator as loc


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """A fresh on-disk DB built from the real schema (triggers included)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sixtyops.db")
    db.init_db()
    return db


def _seed_ap(ip="10.0.0.5", mac="AA:BB:CC:DD:EE:01", serial="SN-1"):
    db.upsert_access_point(ip, "admin", "pw", system_name="AP", model="M", mac=mac)
    db.upsert_device_identity(ip, serial=serial, macs=[mac, "AA:BB:CC:DD:EE:02"])


# ─── Device identity ──────────────────────────────────────────────────────

class TestDeviceIdentity:
    def test_upsert_and_get(self, real_db):
        _seed_ap()
        ident = db.get_device_identity("10.0.0.5")
        assert ident["serial"] == "SN-1"
        assert "AA:BB:CC:DD:EE:01" in ident["macs"]
        assert "AA:BB:CC:DD:EE:02" in ident["macs"]

    def test_missing_serial_does_not_clobber(self, real_db):
        _seed_ap()
        # A later poll that returns no serial keeps the stored one.
        db.upsert_device_identity("10.0.0.5", serial=None, macs=["AA:BB:CC:DD:EE:01"])
        assert db.get_device_identity("10.0.0.5")["serial"] == "SN-1"

    def test_find_ip_by_serial(self, real_db):
        _seed_ap()
        assert db.find_identity_ip_by_serial("SN-1") == "10.0.0.5"
        assert db.find_identity_ip_by_serial("SN-1", exclude_ip="10.0.0.5") is None
        assert db.find_identity_ip_by_serial("NOPE") is None


# ─── change_device_ip ─────────────────────────────────────────────────────

class TestChangeDeviceIp:
    def test_moves_device_and_dependents(self, real_db):
        _seed_ap()
        with db.get_db() as c:
            c.execute("INSERT INTO cpe_cache (ap_ip, ip, mac, system_name) VALUES (?,?,?,?)",
                      ("10.0.0.5", "10.0.0.9", "11:22:33:44:55:66", "CPE"))
            c.execute("INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) "
                      "VALUES (?,?,?,?)", ("10.0.0.5", "ap", "up", "t"))

        mac = db.change_device_ip("10.0.0.5", "10.0.0.77")
        assert mac == "AA:BB:CC:DD:EE:01"

        assert db.get_device("10.0.0.5") is None
        assert db.get_device("10.0.0.77")["system_name"] == "AP"
        with db.get_db() as c:
            aps = [dict(r) for r in c.execute("SELECT ip FROM access_points").fetchall()]
            cpes = [dict(r) for r in c.execute("SELECT ap_ip FROM cpe_cache").fetchall()]
            ups = [dict(r) for r in c.execute("SELECT ip FROM device_uptime_events").fetchall()]
        assert aps == [{"ip": "10.0.0.77"}]          # legacy mirror moved (no desync/dupe)
        assert cpes == [{"ap_ip": "10.0.0.77"}]       # CPEs repointed
        assert ups == [{"ip": "10.0.0.77"}]           # history followed
        assert db.get_device_identity("10.0.0.5") is None
        assert db.get_device_identity("10.0.0.77")["serial"] == "SN-1"

    def test_idempotent_when_already_moved(self, real_db):
        _seed_ap()
        db.change_device_ip("10.0.0.5", "10.0.0.77")
        # Second call (old gone, new present) must not raise.
        assert db.change_device_ip("10.0.0.5", "10.0.0.77") == "AA:BB:CC:DD:EE:01"

    def test_raises_when_neither_exists(self, real_db):
        with pytest.raises(ValueError):
            db.change_device_ip("10.0.0.5", "10.0.0.77")


# ─── The locator ladder ───────────────────────────────────────────────────

class TestLocator:
    def _patch_probe(self, monkeypatch, mapping):
        async def fake_probe(ip, u, p, vendor="tachyon", timeout=12):
            serial = mapping.get(ip)
            return {"ip": ip, "serial": serial, "mac": None, "macs": [],
                    "system_name": "x", "model": "M"} if serial else None
        monkeypatch.setattr(loc, "probe_identity", fake_probe)

    def test_peer_directory_hit_no_scan(self, real_db, monkeypatch):
        _seed_ap()
        with db.get_db() as c:
            c.execute("INSERT INTO cpe_cache (ap_ip, ip, mac) VALUES (?,?,?)",
                      ("10.0.0.99", "10.0.0.42", "AA:BB:CC:DD:EE:01"))
        self._patch_probe(monkeypatch, {"10.0.0.42": "SN-1"})
        found = asyncio.run(loc.locate_device(
            "SN-1", ["AA:BB:CC:DD:EE:01"], "10.0.0.5", "admin", "pw", allow_scan=False))
        assert found == "10.0.0.42"

    def test_scan_hit(self, real_db, monkeypatch):
        _seed_ap()
        self._patch_probe(monkeypatch, {"10.0.0.42": "SN-1", "10.0.0.7": "OTHER"})
        async def fake_reach(ip):
            return ip in ("10.0.0.42", "10.0.0.7")
        monkeypatch.setattr(loc, "_reachable", fake_reach)
        found = asyncio.run(loc.locate_device(
            "SN-1", ["AA:BB:CC:DD:EE:01"], "10.0.0.5", "admin", "pw", allow_scan=True))
        assert found == "10.0.0.42"

    def test_scan_disabled_returns_none(self, real_db, monkeypatch):
        _seed_ap()
        self._patch_probe(monkeypatch, {"10.0.0.42": "SN-1"})
        found = asyncio.run(loc.locate_device(
            "SN-1", ["AA:BB:CC:DD:EE:01"], "10.0.0.5", "admin", "pw", allow_scan=False))
        assert found is None

    def test_serial_mismatch_never_adopted(self, real_db, monkeypatch):
        _seed_ap()
        # A different unit answers everywhere; its serial never matches.
        self._patch_probe(monkeypatch, {"10.0.0.42": "SOMEONE-ELSE"})
        async def fake_reach(ip):
            return True
        monkeypatch.setattr(loc, "_reachable", fake_reach)
        found = asyncio.run(loc.locate_device(
            "SN-1", ["AA:BB:CC:DD:EE:01"], "10.0.0.5", "admin", "pw", allow_scan=True))
        assert found is None


# ─── Manual Change-IP endpoint (through the app + auth) ────────────────────

class TestChangeIpEndpoint:
    def _seed_device(self, mac="AA:BB:CC:DD:EE:01", serial="SN-1"):
        db.upsert_access_point("10.0.0.5", "admin", "pw", system_name="AP", model="M", mac=mac)
        db.upsert_device_identity("10.0.0.5", serial=serial, macs=[mac])

    def test_serial_match_moves_device(self, authed_client, monkeypatch):
        self._seed_device()

        async def fake_probe(ip, u, p, vendor="tachyon", timeout=12):
            return {"ip": ip, "serial": "SN-1", "mac": "AA:BB:CC:DD:EE:01",
                    "macs": ["AA:BB:CC:DD:EE:01"], "system_name": "AP", "model": "M"}
        monkeypatch.setattr("updater.device_locator.probe_identity", fake_probe)

        resp = authed_client.post("/api/aps/10.0.0.5/change-ip", data={"new_ip": "10.0.0.88"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["new_ip"] == "10.0.0.88"
        assert db.get_device("10.0.0.5") is None
        assert db.get_device("10.0.0.88") is not None

    def test_serial_mismatch_rejected(self, authed_client, monkeypatch):
        self._seed_device()

        async def fake_probe(ip, u, p, vendor="tachyon", timeout=12):
            return {"ip": ip, "serial": "DIFFERENT", "mac": None, "macs": [],
                    "system_name": "AP", "model": "M"}
        monkeypatch.setattr("updater.device_locator.probe_identity", fake_probe)

        resp = authed_client.post("/api/aps/10.0.0.5/change-ip", data={"new_ip": "10.0.0.88"})
        assert resp.status_code == 409
        # Device stays put.
        assert db.get_device("10.0.0.5") is not None
        assert db.get_device("10.0.0.88") is None

    def test_unreachable_new_ip_rejected(self, authed_client, monkeypatch):
        self._seed_device()

        async def fake_probe(ip, u, p, vendor="tachyon", timeout=12):
            return None
        monkeypatch.setattr("updater.device_locator.probe_identity", fake_probe)

        resp = authed_client.post("/api/aps/10.0.0.5/change-ip", data={"new_ip": "10.0.0.88"})
        assert resp.status_code == 400
        assert db.get_device("10.0.0.5") is not None


# ─── Rollout recovery (_recover_reipd_device) — the core uptime fix ────────

class _FakeDriver:
    """Minimal driver whose verify_firmware reports the device on target fw."""
    def __init__(self, ip, u, p, timeout=30):
        self.ip = ip

    async def verify_firmware(self, fw, old_version=None, pass_number=1, progress=None):
        from updater.tachyon import UpdateResult
        return UpdateResult(ip=self.ip, success=True, old_version=old_version,
                            new_version="1.5.0", model="M")


class TestRolloutRecovery:
    def _job_with_device(self):
        from updater.app import UpdateJob, DeviceStatus
        job = UpdateJob(job_id="j1")
        job.devices["10.0.0.5"] = DeviceStatus(ip="10.0.0.5", role="ap")
        return job

    def test_locates_verifies_and_moves(self, real_db, monkeypatch):
        from updater import app
        _seed_ap()  # AP + identity (serial SN-1) at 10.0.0.5
        job = self._job_with_device()
        result = app.UpdateResult(ip="10.0.0.5", success=False,
                                  old_version="1.4.0", error="Device did not come back online")

        async def fake_locate(serial, macs, hint, u, p, vendor="tachyon", allow_scan=True):
            assert serial == "SN-1"
            return "10.0.0.66"
        monkeypatch.setattr("updater.device_locator.locate_device", fake_locate)
        monkeypatch.setattr(app, "get_driver", lambda v: _FakeDriver)

        new_ip = asyncio.run(app._recover_reipd_device(
            job, "10.0.0.5", "10.0.0.5", "admin", "pw", "tachyon",
            "/tmp/fw.bin", 1, result, lambda ip, msg: None))

        assert new_ip == "10.0.0.66"
        assert result.success is True and result.error is None
        assert result.new_version == "1.5.0"
        assert job.ip_overrides["10.0.0.5"] == "10.0.0.66"
        assert job.devices["10.0.0.5"].moved_to == "10.0.0.66"
        assert db.get_device("10.0.0.5") is None
        assert db.get_device("10.0.0.66") is not None

    def test_not_located_returns_none_and_leaves_failure(self, real_db, monkeypatch):
        from updater import app
        _seed_ap()
        job = self._job_with_device()
        result = app.UpdateResult(ip="10.0.0.5", success=False,
                                  old_version="1.4.0", error="Device did not come back online")

        async def fake_locate(*a, **k):
            return None
        monkeypatch.setattr("updater.device_locator.locate_device", fake_locate)

        new_ip = asyncio.run(app._recover_reipd_device(
            job, "10.0.0.5", "10.0.0.5", "admin", "pw", "tachyon",
            "/tmp/fw.bin", 1, result, lambda ip, msg: None))

        assert new_ip is None
        assert result.success is False           # failure preserved -> job still halts
        assert db.get_device("10.0.0.5") is not None
        assert db.get_device("10.0.0.5")  # unmoved

    def test_no_serial_cannot_recover(self, real_db, monkeypatch):
        from updater import app
        # AP with no recorded identity/serial.
        db.upsert_access_point("10.0.0.5", "admin", "pw", mac="AA:BB:CC:DD:EE:01")
        job = self._job_with_device()
        result = app.UpdateResult(ip="10.0.0.5", success=False,
                                  old_version="1.4.0", error="Device did not come back online")

        called = {"located": False}
        async def fake_locate(*a, **k):
            called["located"] = True
            return "10.0.0.66"
        monkeypatch.setattr("updater.device_locator.locate_device", fake_locate)

        new_ip = asyncio.run(app._recover_reipd_device(
            job, "10.0.0.5", "10.0.0.5", "admin", "pw", "tachyon",
            "/tmp/fw.bin", 1, result, lambda ip, msg: None))

        assert new_ip is None
        assert called["located"] is False        # no serial -> never even searches
