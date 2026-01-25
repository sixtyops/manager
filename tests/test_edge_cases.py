"""Edge case tests for OVA/initial setup scenarios.

Tests cover 10 edge cases:
EC5  - No DHCP lease (TUI remains accessible)
EC6  - /data mount failure (no false completion marker)
EC7  - First-run password race (concurrent /setup POST)
EC8  - Bootstrap credential state mismatch (no lockout)
EC9  - Setup wizard partial/failed SSL + backup
EC10 - OVA hardware drift across hypervisors
"""

import os
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import bcrypt as _bcrypt

# Add appliance/scripts to path for create-ova
sys.path.insert(0, str(Path(__file__).parent.parent / "appliance" / "scripts"))
import importlib
create_ova = importlib.import_module("create-ova")


# =========================================================================
# EC5: No DHCP lease on first boot
# =========================================================================

class TestNoDHCPLease:
    """TUI remains accessible and static config path works when no DHCP."""

    def test_update_status_api_responds(self, authed_client):
        """When app is running, updates API responds (no external network needed)."""
        resp = authed_client.get("/api/updates")
        # Should return 200 with status, not crash
        assert resp.status_code == 200

    def test_login_page_accessible_without_network(self, client):
        """Login page serves from local templates, no external deps."""
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in" in resp.text


# =========================================================================
# EC6: /data mount failure
# =========================================================================

class TestDataMountFailure:
    """No false first-boot marker when /data is unavailable."""

    def test_database_handles_missing_directory(self, tmp_path):
        """Database init should fail cleanly if path doesn't exist."""
        import sqlite3
        bad_path = tmp_path / "nonexistent" / "subdir" / "test.db"
        with pytest.raises((sqlite3.OperationalError, OSError)):
            conn = sqlite3.connect(str(bad_path))
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.close()

    def test_database_handles_readonly_directory(self, tmp_path):
        """Database init should fail if directory is read-only."""
        import sqlite3
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        os.chmod(ro_dir, 0o444)
        try:
            db_path = ro_dir / "test.db"
            with pytest.raises((sqlite3.OperationalError, OSError)):
                conn = sqlite3.connect(str(db_path))
                conn.execute("CREATE TABLE test (id INTEGER)")
                conn.commit()
                conn.close()
        finally:
            os.chmod(ro_dir, 0o755)


# =========================================================================
# EC7: First-run password race (concurrent /setup POST)
# =========================================================================

