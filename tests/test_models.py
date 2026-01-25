"""Tests for updater.models."""

from updater.models import SignalHealth, CPEInfo, APWithCPEs, NetworkTopology


class TestSignalHealth:
    def test_strong_signal(self):
        assert SignalHealth.from_signal(-64.9) == SignalHealth.GREEN

    def test_boundary_green_yellow(self):
        assert SignalHealth.from_signal(-65.0) == SignalHealth.YELLOW

    def test_acceptable_signal(self):
        assert SignalHealth.from_signal(-70.0) == SignalHealth.YELLOW

    def test_boundary_yellow_red(self):
        assert SignalHealth.from_signal(-75.0) == SignalHealth.YELLOW

    def test_weak_signal(self):
        assert SignalHealth.from_signal(-75.1) == SignalHealth.RED

    def test_none_signal(self):
        assert SignalHealth.from_signal(None) == SignalHealth.RED


class TestCPEInfo:
    def test_signal_health_combined(self):
        cpe = CPEInfo(ip="1.2.3.4", combined_signal=-60.0)
        assert cpe.signal_health == SignalHealth.GREEN

    def test_signal_health_rx_power_fallback(self):
        cpe = CPEInfo(ip="1.2.3.4", rx_power=-70.0)
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


class TestAPWithCPEs:
    def test_health_summary(self):
        cpes = [
            CPEInfo(ip="1.1.1.1", combined_signal=-50.0),  # green
            CPEInfo(ip="1.1.1.2", combined_signal=-70.0),  # yellow
            CPEInfo(ip="1.1.1.3", combined_signal=-80.0),  # red
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
            CPEInfo(ip="1.1.1.1", combined_signal=-50.0),
        ])
        ap2 = APWithCPEs(ip="10.0.0.2", cpes=[
            CPEInfo(ip="2.2.2.1", combined_signal=-70.0),
            CPEInfo(ip="2.2.2.2", combined_signal=-80.0),
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
