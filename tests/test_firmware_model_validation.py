"""Fail-closed firmware/model validation (issue #215).

`validate_firmware_for_model` is the flash-time chokepoint (`update_firmware`
calls it before upload). An unmapped model must be REFUSED, never allowed to
flash another platform's image — e.g. a TNA-305 before Platform 3 support lands.
"""

import pytest

from updater.vendors.tachyon.client import TachyonClient

# Representative real filenames (from the field).
FW_30X = "tna-30x-1.15.0-r55142-20260521-tn-110-prs-squashfs-sysupgrade.bin"
FW_303L = "tna-303l-1.15.0-r8503-20260521-sysupgrade.bin"
FW_TNS100 = "tns-1.12.8-r54729-20251121-tns-100-squashfs-sysupgrade.bin"


@pytest.fixture
def client():
    # Constructor opens no connections; validation is pure (filename + model).
    return TachyonClient("203.0.113.1", "user", "pass")


class TestValidMatches:
    @pytest.mark.parametrize("model", ["TNA-301", "TNA-302", "TNA-303X", "tna-301"])
    def test_platform1_models_accept_30x(self, client, model):
        ok, err = client.validate_firmware_for_model(FW_30X, model)
        assert ok, err

    @pytest.mark.parametrize("model", ["TNA-303L", "TNA-303L-65", "tna-303l-65"])
    def test_303l_models_accept_303l(self, client, model):
        # 303L-65 must resolve via the "tna-303l" prefix, not an exact key.
        ok, err = client.validate_firmware_for_model(FW_303L, model)
        assert ok, err

    def test_switch_accepts_tns100(self, client):
        ok, err = client.validate_firmware_for_model(FW_TNS100, "TNS-100")
        assert ok, err


class TestMismatchRejected:
    def test_303l_device_rejects_30x_firmware(self, client):
        ok, err = client.validate_firmware_for_model(FW_30X, "TNA-303L-65")
        assert not ok
        assert "mismatch" in err.lower()

    def test_30x_device_rejects_303l_firmware(self, client):
        ok, err = client.validate_firmware_for_model(FW_303L, "TNA-301")
        assert not ok
        assert "mismatch" in err.lower()


class TestFailClosed:
    """The core of #215: unmapped models are refused, not allowed through."""

    @pytest.mark.parametrize("model", ["TNA-305X", "TNA-305A", "tna-305x"])
    def test_tna305_refused_even_with_plausible_file(self, client, model):
        # Before the fix this returned (True, "") and would flash 30x firmware.
        ok, err = client.validate_firmware_for_model(FW_30X, model)
        assert not ok
        assert "unsupported model" in err.lower()

    @pytest.mark.parametrize("model", ["", "totally-unknown", "TNA-999Z"])
    def test_unknown_or_empty_model_refused(self, client, model):
        ok, err = client.validate_firmware_for_model(FW_30X, model)
        assert not ok
        assert "unsupported model" in err.lower()


class TestPatternsHelper:
    def test_known_models_resolve(self, client):
        assert client._patterns_for_model("tna-301") is not None
        assert client._patterns_for_model("TNA-303L-65") is not None  # case + prefix
        assert client._patterns_for_model("tns-100") is not None

    def test_unmapped_models_return_none(self, client):
        assert client._patterns_for_model("tna-305x") is None
        assert client._patterns_for_model("") is None
        assert client._patterns_for_model(None) is None


class TestSelectFailClosed:
    """select_firmware_for_model / get_firmware_type_for_model fail closed for
    unmapped models, so fleet-status/planning don't mark an unsupported model as
    eligible and queue a doomed job (#215 review)."""

    FW = {"tna-30x": "/fw/tna-30x.bin", "tna-303l": "/fw/tna-303l.bin", "tns-100": "/fw/tns.bin"}

    @pytest.fixture
    def driver(self):
        from updater.vendors.tachyon import TachyonDriver
        return TachyonDriver("203.0.113.1", "user", "pass")

    def test_known_model_selects_its_family(self, driver):
        assert driver.select_firmware_for_model("TNA-301", self.FW) == "/fw/tna-30x.bin"
        assert driver.select_firmware_for_model("TNA-303L-65", self.FW) == "/fw/tna-303l.bin"
        assert driver.select_firmware_for_model("TNS-100", self.FW) == "/fw/tns.bin"

    def test_known_model_type(self, driver):
        assert driver.get_firmware_type_for_model("TNA-301") == "tna-30x"
        assert driver.get_firmware_type_for_model("TNA-303L-65") == "tna-303l"

    def test_unmapped_model_selects_nothing(self, driver):
        # Was: defaulted to 30x firmware (a wrong-platform target). Now: None.
        assert driver.select_firmware_for_model("TNA-305X", self.FW) is None
        assert driver.select_firmware_for_model("bogus", self.FW) is None
        assert driver.select_firmware_for_model("", self.FW) is None

    def test_unmapped_model_has_no_type(self, driver):
        assert driver.get_firmware_type_for_model("TNA-305X") is None
        assert driver.get_firmware_type_for_model("") is None
