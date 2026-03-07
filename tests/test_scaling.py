"""Tests for ISP-scale performance improvements."""

import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest


@pytest.fixture
def scaling_db(memory_db):
    """Memory DB populated with many devices for scaling tests."""
    # Insert 2 tower sites
    memory_db.execute(
        "INSERT INTO tower_sites (name, location) VALUES (?, ?)",
        ("Site-A", "Tower A"),
    )
    memory_db.execute(
        "INSERT INTO tower_sites (name, location) VALUES (?, ?)",
        ("Site-B", "Tower B"),
    )

    # Insert 20 APs across 2 sites
    for i in range(20):
        site_id = 1 if i < 10 else 2
        memory_db.execute(
            "INSERT INTO access_points (ip, tower_site_id, username, password, model, firmware_version, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"10.0.0.{i+1}", site_id, "admin", "pass", "TNA-30x", "1.0.0", 1),
        )

    # Insert 5 switches
    for i in range(5):
        memory_db.execute(
            "INSERT INTO switches (ip, tower_site_id, username, password, model, firmware_version, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"10.0.1.{i+1}", 1, "admin", "pass", "TNS-100", "1.0.0", 1),
        )

    # Insert 50 CPEs (some per AP)
    for i in range(50):
        ap_idx = i % 20
        memory_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, model, firmware_version, auth_status) VALUES (?, ?, ?, ?, ?)",
            (f"10.0.0.{ap_idx+1}", f"10.0.2.{i+1}", "TNA-30x", "1.0.0", "ok"),
        )

    # Insert job history
    for i in range(5):
        memory_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, success_count, failed_count, skipped_count, cancelled_count, devices_json, ap_cpe_map_json, device_roles_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"job-{i}", "2025-01-01T00:00:00", "2025-01-01T01:00:00", 3600.0, 10, 0, 0, 0, "{}", "{}", "{}"),
        )

    memory_db.commit()
    return memory_db


class TestBatchFetch:
    """Test batch-fetch database functions."""

    def test_get_all_access_points_dict(self, scaling_db, mock_db):
        import updater.database as db

        result = db.get_all_access_points_dict(enabled_only=True)
        assert isinstance(result, dict)
        assert len(result) == 20
        assert "10.0.0.1" in result
        assert result["10.0.0.1"]["model"] == "TNA-30x"

    def test_get_all_access_points_dict_enabled_filter(self, scaling_db, mock_db):
        import updater.database as db

        # Disable one AP
        scaling_db.execute("UPDATE access_points SET enabled = 0 WHERE ip = '10.0.0.1'")
        scaling_db.commit()

        result = db.get_all_access_points_dict(enabled_only=True)
        assert len(result) == 19
        assert "10.0.0.1" not in result

        result_all = db.get_all_access_points_dict(enabled_only=False)
        assert len(result_all) == 20

    def test_get_all_switches_dict(self, scaling_db, mock_db):
        import updater.database as db

        result = db.get_all_switches_dict(enabled_only=True)
        assert isinstance(result, dict)
        assert len(result) == 5
        assert "10.0.1.1" in result

    def test_get_all_cpes_grouped(self, scaling_db, mock_db):
        import updater.database as db

        result = db.get_all_cpes_grouped()
        assert isinstance(result, dict)
        # 20 APs, each should have some CPEs
        total_cpes = sum(len(v) for v in result.values())
        assert total_cpes == 50
        # First AP should have CPEs
        assert "10.0.0.1" in result
        assert len(result["10.0.0.1"]) > 0

    def test_get_job_history_paginated(self, scaling_db, mock_db):
        import updater.database as db

        items, total = db.get_job_history_paginated(page=1, per_page=2)
        assert total == 5
        assert len(items) == 2

        items2, total2 = db.get_job_history_paginated(page=2, per_page=2)
        assert total2 == 5
        assert len(items2) == 2
        # Different items
        assert items[0]["job_id"] != items2[0]["job_id"]

        # Last page
        items3, total3 = db.get_job_history_paginated(page=3, per_page=2)
        assert len(items3) == 1


