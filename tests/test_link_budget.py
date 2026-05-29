"""Tests for updater.link_budget — pure-math survival calculations."""

import pytest

from updater import link_budget


class TestBandwidthBucket:
    def test_full_bw(self):
        assert link_budget.bandwidth_bucket(2160) == "full"
        assert link_budget.bandwidth_bucket(2000) == "full"
        assert link_budget.bandwidth_bucket(1500) == "full"

    def test_half_bw(self):
        assert link_budget.bandwidth_bucket(1080) == "half"
        assert link_budget.bandwidth_bucket(1000) == "half"

    def test_missing_or_invalid(self):
        assert link_budget.bandwidth_bucket(None) is None
        assert link_budget.bandwidth_bucket(0) is None
        assert link_budget.bandwidth_bucket(-1) is None


class TestMcs1Floor:
    def test_full_bw(self):
        assert link_budget.mcs1_floor_dbm("full") == -70

    def test_half_bw(self):
        assert link_budget.mcs1_floor_dbm("half") == -76

    def test_unknown(self):
        assert link_budget.mcs1_floor_dbm("bogus") is None


class TestMaxSurvivableRain:
    def test_typical_link_returns_positive(self):
        # 60 GHz link at 250 m with comfortable -55 dBm: should survive a
        # meaningful storm before dropping below MCS1.
        mm = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=250, channel=3, channel_width_mhz=2160,
        )
        assert mm is not None and mm > 100  # tons of headroom

    def test_marginal_link_drops_at_light_rain(self):
        # Far link with barely-any margin should be in the single-digit
        # mm/hr survivability band (red in any climate zone).
        mm = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-68, distance_m=900, channel=3, channel_width_mhz=2160,
        )
        assert mm is not None and 0 < mm < 12  # below zone-K's 99.9 %

    def test_signal_below_floor_returns_zero(self):
        # MCS1 floor is -70 on full BW; below that the link is already
        # down, so survives "0 mm/hr" of rain.
        mm = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-72, distance_m=500, channel=3, channel_width_mhz=2160,
        )
        assert mm == 0.0

    def test_missing_rssi_returns_none(self):
        assert link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=None, distance_m=500, channel=3, channel_width_mhz=2160,
        ) is None

    def test_missing_distance_returns_none(self):
        assert link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=None, channel=3, channel_width_mhz=2160,
        ) is None

    def test_zero_distance_returns_none(self):
        assert link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=0, channel=3, channel_width_mhz=2160,
        ) is None

    def test_missing_channel_returns_none(self):
        assert link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=500, channel=None, channel_width_mhz=2160,
        ) is None

    def test_missing_bw_returns_none(self):
        assert link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=500, channel=3, channel_width_mhz=None,
        ) is None

    @pytest.mark.parametrize("channel", [1, 2, 3, 4, 5, 6])
    def test_all_full_bw_channels_resolve(self, channel):
        # Sanity: every full-BW channel has rain coefficients.
        mm = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=500, channel=channel, channel_width_mhz=2160,
        )
        assert mm is not None and mm > 0

    def test_shorter_link_survives_more_rain(self):
        # Halving the distance roughly doubles fade margin per km of rain.
        near = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=200, channel=3, channel_width_mhz=2160,
        )
        far = link_budget.max_survivable_rain_mm_hr(
            rssi_dbm=-55, distance_m=800, channel=3, channel_width_mhz=2160,
        )
        assert near > far
