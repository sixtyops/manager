"""Tests for config auto-enforce: scoping, enforce log, template resolution."""

import asyncio
import json
import math
from contextlib import contextmanager
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from updater import database as db
from updater.config_utils import filter_templates_by_device_type
from updater.poller import NetworkPoller, PHASE_ORDER


@pytest.fixture
def scoped_db(memory_db):
    """Memory DB with tower sites and devices pre-populated."""
    memory_db.execute(
        "INSERT INTO tower_sites (id, name) VALUES (1, 'Tower-Alpha')"
    )
    memory_db.execute(
        "INSERT INTO tower_sites (id, name) VALUES (2, 'Tower-Beta')"
    )
    memory_db.execute(
        "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.0.1', 1, 'admin', 'pass', 1)"
    )
    memory_db.execute(
        "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.0.2', 2, 'admin', 'pass', 1)"
    )
    memory_db.execute(
        "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.0.3', NULL, 'admin', 'pass', 1)"
    )
    memory_db.execute(
        "INSERT INTO switches (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.1.1', 1, 'admin', 'pass', 1)"
    )
    memory_db.commit()

    @contextmanager
    def _get_db():
        yield memory_db

    with patch("updater.database.get_db", _get_db):
        yield memory_db


# ---------------------------------------------------------------------------
# Template scoping tests
# ---------------------------------------------------------------------------

class TestTemplateScoping:
    def test_save_template_with_scope(self, scoped_db):
        tid = db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        t = db.get_config_template(tid)
        assert t["scope"] == "global"
        assert t["site_id"] is None

    def test_save_template_with_site_scope(self, scoped_db):
        tid = db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        t = db.get_config_template(tid)
        assert t["scope"] == "site"
        assert t["site_id"] == 1

    def test_save_template_with_device_types(self, scoped_db):
        tid = db.save_config_template(
            name="Custom JSON", category="custom",
            config_fragment='{"custom":"value"}',
            device_types='["ap","switch"]'
        )
        t = db.get_config_template(tid)
        assert json.loads(t["device_types"]) == ["ap", "switch"]

    def test_update_template_scope(self, scoped_db):
        tid = db.save_config_template(
            name="NTP Global", category="ntp",
            config_fragment='{"services":{"ntp":{"enabled":true}}}'
        )
        db.update_config_template(tid, scope="site", site_id=2)
        t = db.get_config_template(tid)
        assert t["scope"] == "site"
        assert t["site_id"] == 2

    def test_global_template_resolves_for_all_devices(self, scoped_db):
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        # Device in site 1
        templates = db.get_config_templates_for_device("10.0.0.1", site_id=1)
        assert len(templates) == 1
        assert templates[0]["category"] == "snmp"

        # Device with no site
        templates = db.get_config_templates_for_device("10.0.0.3", site_id=None)
        assert len(templates) == 1

    def test_site_override_wins_over_global(self, scoped_db):
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        # Device in site 1 gets site override
        templates = db.get_config_templates_for_device("10.0.0.1", site_id=1)
        assert len(templates) == 1
        frag = json.loads(templates[0]["config_fragment"])
        assert frag["services"]["snmp"]["community"] == "secret"

        # Device in site 2 gets global (no site override for site 2)
        templates = db.get_config_templates_for_device("10.0.0.2", site_id=2)
        assert len(templates) == 1
        frag = json.loads(templates[0]["config_fragment"])
        assert frag["services"]["snmp"]["community"] == "public"

    def test_site_override_only_for_matching_category(self, scoped_db):
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        db.save_config_template(
            name="NTP Global", category="ntp",
            config_fragment='{"services":{"ntp":{"enabled":true}}}',
            scope="global"
        )
        db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        # Site 1 device: SNMP overridden, NTP from global
        templates = db.get_config_templates_for_device("10.0.0.1", site_id=1)
        assert len(templates) == 2
        cats = {t["category"]: json.loads(t["config_fragment"]) for t in templates}
        assert cats["snmp"]["services"]["snmp"]["community"] == "secret"
        assert cats["ntp"]["services"]["ntp"]["enabled"] is True

    def test_get_all_effective_templates(self, scoped_db):
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        result = db.get_all_effective_templates()
        # 4 devices total (3 APs + 1 switch)
        assert len(result) == 4
        # Site 1 devices get site override
        frag_10_0_0_1 = json.loads(result["10.0.0.1"][0]["config_fragment"])
        assert frag_10_0_0_1["services"]["snmp"]["community"] == "secret"
        # Site 1 switch also gets override
        frag_10_0_1_1 = json.loads(result["10.0.1.1"][0]["config_fragment"])
        assert frag_10_0_1_1["services"]["snmp"]["community"] == "secret"
        # Site 2 device gets global
        frag_10_0_0_2 = json.loads(result["10.0.0.2"][0]["config_fragment"])
        assert frag_10_0_0_2["services"]["snmp"]["community"] == "public"


# ---------------------------------------------------------------------------
# Enforce log tests
# ---------------------------------------------------------------------------

class TestEnforceLog:
    def test_save_and_get_log(self, scoped_db):
        db.save_config_enforce_log(
            ip="10.0.0.1", device_type="ap", phase="canary",
            status="success", template_ids=[1, 2]
        )
        logs = db.get_config_enforce_log()
        assert len(logs) == 1
        assert logs[0]["ip"] == "10.0.0.1"
        assert logs[0]["status"] == "success"
        assert logs[0]["phase"] == "canary"
        assert json.loads(logs[0]["template_ids"]) == [1, 2]

    def test_log_filter_by_ip(self, scoped_db):
        db.save_config_enforce_log(ip="10.0.0.1", device_type="ap", phase="canary", status="success")
        db.save_config_enforce_log(ip="10.0.0.2", device_type="ap", phase="canary", status="failed", error="timeout")
        logs = db.get_config_enforce_log(ip="10.0.0.2")
        assert len(logs) == 1
        assert logs[0]["status"] == "failed"
        assert logs[0]["error"] == "timeout"

    def test_log_limit(self, scoped_db):
        for i in range(10):
            db.save_config_enforce_log(
                ip=f"10.0.0.{i}", device_type="ap", phase="pct100", status="success"
            )
        logs = db.get_config_enforce_log(limit=5)
        assert len(logs) == 5

    def test_get_enforce_failures(self, scoped_db):
        db.save_config_enforce_log(ip="10.0.0.1", device_type="ap", phase="canary", status="success")
        db.save_config_enforce_log(
            ip="10.0.0.2", device_type="ap", phase="pct10",
            status="failed", error="login failed"
        )
        failures = db.get_enforce_failures(since_hours=24)
        assert len(failures) == 1
        assert failures[0]["ip"] == "10.0.0.2"
        assert failures[0]["error"] == "login failed"

    def test_no_failures_returns_empty(self, scoped_db):
        db.save_config_enforce_log(ip="10.0.0.1", device_type="ap", phase="canary", status="success")
        failures = db.get_enforce_failures(since_hours=24)
        assert len(failures) == 0


# ---------------------------------------------------------------------------
# Poller auto-enforce tests
# ---------------------------------------------------------------------------

