"""Tests for vendor driver wrappers surfacing inner-client telemetry.

Regression coverage for a bug where the Tachyon driver wrapped a TachyonClient
but didn't expose `last_radio_params`, so `poll_ap`'s
`getattr(client, "last_radio_params", None)` always saw None — nulling every
CPE's computed rain tolerance and the Rain-Fade chart.
"""

from updater.vendors.tachyon import TachyonDriver


class TestTachyonDriverRadioParams:
    def test_fresh_driver_has_no_radio_params(self):
        driver = TachyonDriver("10.0.0.1", "u", "p")
        assert driver.last_radio_params is None

    def test_driver_surfaces_inner_client_radio_params(self):
        driver = TachyonDriver("10.0.0.1", "u", "p")
        radios = {
            "channel": 6,
            "channel_width_mhz": 2160,
            "frequency_mhz": 69120,
            "antenna_kit": "none",
            "noise_dbm": 0,
        }
        # Simulate get_connected_cpes() having captured the AP's radios.
        driver._client.last_radio_params = radios
        # The poller reads it via getattr on the driver — it must see the
        # inner client's value, not None.
        assert driver.last_radio_params == radios
        assert getattr(driver, "last_radio_params", None) == radios


class TestTachyonRebootTimeout:
    """AP/CPE reboot wait must cover the post-reboot 60GHz modem flash, which
    can push recovery past the old 300s ceiling (a device seen back online at
    ~314s was being marked "did not come back online"). See issue #217."""

    def test_ap_and_cpe_window_covers_60ghz_flash(self):
        driver = TachyonDriver("10.0.0.1", "u", "p")
        assert driver.get_reboot_timeout("ap") >= 600
        assert driver.get_reboot_timeout("cpe") >= 600

    def test_switch_window_unchanged(self):
        driver = TachyonDriver("10.0.0.1", "u", "p")
        assert driver.get_reboot_timeout("switch") == 600