class TestSetupPasswordRace:
    """Only one concurrent setup request should win."""

    def test_complete_setup_idempotency_guard(self, mock_db):
        """complete_setup returns False if already completed."""
        from updater.auth import complete_setup
        from updater import database as db
        # Reset setup_completed to false (conftest defaults it to true)
        db.set_setting("setup_completed", "false")

        # First call should succeed
        result1 = complete_setup("password123")
        assert result1 is True
        # Second call should be rejected (already completed)
        result2 = complete_setup("different_password")
        assert result2 is False
        # Verify first password is still the one stored
        stored_hash = db.get_setting("admin_password_hash", "")
        assert _bcrypt.checkpw(b"password123", stored_hash.encode())
        assert not _bcrypt.checkpw(b"different_password", stored_hash.encode())

    def test_setup_submit_after_completion_redirects(self, client, mock_db):
        """POST /setup after setup is complete redirects."""
        # setup_completed is already "true" in conftest defaults
        resp = client.post("/setup", data={
            "new_password": "hackerpass1",
            "confirm_password": "hackerpass1",
        }, follow_redirects=False)
        # Should redirect (either to / or /login)
        assert resp.status_code in (302, 303)

    def test_concurrent_setup_single_winner(self, mock_db):
        """Two threads calling complete_setup: only one succeeds."""
        from updater.auth import complete_setup
        from updater import database as db
        # Reset setup_completed to false
        db.set_setting("setup_completed", "false")

        results = [None, None]

        def do_setup(idx, password):
            results[idx] = complete_setup(password)

        t1 = threading.Thread(target=do_setup, args=(0, "password_A1"))
        t2 = threading.Thread(target=do_setup, args=(1, "password_B2"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive(), "Thread t1 hung"
        assert not t2.is_alive(), "Thread t2 hung"

        # Exactly one should return True
        assert results.count(True) == 1
        assert results.count(False) == 1

        # The stored password should match the winner
        stored_hash = db.get_setting("admin_password_hash", "")
        if results[0] is True:
            assert _bcrypt.checkpw(b"password_A1", stored_hash.encode())
        else:
            assert _bcrypt.checkpw(b"password_B2", stored_hash.encode())

    def test_setup_completed_flag_set_atomically(self, mock_db):
        """setup_completed and password hash are written together."""
        from updater.auth import complete_setup
        from updater import database as db
        # Reset setup_completed to false
        db.set_setting("setup_completed", "false")

        complete_setup("mypassword1")
        # Both should be set
        assert db.get_setting("setup_completed") == "true"
        assert db.get_setting("admin_password_hash", "") != ""
        assert db.get_setting("schedule_enabled") == "true"
        assert db.get_setting("autoupdate_enabled") == "true"


# =========================================================================
# EC8: Bootstrap credential state mismatch
# =========================================================================

class TestCredentialStateMismatch:
    """No lockout regardless of DB/env password state permutations."""

    def test_fresh_install_setup_accessible(self, mock_db):
        """No hash, no env password → setup page accessible without auth."""
        from updater import database as db
        from updater.auth import is_first_run, is_setup_required
        db.set_setting("setup_completed", "false")
        db.set_setting("admin_password_hash", "")
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin"}, clear=False):
            env = os.environ.copy()
            env.pop("ADMIN_PASSWORD", None)
            with patch.dict(os.environ, env, clear=True):
                assert is_first_run() is True
                assert is_setup_required() is True

    def test_lockout_state_auto_recovers(self, mock_db):
        """setup_completed=true but no password → auto-reset to allow re-setup."""
        from updater import database as db
        from updater.auth import is_setup_required, is_first_run
        # Simulate lockout: setup done but password wiped
        db.set_setting("setup_completed", "true")
        db.set_setting("admin_password_hash", "")
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin"}, clear=False):
            env = os.environ.copy()
            env.pop("ADMIN_PASSWORD", None)
            with patch.dict(os.environ, env, clear=True):
                # is_first_run should be True (no password anywhere)
                assert is_first_run() is True
                # is_setup_required should auto-recover and return True
                assert is_setup_required() is True
                # setup_completed should have been reset
                assert db.get_setting("setup_completed") == "false"

    def test_env_password_with_incomplete_setup(self, mock_db):
        """ADMIN_PASSWORD set, setup_completed=false → login works, setup requires auth."""
        from updater import database as db
        from updater.auth import is_first_run, is_setup_required, authenticate_local
        db.set_setting("setup_completed", "false")
        db.set_setting("admin_password_hash", "")
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "bootstrap1"}):
            # Not first run because env password exists
            assert is_first_run() is False
            # Setup still required
            assert is_setup_required() is True
            # Login should work with env password
            assert authenticate_local("admin", "bootstrap1") is not None

    def test_db_hash_takes_precedence_over_env(self, mock_db):
        """Both DB hash and env password → DB hash used for auth."""
        from updater import database as db
        from updater.auth import authenticate_local
        hashed = _bcrypt.hashpw(b"db_password", _bcrypt.gensalt()).decode()
        db.set_setting("admin_password_hash", hashed)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "env_pass1"}):
            # DB hash wins
            assert authenticate_local("admin", "db_password") is not None
            # Env password should NOT work
            assert authenticate_local("admin", "env_pass1") is None

    def test_setup_complete_with_valid_hash(self, mock_db):
        """Normal state: setup done with valid hash → no setup required."""
        from updater import database as db
        from updater.auth import is_setup_required, is_first_run
        hashed = _bcrypt.hashpw(b"goodpasswd", _bcrypt.gensalt()).decode()
        db.set_setting("admin_password_hash", hashed)
        db.set_setting("setup_completed", "true")
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin"}, clear=False):
            env = os.environ.copy()
            env.pop("ADMIN_PASSWORD", None)
            with patch.dict(os.environ, env, clear=True):
                assert is_first_run() is False
                assert is_setup_required() is False


# =========================================================================
# EC9: Setup wizard partial/failed SSL + backup
# =========================================================================

class TestSetupWizardReplacement:
    """Setup wizard replaced by App Settings modal with auto-open on first run."""

    def test_first_run_passes_show_settings_flag(self, authed_client, mock_db):
        """When wizard not completed, index passes show_settings=True."""
        from updater import database as db
        db.set_setting("setup_wizard_completed", "false")

        with patch("updater.ssl_manager.get_ssl_status", return_value={"enabled": False}), \
             patch("updater.sftp_backup.get_backup_status", return_value={"enabled": False}):
            resp = authed_client.get("/")
        assert resp.status_code == 200
        # The template should have the first-run auto-open JS
        assert "_firstRunSetup" in resp.text

    def test_completed_wizard_no_auto_open(self, authed_client, mock_db):
        """When wizard already completed, show_settings is False."""
        from updater import database as db
        db.set_setting("setup_wizard_completed", "true")

        resp = authed_client.get("/")
        assert resp.status_code == 200
        # The auto-open setTimeout call should NOT be in the rendered page
        assert "_firstRunSetup = true" not in resp.text

    def test_ssl_setup_api_failure(self, authed_client, mock_db):
        """SSL setup API returns error on certificate failure."""
        with patch("updater.ssl_manager.obtain_certificate", new_callable=AsyncMock) as mock_cert:
            mock_cert.return_value = (False, "ACME server rejected domain")
            resp = authed_client.post("/api/ssl/setup", json={
                "domain": "example.com",
                "email": "admin@example.com",
            })
        assert resp.status_code == 400
        assert "ACME server rejected domain" in resp.json()["detail"]

    def test_wizard_complete_api(self, authed_client, mock_db):
        """POST /api/setup-wizard/complete marks setup done."""
        from updater import database as db
        db.set_setting("setup_wizard_completed", "false")

        resp = authed_client.post("/api/setup-wizard/complete")
        assert resp.status_code == 200
        assert db.get_setting("setup_wizard_completed") == "true"

    def test_wizard_get_redirects_to_index(self, authed_client, mock_db):
        """GET /setup-wizard now redirects to /."""
        resp = authed_client.get("/setup-wizard", follow_redirects=False)
        assert resp.status_code in (301, 302, 303, 307)