class TestAutoEnforce:
    def test_phase_batch_sizes(self):
        poller = NetworkPoller()
        assert poller._phase_batch_size("canary", 100) == 1
        assert poller._phase_batch_size("pct10", 100) == 10
        assert poller._phase_batch_size("pct50", 100) == 50
        assert poller._phase_batch_size("pct100", 100) == 100
        # Small fleet
        assert poller._phase_batch_size("pct10", 3) == 1
        assert poller._phase_batch_size("pct50", 3) == 2

    def test_re_entrancy(self, scoped_db):
        """Second call while enforce is running should skip."""
        poller = NetworkPoller()
        poller._enforce_running = True

        import asyncio
        loop = asyncio.new_event_loop()
        # Should return immediately without error
        loop.run_until_complete(poller._auto_enforce_compliance())
        # Still running (not reset)
        assert poller._enforce_running is True
        loop.close()

    @pytest.mark.asyncio
    async def test_all_compliant_skips_enforce(self, scoped_db):
        """When all devices are compliant, no enforcement runs."""
        # Set up a template + matching config
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        # Store matching config for device 10.0.0.1
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id) VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", config, config_hash, "TN-110-PRS", "tn-110-prs")
        )
        scoped_db.commit()

        broadcast = AsyncMock()
        poller = NetworkPoller(broadcast_func=broadcast)
        await poller._auto_enforce_compliance()

        # Should broadcast idle/compliant status
        broadcast.assert_called()
        last_call = broadcast.call_args_list[-1][0][0]
        assert last_call["type"] == "config_enforce_status"
        assert last_call["status"] == "idle"

        # No enforce log entries
        logs = db.get_config_enforce_log()
        assert len(logs) == 0

    @pytest.mark.asyncio
    async def test_enforce_disabled_mid_run_stops(self, scoped_db):
        """Toggling auto-enforce off mid-run should stop after current phase."""
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="global"
        )
        # Non-compliant config
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        for ip in ["10.0.0.1", "10.0.0.2", "10.0.0.3"]:
            scoped_db.execute(
                "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id) VALUES (?, ?, ?, ?, ?)",
                (ip, config, config_hash, "TN-110-PRS", "tn-110-prs")
            )
        scoped_db.commit()

        # Set auto_enforce to false so it stops at phase check
        scoped_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('config_auto_enforce', 'false')"
        )
        scoped_db.commit()

        broadcast = AsyncMock()
        poller = NetworkPoller(broadcast_func=broadcast)
        await poller._run_enforce_phases()

        # Should have broadcast "stopped" status
        stopped_msgs = [
            c[0][0] for c in broadcast.call_args_list
            if c[0][0].get("status") == "stopped"
        ]
        assert len(stopped_msgs) >= 1

    @pytest.mark.asyncio
    async def test_skip_when_manual_push_in_flight(self, scoped_db):
        """Enforce must defer when an immediate /api/config-push is running."""
        from updater import app as updater_app
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="global"
        )
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id) VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", config, config_hash, "TN-110-PRS", "tn-110-prs")
        )
        scoped_db.commit()

        original_jobs = dict(updater_app._config_push_jobs)
        updater_app._config_push_jobs.clear()
        updater_app._config_push_jobs["job-in-flight"] = {"done": False}
        try:
            broadcast = AsyncMock()
            poller = NetworkPoller(broadcast_func=broadcast)
            await poller._run_enforce_phases()
        finally:
            updater_app._config_push_jobs.clear()
            updater_app._config_push_jobs.update(original_jobs)

        skipped_msgs = [
            c[0][0] for c in broadcast.call_args_list
            if c[0][0].get("status") == "skipped"
        ]
        assert len(skipped_msgs) == 1
        assert "manual config push" in skipped_msgs[0]["message"].lower()
        assert len(db.get_config_enforce_log()) == 0

    @pytest.mark.asyncio
    async def test_proceeds_when_only_completed_jobs(self, scoped_db):
        """Stale done=True entries must not block enforce."""
        from updater import app as updater_app
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        # Compliant config so the run reaches the idle broadcast quickly.
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id) VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", config, config_hash, "TN-110-PRS", "tn-110-prs")
        )
        scoped_db.commit()

        original_jobs = dict(updater_app._config_push_jobs)
        updater_app._config_push_jobs.clear()
        updater_app._config_push_jobs["job-done"] = {"done": True}
        try:
            broadcast = AsyncMock()
            poller = NetworkPoller(broadcast_func=broadcast)
            await poller._run_enforce_phases()
        finally:
            updater_app._config_push_jobs.clear()
            updater_app._config_push_jobs.update(original_jobs)

        skipped_msgs = [
            c[0][0] for c in broadcast.call_args_list
            if c[0][0].get("status") == "skipped"
        ]
        assert skipped_msgs == []


def test_has_active_config_push_helper():
    """`app.has_active_config_push` counts non-done jobs only."""
    from updater import app as updater_app

    original = dict(updater_app._config_push_jobs)
    updater_app._config_push_jobs.clear()
    try:
        assert updater_app.has_active_config_push() == 0
        updater_app._config_push_jobs["a"] = {"done": False}
        updater_app._config_push_jobs["b"] = {"done": True}
        updater_app._config_push_jobs["c"] = {"done": False}
        assert updater_app.has_active_config_push() == 2
    finally:
        updater_app._config_push_jobs.clear()
        updater_app._config_push_jobs.update(original)


# ---------------------------------------------------------------------------
# Canary retry / failure classification (#49)
# ---------------------------------------------------------------------------

class TestCanaryRetry:
    """Verify _run_enforce_phases classifies failures and retries transient
    ones on canary only."""

    def _seed_non_compliant(self, scoped_db, ips):
        """Seed a global SNMP template and non-matching device configs."""
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="global"
        )
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        for ip in ips:
            scoped_db.execute(
                "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id) VALUES (?, ?, ?, ?, ?)",
                (ip, config, config_hash, "TN-110-PRS", "tn-110-prs")
            )
        scoped_db.commit()
        # Use the setter so the settings cache is invalidated.
        db.set_setting("config_auto_enforce", "true")

    @pytest.mark.asyncio
    async def test_canary_transient_retried_and_succeeds(self, scoped_db):
        """A transient canary failure that succeeds on retry must not stop the run."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])

        poller = NetworkPoller()
        # First call fails transient, second call (the retry) succeeds.
        side_effects = [
            (False, "transient", "Login failed: timeout"),
            (True, None, None),
        ]

        async def fake_enforce(ip, dtype, templates, phase, sem):
            return side_effects.pop(0)

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        logs = db.get_config_enforce_log()
        # Only the second attempt's success log row is written by _enforce_device
        # (our patched fake doesn't write any). We don't assert on success rows
        # here; we assert that NO failure row was written, because the retry
        # succeeded and the caller only writes the failure row if the retry
        # also failed.
        failures = [r for r in logs if r["status"] == "failed"]
        assert failures == []
        # Both attempts were consumed: the retry happened.
        assert side_effects == []

    @pytest.mark.asyncio
    async def test_canary_policy_failure_no_retry(self, scoped_db):
        """A policy-class canary failure must NOT be retried and must stop the run."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1", "10.0.0.2"])

        poller = NetworkPoller(broadcast_func=AsyncMock())
        call_count = 0

        async def fake_enforce(ip, dtype, templates, phase, sem):
            nonlocal call_count
            call_count += 1
            return (False, "policy", "Dry run rejected: invalid syntax")

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        # Exactly one call to _enforce_device: canary, no retry.
        assert call_count == 1
        logs = db.get_config_enforce_log()
        failures = [r for r in logs if r["status"] == "failed"]
        assert len(failures) == 1
        assert failures[0]["error"].startswith("policy: ")
        assert "retries" not in failures[0]["error"]

    @pytest.mark.asyncio
    async def test_retry_count_zero_disables_retry(self, scoped_db):
        """Setting retry count to 0 reverts to current behavior (no retry on transient)."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])
        # Use the setter so the settings cache is invalidated.
        db.set_setting("config_enforce_canary_retry_count", "0")

        poller = NetworkPoller(broadcast_func=AsyncMock())
        call_count = 0

        async def fake_enforce(ip, dtype, templates, phase, sem):
            nonlocal call_count
            call_count += 1
            return (False, "transient", "Login failed: connection refused")

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        assert call_count == 1  # No retry
        logs = db.get_config_enforce_log()
        failures = [r for r in logs if r["status"] == "failed"]
        assert len(failures) == 1
        assert failures[0]["error"].startswith("transient: ")

    @pytest.mark.asyncio
    async def test_log_includes_retry_count_suffix(self, scoped_db):
        """When a transient canary failure exhausts retries, the log records the count."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])

        poller = NetworkPoller(broadcast_func=AsyncMock())
        call_count = 0

        async def fake_enforce(ip, dtype, templates, phase, sem):
            nonlocal call_count
            call_count += 1
            return (False, "transient", "Failed to fetch current config")

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        # 1 initial + 1 retry (default count is 1) = 2 attempts
        assert call_count == 2
        logs = db.get_config_enforce_log()
        failures = [r for r in logs if r["status"] == "failed"]
        assert len(failures) == 1
        assert "transient:" in failures[0]["error"]
        assert "(1/1 retries)" in failures[0]["error"]

    @pytest.mark.asyncio
    async def test_no_retry_in_non_canary_phases(self, scoped_db):
        """Transient failures outside canary must not be retried — wasted time."""
        # 10 non-compliant devices: canary=1, pct10=1, pct50=4, pct100=4.
        ips = [f"10.1.0.{i}" for i in range(1, 11)]
        for ip in ips:
            scoped_db.execute(
                "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, 'admin', 'pass', 1)",
                (ip,)
            )
        self._seed_non_compliant(scoped_db, ips)

        poller = NetworkPoller(broadcast_func=AsyncMock())
        canary_calls = 0
        pct10_calls = 0

        async def fake_enforce(ip, dtype, templates, phase, sem):
            nonlocal canary_calls, pct10_calls
            if phase == "canary":
                canary_calls += 1
                return (True, None, None)  # canary passes so we proceed
            if phase == "pct10":
                pct10_calls += 1
                return (False, "transient", "Login failed")
            return (False, "policy", "Should not reach here")

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        # pct10 phase should run exactly once per device (1 of 10 devices) — no retry.
        assert pct10_calls == 1


