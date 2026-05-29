"""Tests for updater.models."""

from updater.models import SignalHealth, CPEInfo, APWithCPEs, NetworkTopology


class TestSignalHealth:
    """Boundaries: Strong >= -60, Low -65 to -61, Marginal < -65."""

    def test_strong_signal(self):
        assert SignalHealth.from_signal(-50.0) == SignalHealth.GREEN

    def test_boundary_green_yellow(self):
        # -60 is the inclusive top of Strong.
        assert SignalHealth.from_signal(-60.0) == SignalHealth.GREEN
        # -60.1 falls into Low.
        assert SignalHealth.from_signal(-60.1) == SignalHealth.YELLOW

    def test_low_signal(self):
        assert SignalHealth.from_signal(-63.0) == SignalHealth.YELLOW

    def test_boundary_yellow_red(self):
        # -65 is the inclusive bottom of Low.
        assert SignalHealth.from_signal(-65.0) == SignalHealth.YELLOW
        # -65.1 falls into Marginal.
        assert SignalHealth.from_signal(-65.1) == SignalHealth.RED

    def test_marginal_signal(self):
        assert SignalHealth.from_signal(-80.0) == SignalHealth.RED

    def test_none_signal(self):
        assert SignalHealth.from_signal(None) == SignalHealth.RED


class TestCPEInfo:
    def test_signal_health_combined(self):
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-58.0)
        assert cpe.signal_health == SignalHealth.GREEN

    def test_signal_health_rx_power_fallback(self):
        cpe = CPEInfo(ip="1.2.3.4", rx_power=-63.0)
        assert cpe.signal_health == SignalHealth.YELLOW

    def test_signal_health_none(self):
        cpe = CPEInfo(ip="1.2.3.4")
        assert cpe.signal_health == SignalHealth.RED

    def test_primary_signal_combined(self):
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-60.0, rx_power=-65.0)
        assert cpe.primary_signal == -60.0

    def test_primary_signal_rx_power(self):
        cpe = CPEInfo(ip="1.2.3.4", rx_power=-65.0)
        assert cpe.primary_signal == -65.0

    def test_primary_signal_rssi_fallback(self):
        cpe = CPEInfo(ip="1.2.3.4", last_local_rssi=-70.0)
        assert cpe.primary_signal == -70.0

    def test_primary_signal_none(self):
        cpe = CPEInfo(ip="1.2.3.4")
        assert cpe.primary_signal is None

    def test_to_dict(self):
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-60.0)
        d = cpe.to_dict()
        assert d["signal_health"] == "green"
        assert d["primary_signal"] == -60.0
        assert d["ip"] == "1.2.3.4"


class TestSignalHealthFromMaxRain:
    """When max_rain_mm_hr is set, signal_health buckets against the
    detected climate's 99.99 %/99.9 % rates instead of bare dBm."""

    def _force_zone(self, monkeypatch, zone):
        # Override the cached climate so we don't depend on real geolocation.
        from updater import services, rain_zones
        monkeypatch.setattr(
            services,
            "get_default_rain_climate_cached",
            lambda: {
                "zone": zone,
                "country": "TEST",
                "rates_mm_hr": rain_zones.rates_for_zone(zone),
            },
        )

    def test_max_rain_above_99_99_is_green(self, monkeypatch):
        self._force_zone(monkeypatch, "K")  # 99.99 % = 42 mm/hr
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-70.0, max_rain_mm_hr=80.0)
        assert cpe.signal_health == SignalHealth.GREEN

    def test_max_rain_between_99_9_and_99_99_is_yellow(self, monkeypatch):
        self._force_zone(monkeypatch, "K")  # 99.9% = 12, 99.99% = 42
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-70.0, max_rain_mm_hr=25.0)
        assert cpe.signal_health == SignalHealth.YELLOW

    def test_max_rain_below_99_9_is_red(self, monkeypatch):
        self._force_zone(monkeypatch, "K")
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-70.0, max_rain_mm_hr=3.0)
        assert cpe.signal_health == SignalHealth.RED

    def test_max_rain_zero_is_red(self, monkeypatch):
        self._force_zone(monkeypatch, "K")
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-70.0, max_rain_mm_hr=0.0)
        assert cpe.signal_health == SignalHealth.RED

    def test_no_max_rain_falls_back_to_legacy_thresholds(self):
        # Regression guard: nothing about the legacy bare-dBm bucketing
        # changes when max_rain_mm_hr isn't set. Other tests in this module
        # already exercise the boundaries; this just spot-checks the path.
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-62.0)
        assert cpe.max_rain_mm_hr is None
        assert cpe.signal_health == SignalHealth.YELLOW


class TestAPWithCPEs:
    def test_health_summary(self):
        cpes = [
            CPEInfo(ip="1.1.1.1", combined_signal=-50.0),  # green (>= -60)
            CPEInfo(ip="1.1.1.2", combined_signal=-63.0),  # yellow (-65..-61)
            CPEInfo(ip="1.1.1.3", combined_signal=-80.0),  # red (< -65)
            CPEInfo(ip="1.1.1.4", combined_signal=-80.0),  # red
        ]
        ap = APWithCPEs(ip="10.0.0.1", cpes=cpes)
        assert ap.health_summary == {"green": 1, "yellow": 1, "red": 2}
        assert ap.cpe_count == 4

    def test_empty(self):
        ap = APWithCPEs(ip="10.0.0.1")
        assert ap.health_summary == {"green": 0, "yellow": 0, "red": 0}
        assert ap.cpe_count == 0


class TestNetworkTopology:
    def test_totals(self):
        ap1 = APWithCPEs(ip="10.0.0.1", cpes=[
            CPEInfo(ip="1.1.1.1", combined_signal=-50.0),  # green
        ])
        ap2 = APWithCPEs(ip="10.0.0.2", cpes=[
            CPEInfo(ip="2.2.2.1", combined_signal=-63.0),  # yellow
            CPEInfo(ip="2.2.2.2", combined_signal=-80.0),  # red
        ])
        topo = NetworkTopology(aps=[ap1, ap2])
        assert topo.total_aps == 2
        assert topo.total_cpes == 3
        assert topo.overall_health == {"green": 1, "yellow": 1, "red": 1}

    def test_to_dict(self):
        topo = NetworkTopology(aps=[APWithCPEs(ip="10.0.0.1")])
        d = topo.to_dict()
        assert d["total_aps"] == 1
        assert d["total_cpes"] == 0
        assert "aps" in d
