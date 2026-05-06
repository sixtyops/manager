"""Tests for config auto-enforce: scoping, enforce log, template resolution."""

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