class TestEnforceErrorClassification:
    """Verify _enforce_device raises the right error class at each failure site.

    Note: the snapshot id (4th tuple element) is included to distinguish
    failures that captured a pre-enforce snapshot (dry-run, apply) from those
    that failed before the snapshot was taken (login, fetch-config).
    """

    @pytest.mark.asyncio
    async def test_login_failure_classified_transient(self, scoped_db):
        poller = NetworkPoller()

        # Patch the driver to fail login.
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(return_value="bad password")
        mock_client.get_hardware_id = MagicMock(return_value="tn-110-prs")
        mock_driver_cls = MagicMock(return_value=mock_client)

        with patch("updater.poller.get_driver", return_value=mock_driver_cls):
            sem = asyncio.Semaphore(1)
            result = await poller._enforce_device(
                "10.0.0.1", "ap", [{"id": 1, "config_fragment": "{}"}], "canary", sem
            )
        # Login failed before snapshot was captured
        assert result == (False, "transient", "Login failed: bad password", None)

    @pytest.mark.asyncio
    async def test_fetch_config_failure_classified_transient(self, scoped_db):
        poller = NetworkPoller()

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.get_config = AsyncMock(return_value=None)
        mock_client.get_hardware_id = MagicMock(return_value="tn-110-prs")
        mock_driver_cls = MagicMock(return_value=mock_client)

        with patch("updater.poller.get_driver", return_value=mock_driver_cls):
            sem = asyncio.Semaphore(1)
            result = await poller._enforce_device(
                "10.0.0.1", "ap", [{"id": 1, "config_fragment": "{}"}], "canary", sem
            )
        # Fetch failed before snapshot was captured
        assert result == (False, "transient", "Failed to fetch current config", None)

    @pytest.mark.asyncio
    async def test_dry_run_rejection_classified_policy(self, scoped_db):
        poller = NetworkPoller()

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.get_config = AsyncMock(return_value={"services": {"snmp": {"community": "old"}}})
        mock_client.apply_config = AsyncMock(return_value={"success": False, "error": "schema error"})
        mock_client.get_hardware_id = MagicMock(return_value="tn-110-prs")
        mock_driver_cls = MagicMock(return_value=mock_client)

        with patch("updater.poller.get_driver", return_value=mock_driver_cls):
            sem = asyncio.Semaphore(1)
            result = await poller._enforce_device(
                "10.0.0.1", "ap",
                [{"id": 1, "config_fragment": '{"services":{"snmp":{"community":"new"}}}'}],
                "canary", sem
            )
        # Snapshot was captured before dry-run rejection
        success, failure_class, error_msg, snapshot_id = result
        assert success is False
        assert failure_class == "policy"
        assert error_msg == "Dry run rejected: schema error"
        assert isinstance(snapshot_id, int) and snapshot_id > 0

    @pytest.mark.asyncio
    async def test_device_not_found_classified_policy(self, scoped_db):
        poller = NetworkPoller()
        sem = asyncio.Semaphore(1)
        result = await poller._enforce_device(
            "10.99.99.99", "ap", [{"id": 1, "config_fragment": "{}"}], "canary", sem
        )
        assert result == (False, "policy", "Device not found in database", None)


# ---------------------------------------------------------------------------
# Auto-rollback on post-enforce mass failure (#50)
# ---------------------------------------------------------------------------