class TestCircuitBreaker:
    """Test poller circuit breaker logic."""

    def test_initial_state_not_backed_off(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller()
        assert not poller._is_backed_off("10.0.0.1")

    def test_backoff_after_threshold(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller(poll_interval=60)
        # 3 failures triggers backoff
        poller._record_poll_failure("10.0.0.1")
        poller._record_poll_failure("10.0.0.1")
        assert not poller._is_backed_off("10.0.0.1")

        poller._record_poll_failure("10.0.0.1")
        assert poller._is_backed_off("10.0.0.1")

    def test_success_resets_backoff(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller(poll_interval=60)
        for _ in range(5):
            poller._record_poll_failure("10.0.0.1")
        assert poller._is_backed_off("10.0.0.1")

        poller._record_poll_success("10.0.0.1")
        assert not poller._is_backed_off("10.0.0.1")
        assert "10.0.0.1" not in poller._failure_counts

    def test_backoff_escalation(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller(poll_interval=1)
        # 3 failures = 2^0 = 1 cycle backoff
        for _ in range(3):
            poller._record_poll_failure("10.0.0.1")
        backoff1 = poller._backoff_until["10.0.0.1"]

        # 4 failures = 2^1 = 2 cycle backoff
        poller._record_poll_failure("10.0.0.1")
        backoff2 = poller._backoff_until["10.0.0.1"]
        assert backoff2 > backoff1

        # 5 failures = 2^2 = 4 cycle backoff
        poller._record_poll_failure("10.0.0.1")
        backoff3 = poller._backoff_until["10.0.0.1"]
        assert backoff3 > backoff2


class TestClientCache:
    """Test client cache with TTL."""

    def test_cache_and_retrieve(self):
        from updater.poller import NetworkPoller
        from unittest.mock import MagicMock

        poller = NetworkPoller()
        mock_client = MagicMock()
        poller._cache_client("10.0.0.1", mock_client)

        retrieved = poller._get_cached_client("10.0.0.1")
        assert retrieved is mock_client

    def test_cache_miss(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller()
        assert poller._get_cached_client("10.0.0.1") is None

    def test_remove_cached_client(self):
        from updater.poller import NetworkPoller
        from unittest.mock import MagicMock

        poller = NetworkPoller()
        poller._cache_client("10.0.0.1", MagicMock())
        poller._remove_cached_client("10.0.0.1")
        assert poller._get_cached_client("10.0.0.1") is None

    def test_evict_stale_by_ttl(self):
        from updater.poller import NetworkPoller, _CLIENT_TTL_SECONDS
        from unittest.mock import MagicMock

        poller = NetworkPoller()
        # Cache a client with old timestamp
        mock_client = MagicMock()
        poller._clients["10.0.0.1"] = (mock_client, time.time() - _CLIENT_TTL_SECONDS - 10)

        with patch("updater.database.get_all_device_ips", return_value={"10.0.0.1"}):
            poller._evict_stale_clients()

        assert "10.0.0.1" not in poller._clients

    def test_evict_stale_unknown_ip(self):
        from updater.poller import NetworkPoller
        from unittest.mock import MagicMock

        poller = NetworkPoller()
        poller._cache_client("10.0.0.99", MagicMock())

        with patch("updater.database.get_all_device_ips", return_value=set()):
            poller._evict_stale_clients()

        assert "10.0.0.99" not in poller._clients


class TestPollerOverlapGuard:
    """Test poll overlap prevention."""

    @pytest.mark.asyncio
    async def test_overlap_skips(self):
        from updater.poller import NetworkPoller

        poller = NetworkPoller()
        poller._poll_in_progress = True

        # Should return immediately without polling
        with patch("updater.database.get_access_points") as mock_get:
            await poller._poll_all_aps()
            mock_get.assert_not_called()


class TestJobCleanup:
    """Test job memory cleanup improvements."""

    def test_cleanup_caps_completed_jobs(self):
        from updater.app import _cleanup_completed_jobs, update_jobs
        from datetime import datetime, timedelta
        from unittest.mock import MagicMock

        # Clear and populate with 25 completed jobs
        update_jobs.clear()
        for i in range(25):
            job = MagicMock()
            job.status = "completed"
            job.completed_at = datetime.now() - timedelta(seconds=30)
            update_jobs[f"job-{i}"] = job

        _cleanup_completed_jobs(max_age_seconds=600)
        # Should cap at 20
        assert len(update_jobs) <= 20

        update_jobs.clear()
