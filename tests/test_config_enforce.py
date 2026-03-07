"""Tests for config auto-enforce: scoping, enforce log, template resolution."""

import json
import math
from contextlib import contextmanager
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from updater import database as db
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