class TestAutoRollback:
    """Verify the optional auto-rollback path runs when the post-enforce
    re-poll shows the last phase exceeded the configured failure threshold."""

    def _seed_non_compliant(self, scoped_db, ips):
        """Seed a global SNMP template and non-matching configs for the given IPs.

        Uses save_device_config (not raw SQL) so the snapshot timestamps match
        the local-time ISO format that the production code writes. Mixing
        SQLite's CURRENT_TIMESTAMP (UTC) with the Python local-time strings
        breaks `ORDER BY fetched_at DESC` when the host TZ is not UTC.
        """
        db.save_config_template(
            name="SNMP", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="global"
        )
        config = json.dumps({"services": {"snmp": {"community": "public"}}},
                            sort_keys=True, separators=(",", ":"))
        import hashlib
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        for ip in ips:
            db.save_device_config(ip, config, config_hash, "TN-110-PRS", "tn-110-prs")
        db.set_setting("config_auto_enforce", "true")

    @pytest.mark.asyncio
    async def test_no_rollback_when_threshold_zero(self, scoped_db):
        """Default (off): no rollback regardless of post-poll outcome."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])
        # Threshold stays at default "0".

        poller = NetworkPoller(broadcast_func=AsyncMock())
        rollback_called = MagicMock()

        async def fake_enforce(ip, dtype, templates, phase, sem):
            return (True, None, None, 999)  # apply success, fake snapshot id

        async def fake_rollback(ip, dtype, snapshot_id):
            rollback_called(ip, snapshot_id)
            return True

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "_auto_rollback_device", side_effect=fake_rollback, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        rollback_called.assert_not_called()
        logs = db.get_config_enforce_log()
        assert [r for r in logs if r["phase"] == "rollback"] == []

    @pytest.mark.asyncio
    async def test_rollback_fires_when_threshold_exceeded(self, scoped_db):
        """Post-poll still non-compliant for all of last phase → rollback runs."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1", "10.0.0.2"])
        db.set_setting("config_enforce_auto_rollback_threshold_pct", "50")

        poller = NetworkPoller(broadcast_func=AsyncMock())
        # Track snapshot id to verify it's passed through.
        rollback_calls = []

        async def fake_enforce(ip, dtype, templates, phase, sem):
            # Apply succeeds but device's stored config wasn't updated, so
            # the post-poll re-check below still finds non-compliance.
            return (True, None, None, 42 if ip == "10.0.0.1" else 43)

        async def fake_rollback(ip, dtype, snapshot_id):
            rollback_calls.append((ip, snapshot_id))
            db.save_config_enforce_log(ip, dtype, "rollback", "success")
            return True

        # Default: device_configs are still non-compliant after re-poll.
        # poll_configs_for_ips is a no-op in tests.
        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "_auto_rollback_device", side_effect=fake_rollback, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        # With 2 non-compliant devices: canary=1 (pushed first), pct10=1.
        # pct100 has 0 remaining devices, so last_phase_ips is the final
        # non-empty batch (pct10 with 10.0.0.2 — or whichever IP got pushed
        # in the final non-empty batch). Both devices remain non-compliant
        # post-poll because we don't actually update the snapshot.
        assert len(rollback_calls) >= 1, f"Expected rollback, got: {rollback_calls}"
        rollback_logs = [
            r for r in db.get_config_enforce_log() if r["phase"] == "rollback"
        ]
        assert len(rollback_logs) >= 1
        assert all(r["status"] == "success" for r in rollback_logs)

    @pytest.mark.asyncio
    async def test_no_rollback_when_devices_now_compliant(self, scoped_db):
        """If the re-poll shows the last-phase devices are compliant, no rollback."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])
        db.set_setting("config_enforce_auto_rollback_threshold_pct", "50")

        # Stub poll_configs_for_ips to overwrite the snapshot with a compliant one.
        async def fake_poll(ips):
            compliant_config = json.dumps(
                {"services": {"snmp": {"community": "secret"}}},
                sort_keys=True, separators=(",", ":")
            )
            import hashlib
            h = hashlib.sha256(compliant_config.encode()).hexdigest()
            for ip in ips:
                db.save_device_config(ip, compliant_config, h, "TN-110-PRS", "tn-110-prs")

        poller = NetworkPoller(broadcast_func=AsyncMock())
        rollback_called = MagicMock()

        async def fake_enforce(ip, dtype, templates, phase, sem):
            return (True, None, None, 99)

        async def fake_rollback(ip, dtype, snapshot_id):
            rollback_called(ip, snapshot_id)
            return True

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "_auto_rollback_device", side_effect=fake_rollback, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", side_effect=fake_poll, autospec=False), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        rollback_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_rolled_back_broadcast_emitted(self, scoped_db):
        """When rollback fires, broadcast status=rolled_back with the rolled count."""
        self._seed_non_compliant(scoped_db, ["10.0.0.1"])
        db.set_setting("config_enforce_auto_rollback_threshold_pct", "50")

        broadcast = AsyncMock()
        poller = NetworkPoller(broadcast_func=broadcast)

        async def fake_enforce(ip, dtype, templates, phase, sem):
            return (True, None, None, 42)

        async def fake_rollback(ip, dtype, snapshot_id):
            db.save_config_enforce_log(ip, dtype, "rollback", "success")
            return True

        with patch.object(NetworkPoller, "_enforce_device", side_effect=fake_enforce, autospec=False), \
             patch.object(NetworkPoller, "_auto_rollback_device", side_effect=fake_rollback, autospec=False), \
             patch.object(NetworkPoller, "poll_configs_for_ips", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            await poller._run_enforce_phases()

        rolled_back_msgs = [
            c[0][0] for c in broadcast.call_args_list
            if c[0][0].get("status") == "rolled_back"
        ]
        assert len(rolled_back_msgs) == 1
        assert "rollback" in rolled_back_msgs[0]["message"].lower()
        # The final "Enforce completed" broadcast must be suppressed when
        # rollback fires — operators shouldn't see a green "completed" event
        # right after the rollback.
        idle_completed = [
            c[0][0] for c in broadcast.call_args_list
            if c[0][0].get("status") == "idle"
            and c[0][0].get("message") == "Enforce completed"
        ]
        assert idle_completed == []


class TestAutoRollbackDevice:
    """Verify _auto_rollback_device's connect → dry-run → apply sequence and
    its enforce-log writes."""

    @pytest.mark.asyncio
    async def test_writes_success_log_on_apply(self, scoped_db):
        # Insert a snapshot
        snapshot_id = db.save_device_config(
            "10.0.0.1",
            json.dumps({"services": {"snmp": {"community": "old"}}}),
            "abc123", "TN-110-PRS", "tn-110-prs"
        )

        poller = NetworkPoller()
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.apply_config = AsyncMock(return_value={"success": True})
        mock_driver_cls = MagicMock(return_value=mock_client)

        with patch("updater.poller.get_driver", return_value=mock_driver_cls):
            result = await poller._auto_rollback_device("10.0.0.1", "ap", snapshot_id)

        assert result is True
        # Two apply_config calls: dry-run and real apply
        assert mock_client.apply_config.call_count == 2
        rollback_logs = [
            r for r in db.get_config_enforce_log("10.0.0.1")
            if r["phase"] == "rollback"
        ]
        assert len(rollback_logs) == 1
        assert rollback_logs[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_writes_failed_log_on_dry_run_rejection(self, scoped_db):
        snapshot_id = db.save_device_config(
            "10.0.0.1",
            json.dumps({"services": {"snmp": {"community": "old"}}}),
            "abc123", "TN-110-PRS", "tn-110-prs"
        )

        poller = NetworkPoller()
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(return_value=True)
        # Dry-run rejection
        mock_client.apply_config = AsyncMock(return_value={"success": False, "error": "bad schema"})
        mock_driver_cls = MagicMock(return_value=mock_client)

        with patch("updater.poller.get_driver", return_value=mock_driver_cls):
            result = await poller._auto_rollback_device("10.0.0.1", "ap", snapshot_id)

        assert result is False
        rollback_logs = [
            r for r in db.get_config_enforce_log("10.0.0.1")
            if r["phase"] == "rollback"
        ]
        assert len(rollback_logs) == 1
        assert rollback_logs[0]["status"] == "failed"
        assert "Dry run rejected" in rollback_logs[0]["error"]


def test_save_device_config_returns_row_id(scoped_db):
    """save_device_config must return the new row id for the auto-rollback path."""
    rid1 = db.save_device_config(
        "10.0.0.1",
        json.dumps({"a": 1}, sort_keys=True),
        "hash1", "TN-110", "tn-110"
    )
    rid2 = db.save_device_config(
        "10.0.0.1",
        json.dumps({"a": 2}, sort_keys=True),
        "hash2", "TN-110", "tn-110"
    )
    assert isinstance(rid1, int) and rid1 > 0
    assert isinstance(rid2, int) and rid2 > rid1
    # Confirm we can round-trip the id back to the snapshot
    snap = db.get_device_config_by_id(rid1)
    assert snap is not None
    assert snap["ip"] == "10.0.0.1"


# ---------------------------------------------------------------------------
# Fix validation tests
# ---------------------------------------------------------------------------

class TestEnforceLogCleanup:
    def test_cleanup_old_entries(self, scoped_db):
        """Enforce log entries older than max_age_days are purged."""
        from datetime import datetime, timedelta
        old_date = (datetime.now() - timedelta(days=100)).isoformat()
        scoped_db.execute(
            "INSERT INTO config_enforce_log (ip, device_type, phase, status, enforced_at) VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", "ap", "canary", "success", old_date)
        )
        db.save_config_enforce_log(ip="10.0.0.2", device_type="ap", phase="canary", status="success")
        scoped_db.commit()

        assert len(db.get_config_enforce_log()) == 2
        db.cleanup_old_config_enforce_log(max_age_days=90)
        remaining = db.get_config_enforce_log()
        assert len(remaining) == 1
        assert remaining[0]["ip"] == "10.0.0.2"


class TestEffectiveTemplatesPerformance:
    def test_single_query_approach(self, scoped_db):
        """get_all_effective_templates uses single query, not N+1."""
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        result = db.get_all_effective_templates()
        # Site 1 devices get site override
        frag = json.loads(result["10.0.0.1"][0]["config_fragment"])
        assert frag["services"]["snmp"]["community"] == "secret"
        # Site 2 device gets global
        frag = json.loads(result["10.0.0.2"][0]["config_fragment"])
        assert frag["services"]["snmp"]["community"] == "public"


class TestDeviceTypesFiltering:
    def test_device_types_respected_in_template_resolution(self, scoped_db):
        """Templates with device_types should only apply to matching device types."""
        # Custom template only for switches
        db.save_config_template(
            name="Switch Custom", category="custom",
            config_fragment='{"custom":{"switch_only":true}}',
            scope="global",
            device_types='["switch"]'
        )
        # SNMP for all
        db.save_config_template(
            name="SNMP Global", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"public"}}}',
            scope="global"
        )
        # All devices get both templates from get_config_templates_for_device
        # (device_types filtering happens in the poller, not the DB query)
        templates = db.get_config_templates_for_device("10.0.0.1", site_id=1)
        assert len(templates) == 2  # Both returned; poller filters by device_type


class TestFilterTemplatesByDeviceType:
    """Helper used by both auto-enforce and manual config push to drop
    templates whose device_types don't include the target's role."""

    def test_no_device_types_applies_to_all(self):
        templates = [{"id": 1, "name": "Global", "device_types": None}]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert [t["id"] for t in applicable] == [1]
        assert excluded == []

    def test_empty_string_device_types_applies_to_all(self):
        templates = [{"id": 1, "name": "Global", "device_types": ""}]
        applicable, excluded = filter_templates_by_device_type(templates, "switch")
        assert [t["id"] for t in applicable] == [1]
        assert excluded == []

    def test_ap_only_excludes_switch(self):
        templates = [{"id": 1, "name": "AP-only", "device_types": '["ap"]'}]
        applicable, excluded = filter_templates_by_device_type(templates, "switch")
        assert applicable == []
        assert [t["id"] for t in excluded] == [1]

    def test_switch_only_excludes_ap(self):
        templates = [{"id": 1, "name": "Switch-only", "device_types": '["switch"]'}]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert applicable == []
        assert [t["id"] for t in excluded] == [1]

    def test_multi_type_match(self):
        templates = [{"id": 1, "name": "AP+Switch", "device_types": '["ap","switch"]'}]
        for dtype in ("ap", "switch"):
            applicable, excluded = filter_templates_by_device_type(templates, dtype)
            assert [t["id"] for t in applicable] == [1]
            assert excluded == []
        applicable, excluded = filter_templates_by_device_type(templates, "cpe")
        assert applicable == []
        assert [t["id"] for t in excluded] == [1]

    def test_device_types_already_a_list(self):
        # Templates loaded from a rollout snapshot may already be a list,
        # not a JSON string.
        templates = [{"id": 1, "name": "AP-only", "device_types": ["ap"]}]
        applicable, _ = filter_templates_by_device_type(templates, "ap")
        assert [t["id"] for t in applicable] == [1]
        applicable, excluded = filter_templates_by_device_type(templates, "switch")
        assert applicable == []
        assert [t["id"] for t in excluded] == [1]

    def test_malformed_device_types_treated_as_unrestricted(self):
        # Don't surface "skipped" noise on bad data; same forgiving behavior
        # as the prior inline auto-enforce filter.
        templates = [{"id": 1, "name": "Bad", "device_types": "not-json"}]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert [t["id"] for t in applicable] == [1]
        assert excluded == []

    def test_empty_list_treated_as_unrestricted(self):
        templates = [{"id": 1, "name": "EmptyList", "device_types": "[]"}]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert [t["id"] for t in applicable] == [1]
        assert excluded == []

    def test_cpe_role(self):
        templates = [{"id": 1, "name": "CPE-only", "device_types": '["cpe"]'}]
        applicable, _ = filter_templates_by_device_type(templates, "cpe")
        assert [t["id"] for t in applicable] == [1]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert applicable == []
        assert [t["id"] for t in excluded] == [1]

    def test_mixed_set_partial_filter(self):
        templates = [
            {"id": 1, "name": "Global", "device_types": None},
            {"id": 2, "name": "AP-only", "device_types": '["ap"]'},
            {"id": 3, "name": "Switch-only", "device_types": '["switch"]'},
        ]
        applicable, excluded = filter_templates_by_device_type(templates, "ap")
        assert [t["id"] for t in applicable] == [1, 2]
        assert [t["id"] for t in excluded] == [3]


class TestPrefillScopeAware:
    def test_site_template_doesnt_block_global_prefill(self, scoped_db):
        """A site-scoped template shouldn't prevent global prefill suggestions."""
        db.save_config_template(
            name="SNMP Site-Alpha", category="snmp",
            config_fragment='{"services":{"snmp":{"community":"secret"}}}',
            scope="site", site_id=1
        )
        # Global lookup should return None (no global template)
        result = db.get_config_template_by_category("snmp", scope="global")
        assert result is None

        # Without scope filter, it should find the site template
        result = db.get_config_template_by_category("snmp")
        assert result is not None
        assert result["scope"] == "site"


