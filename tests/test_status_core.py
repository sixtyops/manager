"""Characterization tests for the six firmware decision functions.

These pin the EXACT current behavior of every "is this device on target / does
it need an update" function across the boundary matrix, BEFORE the consolidation
refactor. They must pass against today's code, then remain unchanged after the
functions become thin shims over the new `version_utils` core — proving the
refactor preserves behavior.

The functions deliberately disagree (parsed vs exact-string compare, bank mode,
cooldown, missing-target polarity). That divergence is load-bearing and is
documented case-by-case here.
"""

from datetime import datetime, timedelta

from updater.app import (
    _device_version_status,
    _behind_direction,
    _device_needs_update,
    _device_needs_update_for_bank_mode,
    _device_on_exact_build,
)
from updater.scheduler import _device_needs_update as sched_needs_update

# Version fixtures (target T; EQ_DOT equal dotted; EQ_R equal but device-reported
# ".r" form; OLD older; NEW strictly newer).
T = "1.15.0.8515"
EQ_DOT = "1.15.0.8515"
EQ_R = "1.15.0.r8515"
OLD = "1.12.4.7782"
NEW = "1.16.0.9000"


def dev(fw="", b1=None, b2=None, active_bank=None):
    d = {"firmware_version": fw}
    if b1 is not None:
        d["bank1_version"] = b1
    if b2 is not None:
        d["bank2_version"] = b2
    if active_bank is not None:
        d["active_bank"] = active_bank
    return d


class TestDeviceVersionStatus:
    """#1 — parsed compare, no bank mode. Display/eligibility."""

    def test_unknown_on_missing(self):
        assert _device_version_status("", T) == "unknown"
        assert _device_version_status(T, "") == "unknown"

    def test_equal_forms_are_current(self):
        assert _device_version_status(EQ_DOT, T) == "current"
        assert _device_version_status(EQ_R, T) == "current"  # parsed: .r == .

    def test_older_is_behind(self):
        assert _device_version_status(OLD, T) == "behind"

    def test_newer_depends_on_downgrade(self):
        assert _device_version_status(NEW, T) == "current"
        assert _device_version_status(NEW, T, allow_downgrade=True) == "behind"

    def test_unparseable_current_reads_behind(self):
        # "__unknown__" parses to (0,), which is < any real target.
        assert _device_version_status("__unknown__", T) == "behind"


class TestBehindDirection:
    """#2 — direction label; only meaningful when behind, but defined for all."""

    def test_older_is_upgrade(self):
        assert _behind_direction(OLD, T) == "upgrade"

    def test_newer_is_downgrade(self):
        assert _behind_direction(NEW, T) == "downgrade"

    def test_equal_is_upgrade(self):
        assert _behind_direction(EQ_DOT, T) == "upgrade"

    def test_none_on_missing(self):
        assert _behind_direction("", T) is None
        assert _behind_direction(T, "") is None


class TestAppNeedsUpdate:
    """#3 — bank-aware, PARSED compare via #1; missing target -> push."""

    def test_missing_target_pushes(self):
        assert _device_needs_update(dev(fw=OLD), "", "one", False) is True

    def test_missing_current_pushes(self):
        assert _device_needs_update(dev(fw=""), T, "one", False) is True

    def test_behind_needs_update(self):
        assert _device_needs_update(dev(fw=OLD), T, "one", False) is True

    def test_current_one_bank_skips(self):
        assert _device_needs_update(dev(fw=EQ_DOT), T, "one", False) is False

    def test_newer_one_bank(self):
        assert _device_needs_update(dev(fw=NEW), T, "one", False) is False
        assert _device_needs_update(dev(fw=NEW), T, "one", True) is True

    def test_both_inactive_behind_needs_update(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=OLD, active_bank=1)
        assert _device_needs_update(d, T, "both", False) is True

    def test_both_both_banks_current_skips(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=EQ_DOT, active_bank=1)
        assert _device_needs_update(d, T, "both", False) is False

    def test_both_no_bank_info_skips(self):
        d = dev(fw=EQ_DOT, b1="", b2="", active_bank=1)
        assert _device_needs_update(d, T, "both", False) is False

    def test_both_active_current_inactive_blank_but_bank1_set_pushes(self):
        # Quirk: active on target, inactive unknown but a bank string exists -> push.
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2="", active_bank=1)
        assert _device_needs_update(d, T, "both", False) is True


