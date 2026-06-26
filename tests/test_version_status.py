"""Version comparison, device status, and downgrade-direction logic.

Covers the live bug where a TNA-303L on the newest beta read as "behind" (a
downgrade candidate) because the selected target had silently reverted to an
older stable build, and the UI then mislabeled the ahead-of-target device as
needing an update.
"""

from updater import version_utils as vu
from updater.app import _device_version_status, _behind_direction


class TestVersionUtils:
    def test_extract_handles_bare_tns_switch_revision(self):
        # The vendor copy dropped the switch revision; the canonical one keeps it.
        assert vu.extract_version_from_filename(
            "tns-1.12.8-r54729-20251121-tns-100-squashfs-sysupgrade.bin"
        ) == "1.12.8.54729"

    def test_extract_ap(self):
        assert vu.extract_version_from_filename(
            "tna-303l-1.15.0-r8515-20260609-sysupgrade.bin"
        ) == "1.15.0.8515"

    def test_compare_device_reported_equals_filename_build(self):
        # Device reports "1.15.0.r8515"; target filename extracts "1.15.0.8515".
        assert vu.compare_versions("1.15.0.r8515", "1.15.0.8515") == 0

    def test_compare_orders_by_build_number(self):
        assert vu.compare_versions("1.15.0.8515", "1.15.0.8503") > 0
        assert vu.compare_versions("1.12.4.7782", "1.15.0.8515") < 0

    def test_app_aliases_are_the_shared_functions(self):
        # The de-duplication must not silently diverge.
        import updater.app as app
        assert app._extract_version_from_filename is vu.extract_version_from_filename
        assert app._compare_versions is vu.compare_versions
        assert app._parse_version is vu.parse_version


class TestDeviceVersionStatus:
    def test_equal_is_current(self):
        assert _device_version_status("1.15.0.r8515", "1.15.0.8515") == "current"

    def test_behind_is_behind(self):
        assert _device_version_status("1.12.4.7782", "1.15.0.8515") == "behind"

    def test_ahead_is_current_without_downgrade(self):
        # Newer-than-target stays "current" unless downgrades are allowed.
        assert _device_version_status("1.15.0.8515", "1.12.4.7782") == "current"

    def test_ahead_is_behind_with_downgrade(self):
        assert _device_version_status(
            "1.15.0.8515", "1.12.4.7782", allow_downgrade=True
        ) == "behind"

    def test_unknown_when_missing(self):
        assert _device_version_status("", "1.15.0.8515") == "unknown"
        assert _device_version_status("1.15.0.8515", "") == "unknown"


class TestBehindDirection:
    def test_downgrade_when_device_newer(self):
        # The exact live case: device on 8515, target reverted to 7782 stable.
        assert _behind_direction("1.15.0.r8515", "1.12.4.7782") == "downgrade"

    def test_upgrade_when_device_older(self):
        assert _behind_direction("1.12.4.7782", "1.15.0.8515") == "upgrade"

    def test_none_when_unknown(self):
        assert _behind_direction("", "1.15.0.8515") is None
        assert _behind_direction("1.15.0.8515", "") is None