# ---------------------------------------------------------------------------
# Config-poll catch-up tests (issue #41)
# ---------------------------------------------------------------------------


class TestConfigPollCatchup:
    """`_maybe_poll_configs` should catch up if the manager was down through
    the configured poll window, and `last_config_poll_at` should survive a
    process restart so the catch-up decision is correct."""

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self, scoped_db):
        # scoped_db doesn't invalidate the in-process settings cache between
        # tests, so a setting written by an earlier test would leak into
        # later ones. Drop the cache before each test in this class.
        from updater import database as db_mod
        db_mod._invalidate_settings_cache()
        yield

    @pytest.mark.asyncio
    async def test_record_persists_to_settings(self, scoped_db):
        poller = NetworkPoller()
        poller._record_last_config_poll()
        # In-memory + persisted both updated
        assert poller._last_config_poll is not None
        assert db.get_setting("last_config_poll_at") is not None

    def test_hydrate_loads_persisted_value(self, scoped_db):
        from datetime import datetime
        ts = datetime(2026, 5, 1, 4, 0, 0)
        db.set_setting("last_config_poll_at", ts.isoformat())
        poller = NetworkPoller()
        poller._hydrate_last_config_poll()
        assert poller._last_config_poll == ts

    def test_hydrate_handles_missing_setting(self, scoped_db):
        poller = NetworkPoller()
        poller._hydrate_last_config_poll()
        assert poller._last_config_poll is None

    def test_hydrate_handles_corrupt_value(self, scoped_db):
        db.set_setting("last_config_poll_at", "not-a-timestamp")
        poller = NetworkPoller()
        poller._hydrate_last_config_poll()
        assert poller._last_config_poll is None

    @pytest.mark.asyncio
    async def test_catchup_runs_when_last_poll_is_stale(self, scoped_db):
        """If last poll was >25h ago, run a catch-up poll regardless of the
        current hour — the catch-up branch fires before the hour gate."""
        from datetime import datetime, timedelta

        # Force config_auto_enforce off so the catch-up branch returns after
        # poll_all_configs without trying to enforce
        db.set_setting("config_auto_enforce", "false")

        poller = NetworkPoller()
        # Pretend last poll was 30 hours ago (manager was down for the window)
        poller._last_config_poll = datetime.now() - timedelta(hours=30)
        poller._last_config_poll_hydrated = True

        with patch.object(poller, "poll_all_configs", new=AsyncMock()) as poll:
            await poller._maybe_poll_configs()

        poll.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_catchup_when_recent_poll_outside_window(self, scoped_db):
        """A poll within the last 25h is not stale — don't catch up. The
        existing hour gate also skips when we're outside the target hour."""
        from datetime import datetime, timedelta

        poller = NetworkPoller()
        # 10h ago — well within the 25h freshness window
        poller._last_config_poll = datetime.now() - timedelta(hours=10)
        poller._last_config_poll_hydrated = True

        # Pin target hour to one that is definitely not "now" so the hour
        # gate also rejects (otherwise this test would be time-of-day flaky)
        next_hour = (datetime.now().hour + 6) % 24
        db.set_setting("config_enforce_hour", str(next_hour))

        with patch.object(poller, "poll_all_configs", new=AsyncMock()) as poll:
            await poller._maybe_poll_configs()

        poll.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_catchup_on_fresh_install(self, scoped_db):
        """First-ever startup (no persisted timestamp) should not trigger an
        immediate catch-up — wait for the daily window. `_poll_missing_configs`
        handles the brand-new-device case separately."""
        from datetime import datetime

        poller = NetworkPoller()
        poller._last_config_poll_hydrated = True
        poller._last_config_poll = None

        next_hour = (datetime.now().hour + 6) % 24
        db.set_setting("config_enforce_hour", str(next_hour))

        with patch.object(poller, "poll_all_configs", new=AsyncMock()) as poll:
            await poller._maybe_poll_configs()

        poll.assert_not_called()

    def test_hydrate_falls_back_to_latest_config_when_setting_missing(self, scoped_db):
        """Upgrade path: setting wasn't written by an older build, but
        `device_configs` has live rows. Hydrate should pick MAX(fetched_at)."""
        from datetime import datetime

        ts = "2026-05-01T04:00:00"
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", ts),
        )
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.2", "{}", "h", "2026-04-15T04:00:00"),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll == datetime.fromisoformat(ts)
        assert poller._last_config_poll_hydrated is True

    def test_hydrate_corrupt_setting_falls_back_to_rows(self, scoped_db):
        """Corrupt persisted setting plus live config rows: fall back to
        MAX(fetched_at) instead of leaving `_last_config_poll` as None.
        `test_hydrate_handles_corrupt_value` covers the no-rows case; this
        locks in the precedence (parse-fail ⇒ try fallback) when rows do
        exist."""
        from datetime import datetime

        db.set_setting("last_config_poll_at", "not-a-timestamp")
        ts = "2026-05-01T04:00:00"
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", ts),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll == datetime.fromisoformat(ts)

    def test_hydrate_setting_wins_over_rows_when_present(self, scoped_db):
        """Precedence contract: a parseable setting is authoritative even
        if `device_configs` has a newer row. The setting means "manager
        observed a poll completion"; rows can advance ahead of it (e.g.,
        priming runs) and shouldn't override the canonical value."""
        from datetime import datetime

        setting_ts = datetime(2026, 4, 1, 4, 0, 0)
        db.set_setting("last_config_poll_at", setting_ts.isoformat())
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", "2026-05-01T04:00:00"),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll == setting_ts

    def test_hydrate_fallback_ignores_soft_deleted_rows(self, scoped_db):
        """If every config row is soft-deleted, no fallback timestamp.
        Preserves fresh-install semantics for the recycle-bin-only state."""
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", "2026-05-01T04:00:00", "2026-05-02T00:00:00"),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll is None

    def test_hydrate_fallback_handles_utc_space_separator(self, scoped_db):
        """`CURRENT_TIMESTAMP` writes naive UTC with a space separator. The
        fallback parser must accept it without crashing and the resulting
        local-time datetime must not be in the future."""
        from datetime import datetime

        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", "2026-05-01 04:00:00"),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll is not None
        assert poller._last_config_poll <= datetime.now()

    def test_hydrate_fallback_clamps_future_timestamps(self, scoped_db):
        """A future-dated `fetched_at` (clock skew or residual UTC quirk)
        must not produce a `_last_config_poll` newer than now, otherwise
        `stale_for` goes negative and catch-up never fires."""
        from datetime import datetime, timedelta

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", future),
        )
        scoped_db.commit()

        poller = NetworkPoller()
        poller._hydrate_last_config_poll()

        assert poller._last_config_poll is not None
        assert poller._last_config_poll <= datetime.now()

    @pytest.mark.asyncio
    async def test_catchup_runs_after_version_transition(self, scoped_db):
        """Manager upgraded from a build that didn't persist
        `last_config_poll_at`: setting absent, but device_configs has stale
        rows. Catch-up branch should fire on next tick instead of waiting
        for the daily window."""
        from datetime import datetime, timedelta

        stale = (datetime.now() - timedelta(hours=30)).isoformat()
        scoped_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "{}", "h", stale),
        )
        scoped_db.commit()

        db.set_setting("config_auto_enforce", "false")
        next_hour = (datetime.now().hour + 6) % 24
        db.set_setting("config_enforce_hour", str(next_hour))

        poller = NetworkPoller()
        with patch.object(poller, "poll_all_configs", new=AsyncMock()) as poll:
            await poller._maybe_poll_configs()

        poll.assert_awaited_once()


# ---------------------------------------------------------------------------
# Per-device polling-failure surfacing (issue #52)
# ---------------------------------------------------------------------------


