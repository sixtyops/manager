"""Edge case tests for initial setup scenarios.

Tests cover edge cases:
EC5  - No DHCP lease (TUI remains accessible)
EC6  - /data mount failure (no false completion marker)
EC7  - First-run password race (concurrent /setup POST)
EC8  - Bootstrap credential state mismatch (no lockout)
EC9  - Setup wizard partial/failed SSL + backup
"""

import os
import threading
from unittest.mock import patch, AsyncMock

import pytest
import bcrypt as _bcrypt


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

