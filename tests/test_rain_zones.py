"""Tests for updater.rain_zones — ITU-R P.837 rain climate tables."""

from updater import rain_zones


class TestCountryToZone:
    def test_us_is_k(self):
        assert rain_zones.zone_for_country("US") == "K"

    def test_uk_is_e(self):
        assert rain_zones.zone_for_country("GB") == "E"

    def test_india_is_n(self):
        assert rain_zones.zone_for_country("IN") == "N"

    def test_singapore_is_tropical(self):
        assert rain_zones.zone_for_country("SG") == "P"

    def test_lowercase_country_normalised(self):
        assert rain_zones.zone_for_country("us") == "K"

    def test_unknown_country_defaults_to_k(self):
        assert rain_zones.zone_for_country("ZZ") == "K"

    def test_none_country_defaults_to_k(self):
        assert rain_zones.zone_for_country(None) == "K"


class TestRatesForZone:
    def test_zone_k_published_values(self):
        # ITU-R P.837-7 zone K reference values.
        rates = rain_zones.rates_for_zone("K")
        assert rates["0.01%"] == 42       # 99.99 % storm
        assert rates["0.1%"] == 12        # 99.9 % storm
        assert rates["1%"] == 1.5

    def test_zone_p_tropical(self):
        rates = rain_zones.rates_for_zone("P")
        assert rates["0.01%"] == 145      # heavier than K
        assert rates["0.1%"] == 65

    def test_unknown_zone_falls_back_to_default(self):
        rates = rain_zones.rates_for_zone("BOGUS")
        assert rates["0.01%"] == 42       # fell back to K

    def test_all_zones_have_complete_rate_table(self):
        # Every zone exposes all 7 exceedance buckets.
        labels = set(rain_zones.EXCEEDANCE_LABELS)
        for zone, rates in rain_zones.RAIN_RATES_BY_ZONE.items():
            assert set(rates.keys()) == labels, f"Zone {zone} missing buckets"
            # Rain rates increase as exceedance percentage drops.
            ordered = [rates[lbl] for lbl in rain_zones.EXCEEDANCE_LABELS]
            assert ordered == sorted(ordered), f"Zone {zone} rates not monotonic"