class TestFetchConfigClassification:
    """`TachyonClient.fetch_config` returns (config, status, error_msg) so the
    poller can persist *why* a config-poll attempt failed."""

    @pytest.mark.asyncio
    async def test_ok_status_on_successful_poll(self):
        from updater.vendors.tachyon.client import TachyonClient
        client = TachyonClient("10.0.0.1", "root", "pass")
        with patch.object(client, "_curl", new=AsyncMock(return_value=(200, '{"x": 1}'))):
            config, status, err = await client.fetch_config()
        assert status == "ok"
        assert config == {"x": 1}
        assert err is None

    @pytest.mark.asyncio
    async def test_http_status_when_non_200(self):
        from updater.vendors.tachyon.client import TachyonClient
        client = TachyonClient("10.0.0.1", "root", "pass")
        with patch.object(client, "_curl", new=AsyncMock(return_value=(401, "auth failed"))):
            config, status, err = await client.fetch_config()
        assert status == "http_status"
        assert config is None
        assert "401" in err

    @pytest.mark.asyncio
    async def test_json_decode_when_body_unparseable(self):
        from updater.vendors.tachyon.client import TachyonClient
        client = TachyonClient("10.0.0.1", "root", "pass")
        with patch.object(client, "_curl", new=AsyncMock(return_value=(200, "not-json"))):
            config, status, err = await client.fetch_config()
        assert status == "json_decode"
        assert config is None
        assert err is not None

    @pytest.mark.asyncio
    async def test_timeout_classified_explicitly(self):
        import asyncio
        from updater.vendors.tachyon.client import TachyonClient
        client = TachyonClient("10.0.0.1", "root", "pass")
        with patch.object(client, "_curl", new=AsyncMock(side_effect=asyncio.TimeoutError())):
            config, status, err = await client.fetch_config()
        assert status == "timeout"
        assert config is None

    @pytest.mark.asyncio
    async def test_curl_runtime_error_classified_unknown(self):
        from updater.vendors.tachyon.client import TachyonClient
        client = TachyonClient("10.0.0.1", "root", "pass")
        with patch.object(client, "_curl", new=AsyncMock(side_effect=RuntimeError("curl: connection refused"))):
            config, status, err = await client.fetch_config()
        assert status == "unknown"
        assert "connection refused" in err