# =========================================================================
# EC10: OVA import hardware drift across hypervisors
# =========================================================================

class TestOVAHardwareDrift:
    """Generated OVA matches intended defaults and rejects invalid params."""

    def _parse_ovf(self, **kwargs):
        defaults = {
            "vmdk_filename": "test.vmdk",
            "vmdk_size": 1000000,
            "name": "test-appliance",
            "version": "1.0.0",
        }
        defaults.update(kwargs)
        xml_str = create_ova.generate_ovf(**defaults)
        return ET.fromstring(xml_str)

    def test_ovf_matches_workflow_defaults(self):
        """OVF with exact workflow params produces correct hardware values."""
        root = self._parse_ovf(cpus=2, memory_mb=1024, disk_capacity_bytes=8 * 1024**3)
        ns = {
            "rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData",
            "ovf": "http://schemas.dmtf.org/ovf/envelope/1",
        }

        # Verify CPU = 2
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "3":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "2"
                break
        else:
            pytest.fail("CPU item not found in OVF")

        # Verify Memory = 1024 MB
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "4":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "1024"
                break
        else:
            pytest.fail("Memory item not found in OVF")

        # Verify Disk = 8GB
        disk = root.find(".//ovf:Disk", ns)
        assert disk is not None
        assert disk.get("{http://schemas.dmtf.org/ovf/envelope/1}capacity") == str(8 * 1024**3)

    def test_ovf_rejects_negative_cpus(self):
        with pytest.raises(ValueError, match="cpus must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", cpus=-1)

    def test_ovf_rejects_zero_cpus(self):
        with pytest.raises(ValueError, match="cpus must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", cpus=0)

    def test_ovf_rejects_excessive_cpus(self):
        with pytest.raises(ValueError, match="cpus must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", cpus=64)

    def test_ovf_rejects_zero_memory(self):
        with pytest.raises(ValueError, match="memory_mb must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", memory_mb=0)

    def test_ovf_rejects_tiny_memory(self):
        with pytest.raises(ValueError, match="memory_mb must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", memory_mb=128)

    def test_ovf_rejects_excessive_memory(self):
        with pytest.raises(ValueError, match="memory_mb must be between"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", memory_mb=100000)

    def test_ovf_rejects_tiny_disk(self):
        with pytest.raises(ValueError, match="disk_capacity_bytes must be at least"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0", disk_capacity_bytes=100)

    def test_ovf_rejects_sub_gigabyte_disk(self):
        with pytest.raises(ValueError, match="disk_capacity_bytes must be at least"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0",
                                     disk_capacity_bytes=512 * 1024 * 1024)

    def test_ovf_vmxnet3_nic_for_hypervisor_compat(self):
        """NIC type must be VMXNET3 for VMware/Proxmox compatibility."""
        root = self._parse_ovf()
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "10":
                subtype = item.find("rasd:ResourceSubType", ns)
                assert subtype.text == "VMXNET3"
                return
        pytest.fail("Ethernet adapter not found in OVF")

    def test_ovf_accepts_valid_boundary_values(self):
        """Boundary values (1 CPU, 256MB, 1GB disk) should be accepted."""
        root = self._parse_ovf(cpus=1, memory_mb=256, disk_capacity_bytes=1073741824)
        assert root is not None

    def test_ovf_accepts_max_boundary_values(self):
        """Max boundary values (16 CPUs, 65536MB) should be accepted."""
        root = self._parse_ovf(cpus=16, memory_mb=65536, disk_capacity_bytes=100 * 1024**3)
        assert root is not None

    def test_negative_disk_size_caught_by_ovf_validation(self):
        """Negative disk size from parse_disk_size is caught by generate_ovf validation."""
        negative_bytes = create_ova.parse_disk_size("-8G")
        assert negative_bytes < 0
        with pytest.raises(ValueError, match="disk_capacity_bytes must be at least"):
            create_ova.generate_ovf("t.vmdk", 100, "app", "1.0",
                                     disk_capacity_bytes=negative_bytes)