class TestNeedsUpdateForBankMode:
    """#4 — EXACT-STRING (after .r normalization) + bank mode. Skip gate."""

    def test_missing_target_pushes(self):
        assert _device_needs_update_for_bank_mode(dev(fw=OLD), "", "one") is True

    def test_unknown_active_pushes(self):
        assert _device_needs_update_for_bank_mode(dev(fw=""), T, "one") is True

    def test_equal_forms_skip(self):
        assert _device_needs_update_for_bank_mode(dev(fw=EQ_DOT), T, "one") is False
        assert _device_needs_update_for_bank_mode(dev(fw=EQ_R), T, "one") is False

    def test_older_pushes(self):
        assert _device_needs_update_for_bank_mode(dev(fw=OLD), T, "one") is True

    def test_newer_skips_without_downgrade(self):
        assert _device_needs_update_for_bank_mode(dev(fw=NEW), T, "one", False) is False
        assert _device_needs_update_for_bank_mode(dev(fw=NEW), T, "one", True) is True

    def test_both_inactive_older_pushes(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=OLD, active_bank=1)
        assert _device_needs_update_for_bank_mode(d, T, "both", False) is True

    def test_both_both_current_skips(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=EQ_DOT, active_bank=1)
        assert _device_needs_update_for_bank_mode(d, T, "both", False) is False

    def test_both_inactive_newer_skips_without_downgrade(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=NEW, active_bank=1)
        assert _device_needs_update_for_bank_mode(d, T, "both", False) is False


class TestOnExactBuild:
    """#5 — EXACT-STRING, bank-aware, ignores downgrade (always push unless provable)."""

    def test_missing_target_not_provable(self):
        assert _device_on_exact_build(dev(fw=EQ_DOT), "", "one") is False

    def test_equal_forms_are_provable(self):
        assert _device_on_exact_build(dev(fw=EQ_DOT), T, "one") is True
        assert _device_on_exact_build(dev(fw=EQ_R), T, "one") is True

    def test_older_not_provable(self):
        assert _device_on_exact_build(dev(fw=OLD), T, "one") is False

    def test_newer_not_provable(self):
        assert _device_on_exact_build(dev(fw=NEW), T, "one") is False

    def test_both_both_banks_match(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=EQ_DOT, active_bank=1)
        assert _device_on_exact_build(d, T, "both") is True

    def test_both_inactive_differs_not_provable(self):
        d = dev(fw=EQ_DOT, b1=EQ_DOT, b2=OLD, active_bank=1)
        assert _device_on_exact_build(d, T, "both") is False


class TestSchedulerNeedsUpdate:
    """#6 — HYBRID: exact-string equality gate + parsed newer-guard, cooldown,
    __unknown__, missing-target -> don't enroll."""

    def test_missing_target_does_not_enroll(self):
        assert sched_needs_update(OLD, "", False) is False

    def test_unknown_marker_enrolls(self):
        assert sched_needs_update(OLD, "__unknown__", False) is True

    def test_exact_equal_skips(self):
        assert sched_needs_update(EQ_DOT, T, False) is False

    def test_reformatted_equal_enrolls(self):
        # The hybrid wart: ".r" form is NOT exact-equal to dotted target, so the
        # equality skip misses; parsed newer-guard then sees equal (not newer) ->
        # enroll. Must be preserved.
        assert sched_needs_update(EQ_R, T, False) is True

    def test_older_enrolls(self):
        assert sched_needs_update(OLD, T, False) is True

    def test_newer_blocked_without_downgrade(self):
        assert sched_needs_update(NEW, T, False) is False
        assert sched_needs_update(NEW, T, True) is True

    def test_cooldown_within_skips(self):
        recent = (datetime.now() - timedelta(days=1)).isoformat()
        assert sched_needs_update(OLD, T, False, last_update_iso=recent, cooldown_days=30) is False

    def test_cooldown_expired_enrolls(self):
        old = (datetime.now() - timedelta(days=60)).isoformat()
        assert sched_needs_update(OLD, T, False, last_update_iso=old, cooldown_days=30) is True