class TestPollerWritesPollStatus:
    """Issue #52: `_fetch_and_store_config` should persist the per-device
    poll outcome so the dashboard can surface failures with a reason."""

    @pytest.mark.asyncio
    async def test_records_ok_status_on_success(self, scoped_db):
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value=True)
        fake_client.fetch_config = AsyncMock(return_value=({"x": 1}, "ok", None))
        fake_client.get_hardware_id = MagicMock(return_value="tn-110")
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass", "TNA-301")
        row = scoped_db.execute(
            "SELECT last_config_poll_status, last_config_poll_error "
            "FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "ok"
        assert row["last_config_poll_error"] is None

    @pytest.mark.asyncio
    async def test_records_failure_classification(self, scoped_db):
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value=True)
        fake_client.fetch_config = AsyncMock(
            return_value=(None, "http_status", "HTTP 500")
        )
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status, last_config_poll_error "
            "FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "http_status"
        assert row["last_config_poll_error"] == "HTTP 500"

    @pytest.mark.asyncio
    async def test_records_auth_failure_when_login_fails(self, scoped_db):
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value="bad password")
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "wrong")
        row = scoped_db.execute(
            "SELECT last_config_poll_status, last_config_poll_error "
            "FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "auth"
        assert "bad password" in row["last_config_poll_error"]

    @pytest.mark.asyncio
    async def test_http_401_reclassified_as_auth(self, scoped_db):
        """A 401/403 mid-session (cookie expired) is an auth problem, not a
        generic HTTP one — should land as `auth` for clearer operator signal."""
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value=True)
        fake_client.fetch_config = AsyncMock(
            return_value=(None, "http_status", "HTTP 401")
        )
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "auth"

    @pytest.mark.asyncio
    async def test_http_403_reclassified_as_auth(self, scoped_db):
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value=True)
        fake_client.fetch_config = AsyncMock(
            return_value=(None, "http_status", "HTTP 403")
        )
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "auth"

    @pytest.mark.asyncio
    async def test_http_500_stays_as_http_status(self, scoped_db):
        """Non-auth HTTP errors should stay classified as http_status."""
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(return_value=True)
        fake_client.fetch_config = AsyncMock(
            return_value=(None, "http_status", "HTTP 500")
        )
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "http_status"

    @pytest.mark.asyncio
    async def test_records_unknown_when_connect_throws(self, scoped_db):
        """A connect()-raised exception (network unreachable, timeout, SSL
        error, etc.) used to bypass every status write and leave the device
        looking like it had never been polled. The outer except now writes
        an `unknown` status so operators see the failure."""
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(side_effect=TimeoutError("network unreachable"))
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("10.0.0.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status, last_config_poll_error "
            "FROM devices WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "unknown"
        assert "network unreachable" in row["last_config_poll_error"]

    @pytest.mark.asyncio
    async def test_records_unknown_for_cpe_when_connect_throws(self, scoped_db):
        """Same path for CPEs — `_fetch_and_store_config` is shared across
        roles, and the new outcome write must reach `cpe_cache` too."""
        from updater import database as db
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1"})
        poller = NetworkPoller()
        fake_client = MagicMock()
        fake_client.connect = AsyncMock(side_effect=ConnectionRefusedError("ECONNREFUSED"))
        with patch("updater.poller.get_driver", return_value=lambda *a, **kw: fake_client):
            await poller._fetch_and_store_config("1.1.1.1", "root", "pass")
        row = scoped_db.execute(
            "SELECT last_config_poll_status, last_config_poll_error "
            "FROM cpe_cache WHERE ip = '1.1.1.1'"
        ).fetchone()
        assert row["last_config_poll_status"] == "unknown"
        assert "ECONNREFUSED" in row["last_config_poll_error"]



class TestNormalizeUserPasswords:
    """Tachyon's write validator only knows the JSON key `password` —
    `password_hash` is silently dropped. For users that don't already
    exist on the device (e.g. when a Users template merge expands the
    user list), `password` must be a 34-char `$1$<salt>$<hash>` MD5
    crypt string. `_normalize_user_passwords` hashes plaintext values
    in place before send. Tested for every state the merge can hand it."""

    @pytest.fixture
    def normalize(self):
        from updater.vendors.tachyon.client import _normalize_user_passwords
        return _normalize_user_passwords

    def test_plaintext_gets_hashed(self, normalize):
        cfg = {"system": {"users": [{"username": "root", "password": "secret"}]}}
        normalize(cfg)
        pw = cfg["system"]["users"][0]["password"]
        assert pw.startswith("$1$")
        # $1$ + 8-char salt + $ + 22-char hash = 34
        assert len(pw) == 34

    def test_existing_crypt_hash_passes_through(self, normalize):
        # If a prior cycle already hashed the password (or the operator
        # pasted an `openssl passwd -1` value), don't re-hash. Re-hashing
        # would break the device's stored hash because crypt(plaintext)
        # uses a fresh random salt each time.
        already = "$1$abcdefgh$SbLlAj1nnaSEyBXEfwtQM/"
        cfg = {"system": {"users": [{"username": "root", "password": already}]}}
        normalize(cfg)
        assert cfg["system"]["users"][0]["password"] == already

    def test_empty_password_left_alone(self, normalize):
        # Tachyon treats empty `password` as "no change". Don't hash it
        # (would store a hash of the empty string, not the device's
        # current password).
        cfg = {"system": {"users": [{"username": "root", "password": ""}]}}
        normalize(cfg)
        assert cfg["system"]["users"][0]["password"] == ""

    def test_missing_password_field_left_alone(self, normalize):
        cfg = {"system": {"users": [{"username": "root", "enabled": True}]}}
        normalize(cfg)
        assert "password" not in cfg["system"]["users"][0]

    def test_no_users_section(self, normalize):
        cfg = {"system": {"hostname": "ap-1"}, "services": {}}
        normalize(cfg)
        assert cfg == {"system": {"hostname": "ap-1"}, "services": {}}

    def test_no_system_section(self, normalize):
        cfg = {"services": {"snmp": {"enabled": True}}}
        normalize(cfg)
        assert cfg == {"services": {"snmp": {"enabled": True}}}

    def test_mixed_users_each_handled_independently(self, normalize):
        already = "$1$abcdefgh$SbLlAj1nnaSEyBXEfwtQM/"
        cfg = {"system": {"users": [
            {"username": "root", "password": "plain1"},
            {"username": "admin", "password": already},
            {"username": "rwapi", "password": "plain2"},
        ]}}
        normalize(cfg)
        users = cfg["system"]["users"]
        assert users[0]["password"].startswith("$1$") and users[0]["password"] != "plain1"
        assert users[1]["password"] == already  # untouched
        assert users[2]["password"].startswith("$1$") and users[2]["password"] != "plain2"

    def test_non_string_password_skipped(self, normalize):
        # Defensive: a malformed template that put a non-string in
        # `password` shouldn't crash the push. Leave the field alone
        # so the device validator surfaces a clearer error.
        cfg = {"system": {"users": [{"username": "root", "password": None}]}}
        normalize(cfg)
        assert cfg["system"]["users"][0]["password"] is None


class TestHashTemplateUserPasswordsAtRest:
    """Storing operator-entered plaintext passwords for device users in
    `config_templates` was a soft-secret leak (SQL dumps, CSV backups, anyone
    with read access). Saved templates are now hashed in-place at the
    `$1$<salt>$<hash>` modular-crypt-MD5 format the device stores. These
    tests cover the helper that does the in-place hashing on save."""

    @pytest.fixture
    def hash_helper(self):
        from updater.config_utils import hash_template_user_passwords
        return hash_template_user_passwords

    def test_plaintext_hashed_in_both_fragment_and_form(self, hash_helper):
        frag = {"system": {"users": [{"username": "root", "password": "secret1"}]}}
        form = {"users": [{"username": "root", "password": "secret1"}]}
        hash_helper(frag, form, prior_fragment=None)
        assert frag["system"]["users"][0]["password"].startswith("$1$")
        assert len(frag["system"]["users"][0]["password"]) == 34
        assert form["users"][0]["password"].startswith("$1$")
        assert len(form["users"][0]["password"]) == 34

    def test_existing_crypt_hash_preserved(self, hash_helper):
        # If someone re-saves a template whose API response already contained
        # the stored hash (or an admin pasted an `openssl passwd -1` output),
        # we must not re-hash — that would generate a fresh salt and break
        # the device's stored credential.
        already = "$1$abcdefgh$SbLlAj1nnaSEyBXEfwtQM/"
        frag = {"system": {"users": [{"username": "root", "password": already}]}}
        hash_helper(frag, None, prior_fragment=None)
        assert frag["system"]["users"][0]["password"] == already

    def test_empty_password_with_prior_hash_preserved(self, hash_helper):
        # Operator left the password field blank — this is the "no change"
        # signal. Look up the username in `prior_fragment` and copy its
        # hash forward instead of dropping the credential.
        prior_hash = "$1$xxxxxxxx$AaaaaaaaaaaaaaaaaaaaA0"
        prior = {"system": {"users": [{"username": "root", "password": prior_hash}]}}
        frag = {"system": {"users": [{"username": "root", "password": ""}]}}
        form = {"users": [{"username": "root", "password": ""}]}
        hash_helper(frag, form, prior_fragment=prior)
        assert frag["system"]["users"][0]["password"] == prior_hash
        assert form["users"][0]["password"] == prior_hash

    def test_empty_password_without_prior_drops_field(self, hash_helper):
        # No prior hash for this username + operator left it blank → drop
        # the password key entirely. The device treats a missing field as
        # "user has no set password" which matches what we'd want anyway.
        frag = {"system": {"users": [{"username": "newuser", "password": ""}]}}
        hash_helper(frag, None, prior_fragment=None)
        assert "password" not in frag["system"]["users"][0]

    def test_plaintext_overrides_prior_hash(self, hash_helper):
        # Operator typed a new password for a user who already had one.
        # Hash the new value; the prior is replaced.
        prior_hash = "$1$old00000$zzzzzzzzzzzzzzzzzzzzzz"
        prior = {"system": {"users": [{"username": "root", "password": prior_hash}]}}
        frag = {"system": {"users": [{"username": "root", "password": "newvalue"}]}}
        hash_helper(frag, None, prior_fragment=prior)
        new_pw = frag["system"]["users"][0]["password"]
        assert new_pw.startswith("$1$")
        assert new_pw != prior_hash

    def test_user_list_can_grow_via_prior_lookup(self, hash_helper):
        # Mixed update: existing user (empty → preserved), new user
        # (plaintext → hashed). Demonstrates per-user resolution.
        prior_hash = "$1$rrrrrrrr$zzzzzzzzzzzzzzzzzzzzzz"
        prior = {"system": {"users": [{"username": "root", "password": prior_hash}]}}
        frag = {"system": {"users": [
            {"username": "root", "password": ""},
            {"username": "admin", "password": "freshpass"},
        ]}}
        hash_helper(frag, None, prior_fragment=prior)
        users = frag["system"]["users"]
        assert users[0]["password"] == prior_hash
        assert users[1]["password"].startswith("$1$") and users[1]["password"] != "freshpass"

    def test_handles_missing_sections(self, hash_helper):
        # Defensive: empty/None inputs shouldn't crash. The Users category
        # template editor can technically POST a fragment that lacks
        # `system.users`; just no-op rather than throw.
        hash_helper({}, {}, prior_fragment=None)
        hash_helper({"system": {}}, None, prior_fragment=None)
        hash_helper(None, None, prior_fragment=None)

    def test_non_string_password_skipped(self, hash_helper):
        # Defensive: malformed payload shouldn't raise. Drop the bogus
        # value via the empty-password branch.
        frag = {"system": {"users": [{"username": "root", "password": None}]}}
        hash_helper(frag, None, prior_fragment=None)
        assert "password" not in frag["system"]["users"][0]


class TestUsersTemplateAPIHashing:
    """End-to-end: POST/PUT /api/config-templates with category='users'
    persists hashes in the DB rather than the operator-entered plaintext.
    GET scrubs the stored hash from the response so the form can't echo
    even the hashed value back to the browser."""

    def test_post_hashes_plaintext_passwords(self, operator_client, mock_db):
        import json as _json
        resp = operator_client.post("/api/config-templates", json={
            "name": "Users Test",
            "category": "users",
            "config_fragment": {"system": {"users": [
                {"username": "root", "password": "topsecret"},
            ]}},
            "form_data": {"users": [{"username": "root", "password": "topsecret"}]},
        })
        assert resp.status_code == 200, resp.text
        # The DB row should have a hashed password, not "topsecret".
        row = mock_db.execute(
            "SELECT config_fragment, form_data FROM config_templates WHERE name = 'Users Test'"
        ).fetchone()
        frag = _json.loads(row["config_fragment"])
        form = _json.loads(row["form_data"])
        assert "topsecret" not in row["config_fragment"]
        assert "topsecret" not in row["form_data"]
        assert frag["system"]["users"][0]["password"].startswith("$1$")
        assert form["users"][0]["password"].startswith("$1$")

    def test_get_scrubs_stored_hash_and_flags_users(self, authed_client, mock_db):
        # Seed a Users template with a pre-hashed password directly so the
        # GET path is what's under test, not the POST hashing.
        import json as _json
        already = "$1$abcdefgh$SbLlAj1nnaSEyBXEfwtQM/"
        frag = {"system": {"users": [{"username": "root", "password": already}]}}
        form = {"users": [{"username": "root", "password": already}]}
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, form_data, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            ("Users Seed", "users", _json.dumps(frag), _json.dumps(form)),
        )
        mock_db.commit()
        resp = authed_client.get("/api/config-templates")
        assert resp.status_code == 200
        seed = next(t for t in resp.json()["templates"] if t["name"] == "Users Seed")
        # Stored hash must not appear anywhere in the response body — neither
        # in `config_fragment.system.users` nor `form_data.users`.
        assert already not in resp.text
        # Frontend gets `has_stored_password=True` so it can render a
        # "(unchanged)" placeholder instead of leaving the field looking empty.
        u_frag = seed["config_fragment"]["system"]["users"][0]
        u_form = seed["form_data"]["users"][0]
        assert u_frag["has_stored_password"] is True
        assert u_form["has_stored_password"] is True
        assert u_frag["password"] == ""
        assert u_form["password"] == ""

    def test_put_with_empty_password_preserves_prior_hash(self, operator_client, mock_db):
        # Re-saving a Users template with an empty password field must not
        # drop the existing credential — empty is the "leave alone" signal.
        import json as _json
        prior_hash = "$1$rrrrrrrr$xxxxxxxxxxxxxxxxxxxxxx"
        cur = mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, form_data, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            ("Users PUT", "users",
             _json.dumps({"system": {"users": [{"username": "root", "password": prior_hash}]}}),
             _json.dumps({"users": [{"username": "root", "password": prior_hash}]})),
        )
        mock_db.commit()
        tid = cur.lastrowid
        # Simulate the form sending back an empty password (frontend sends
        # "" when the operator didn't touch the masked field).
        resp = operator_client.put(f"/api/config-templates/{tid}", json={
            "config_fragment": {"system": {"users": [{"username": "root", "password": ""}]}},
            "form_data": {"users": [{"username": "root", "password": ""}]},
        })
        assert resp.status_code == 200, resp.text
        row = mock_db.execute(
            "SELECT config_fragment, form_data FROM config_templates WHERE id = ?", (tid,)
        ).fetchone()
        frag = _json.loads(row["config_fragment"])
        form = _json.loads(row["form_data"])
        assert frag["system"]["users"][0]["password"] == prior_hash
        assert form["users"][0]["password"] == prior_hash

    def test_put_with_new_plaintext_replaces_prior_hash(self, operator_client, mock_db):
        import json as _json
        prior_hash = "$1$rrrrrrrr$xxxxxxxxxxxxxxxxxxxxxx"
        cur = mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, form_data, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            ("Users Replace", "users",
             _json.dumps({"system": {"users": [{"username": "root", "password": prior_hash}]}}),
             _json.dumps({"users": [{"username": "root", "password": prior_hash}]})),
        )
        mock_db.commit()
        tid = cur.lastrowid
        resp = operator_client.put(f"/api/config-templates/{tid}", json={
            "config_fragment": {"system": {"users": [{"username": "root", "password": "newpass"}]}},
            "form_data": {"users": [{"username": "root", "password": "newpass"}]},
        })
        assert resp.status_code == 200, resp.text
        row = mock_db.execute(
            "SELECT config_fragment FROM config_templates WHERE id = ?", (tid,)
        ).fetchone()
        new_pw = _json.loads(row["config_fragment"])["system"]["users"][0]["password"]
        assert new_pw.startswith("$1$")
        assert new_pw != prior_hash
        assert "newpass" not in row["config_fragment"]


class TestUsersTemplateMigration:
    """The `_migrate_users_template_password_hashing` startup hook hashes
    pre-existing plaintext rows once. Idempotent on already-hashed data."""

    def test_migrates_plaintext_users_row(self, mock_db):
        import json as _json
        from updater.app import _migrate_users_template_password_hashing
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, form_data, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            ("Legacy Users", "users",
             _json.dumps({"system": {"users": [{"username": "root", "password": "leakable"}]}}),
             _json.dumps({"users": [{"username": "root", "password": "leakable"}]})),
        )
        mock_db.commit()
        _migrate_users_template_password_hashing()
        row = mock_db.execute(
            "SELECT config_fragment, form_data FROM config_templates WHERE name = 'Legacy Users'"
        ).fetchone()
        assert "leakable" not in row["config_fragment"]
        assert "leakable" not in row["form_data"]
        assert _json.loads(row["config_fragment"])["system"]["users"][0]["password"].startswith("$1$")

    def test_already_hashed_unchanged(self, mock_db):
        import json as _json
        from updater.app import _migrate_users_template_password_hashing
        already = "$1$abcdefgh$SbLlAj1nnaSEyBXEfwtQM/"
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, form_data, enabled) "
            "VALUES (?, ?, ?, ?, 1)",
            ("Users Hashed", "users",
             _json.dumps({"system": {"users": [{"username": "root", "password": already}]}}),
             _json.dumps({"users": [{"username": "root", "password": already}]})),
        )
        mock_db.commit()
        _migrate_users_template_password_hashing()
        row = mock_db.execute(
            "SELECT config_fragment FROM config_templates WHERE name = 'Users Hashed'"
        ).fetchone()
        assert _json.loads(row["config_fragment"])["system"]["users"][0]["password"] == already

    def test_non_users_categories_untouched(self, mock_db):
        import json as _json
        from updater.app import _migrate_users_template_password_hashing
        # An NTP template happens to have a `password`-named field somewhere
        # — migration must not touch it (only walks the Users category).
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, enabled) "
            "VALUES (?, ?, ?, 1)",
            ("NTP", "ntp",
             _json.dumps({"services": {"ntp": {"servers": ["time.google.com"], "password": "weird"}}})),
        )
        mock_db.commit()
        _migrate_users_template_password_hashing()
        row = mock_db.execute(
            "SELECT config_fragment FROM config_templates WHERE name = 'NTP'"
        ).fetchone()
        # Untouched — both the field and value preserved verbatim.
        assert "weird" in row["config_fragment"]


class TestUserListsCompatible:
    """Compliance check for the Users template tolerates differing
    `$1$<salt>$<hash>` salts on both sides — every push generates a
    fresh salt, so naïve string-equal would flag every fleet as
    non-compliant immediately after a successful push. This is the
    edge-case lane operators kept hitting on dev15."""

    @pytest.fixture
    def matches(self):
        from updater.config_utils import fragment_matches
        return fragment_matches

    def test_different_hashes_match_when_both_are_stored(self, matches):
        # Both sides are 34-char $1$ strings but with different salts —
        # this is the case that was lighting up "non-compliant" forever.
        device = {"system": {"users": [
            {"username": "root", "password": "$1$aabbccdd$AAAAAAAAAAAAAAAAAAAAA0", "level": 0, "enabled": True},
        ]}}
        template = {"system": {"users": [
            {"username": "root", "password": "$1$xxxxyyyy$BBBBBBBBBBBBBBBBBBBBB0", "level": 0, "enabled": True},
        ]}}
        assert matches(device, template) is True

    def test_plaintext_password_on_device_flags_drift(self, matches):
        # Factory defaults + post-reset states return plaintext `password`
        # rather than `$1$` hash. The compliance check must surface those
        # as drift so the manager re-pushes the operator's preferred creds.
        device = {"system": {"users": [
            {"username": "root", "password": "admin", "level": 0, "enabled": True},
        ]}}
        template = {"system": {"users": [
            {"username": "root", "password": "$1$xxxxyyyy$BBBBBBBBBBBBBBBBBBBBB0", "level": 0, "enabled": True},
        ]}}
        assert matches(device, template) is False

    def test_missing_username_on_device_flags_drift(self, matches):
        device = {"system": {"users": [
            {"username": "root", "password": "$1$aabbccdd$AAAAAAAAAAAAAAAAAAAAA0"},
        ]}}
        template = {"system": {"users": [
            {"username": "root", "password": "$1$xxxxyyyy$BBBBBBBBBBBBBBBBBBBBB0"},
            {"username": "admin", "password": "$1$qqqqrrrr$CCCCCCCCCCCCCCCCCCCCC0"},
        ]}}
        assert matches(device, template) is False

    def test_username_order_independent(self, matches):
        # Devices return users in factory-defined order which is rarely
        # the same as the template's order — match by username, not index.
        # Hashes are 34-char `$1$<8-char salt>$<22-char hash>`.
        device = {"system": {"users": [
            {"username": "admin", "password": "$1$11111111$" + "a" * 22},
            {"username": "root",  "password": "$1$22222222$" + "b" * 22},
        ]}}
        template = {"system": {"users": [
            {"username": "root",  "password": "$1$33333333$" + "c" * 22},
            {"username": "admin", "password": "$1$44444444$" + "d" * 22},
        ]}}
        assert matches(device, template) is True

    def test_level_mismatch_still_flagged(self, matches):
        # Password tolerance applies *only* to the password field —
        # level/enabled/etc. still compare exactly. Level=9 (read-only)
        # vs level=0 (admin) is a real drift.
        device = {"system": {"users": [
            {"username": "root", "password": "$1$aabbccdd$AAAAAAAAAAAAAAAAAAAAA0", "level": 9},
        ]}}
        template = {"system": {"users": [
            {"username": "root", "password": "$1$xxxxyyyy$BBBBBBBBBBBBBBBBBBBBB0", "level": 0},
        ]}}
        assert matches(device, template) is False

    def test_other_password_paths_unaffected(self, matches):
        # The Users tolerance is path-scoped to `system.users`. SNMP v3
        # passwords (and any other `password` field) still compare exactly.
        device = {"services": {"snmp": {"v3": {"ro": {"password": "x"}}}}}
        template = {"services": {"snmp": {"v3": {"ro": {"password": "y"}}}}}
        assert matches(device, template) is False


class TestEnforceFailuresAutoClear:
    """Stale failure entries shouldn't pad the dashboard's `failure_count`
    chip after the underlying issue has been resolved by a subsequent
    successful push for the same device."""

    def test_failure_followed_by_success_is_filtered(self, mock_db):
        from updater import database as db
        # Yesterday's failure...
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "failed", error="timeout")
        # ...resolved by a subsequent success on the same IP.
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "success", template_ids=[1])
        failures = db.get_enforce_failures(since_hours=24)
        assert failures == []

    def test_unresolved_failure_still_counted(self, mock_db):
        from updater import database as db
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "failed", error="timeout")
        # No subsequent success row.
        failures = db.get_enforce_failures(since_hours=24)
        assert len(failures) == 1
        assert failures[0]["ip"] == "10.0.0.1"

    def test_per_ip_resolution(self, mock_db):
        from updater import database as db
        # Two failures, only one resolved. The other should still count.
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "failed")
        db.save_config_enforce_log("10.0.0.2", "ap", "canary", "failed")
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "success", template_ids=[1])
        failures = db.get_enforce_failures(since_hours=24)
        assert [f["ip"] for f in failures] == ["10.0.0.2"]

    def test_failure_after_success_is_still_counted(self, mock_db):
        # Old success, then a fresh failure — the failure is *new* drift,
        # don't accidentally suppress it just because a success exists in
        # the log somewhere.
        from updater import database as db
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "success", template_ids=[1])
        db.save_config_enforce_log("10.0.0.1", "ap", "canary", "failed", error="auth")
        failures = db.get_enforce_failures(since_hours=24)
        assert len(failures) == 1
