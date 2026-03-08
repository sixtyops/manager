"""Tests for post-update smoke tests."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from updater.tachyon import TachyonClient, SmokeTestResult, DeviceInfo


# ── Unit tests for TachyonClient.run_smoke_tests() ──


@pytest.mark.asyncio
async def test_smoke_tests_all_pass_ap():
    """AP smoke tests pass when device responds, config readable, CPEs present."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"

    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={"wireless": {}})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -65
    mock_cpe.last_local_rssi = -65
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True
    assert len(result.warnings) == 0
    assert len(result.checks) >= 3  # responsive, config, cpe_connectivity


@pytest.mark.asyncio
async def test_smoke_tests_all_pass_cpe():
    """CPE smoke tests pass (no CPE connectivity check)."""
    client = TachyonClient("10.0.0.100", "admin", "pass")
    client._token = "fake"

    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.100", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={"system": {}})
    client.get_connected_cpes = AsyncMock()

    result = await client.run_smoke_tests(role="cpe")
    assert result.passed is True
    assert len(result.warnings) == 0
    client.get_connected_cpes.assert_not_called()


@pytest.mark.asyncio
async def test_smoke_tests_switch_skips_cpe_check():
    """Switch role skips CPE connectivity check."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"

    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})
    client.get_connected_cpes = AsyncMock()

    result = await client.run_smoke_tests(role="switch")
    assert result.passed is True
    client.get_connected_cpes.assert_not_called()


@pytest.mark.asyncio
async def test_smoke_tests_cpe_count_dropped_to_zero():
    """Warning when all CPEs lost after update."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})
    client.get_connected_cpes = AsyncMock(return_value=[])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=5)
    assert result.passed is False
    assert any("5" in w and "0" in w for w in result.warnings)
    cpe_check = next(c for c in result.checks if c["check"] == "cpe_connectivity")
    assert cpe_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_cpe_count_decreased():
    """Warning when some CPEs lost after update."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -65
    mock_cpe.last_local_rssi = -65
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=3)
    assert result.passed is False
    assert any("3" in w and "1" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_smoke_tests_low_signal():
    """Warning when CPE signal is below -80 dBm threshold."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -85
    mock_cpe.last_local_rssi = -85
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is False
    assert any("signal" in w.lower() for w in result.warnings)
    signal_check = next(c for c in result.checks if c["check"] == "cpe_signal_levels")
    assert signal_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_device_unresponsive():
    """Warning when device status check fails."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(side_effect=Exception("Connection timeout"))
    client.get_config = AsyncMock(return_value=None)

    result = await client.run_smoke_tests(role="cpe")
    assert result.passed is False
    assert any("status check failed" in w.lower() for w in result.warnings)
    responsive_check = next(c for c in result.checks if c["check"] == "device_responsive")
    assert responsive_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_config_unreadable():
    """Warning when config returns None."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value=None)

    result = await client.run_smoke_tests(role="cpe")
    assert result.passed is False
    assert any("config" in w.lower() for w in result.warnings)
    config_check = next(c for c in result.checks if c["check"] == "config_intact")
    assert config_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_config_exception():
    """Config exception doesn't prevent other checks from running."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(side_effect=Exception("Config error"))
    client.get_connected_cpes = AsyncMock(return_value=[])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=0)
    # Config failed but device_responsive and cpe_connectivity checks still ran
    check_names = [c["check"] for c in result.checks]
    assert "device_responsive" in check_names
    assert "config_intact" in check_names
    assert "cpe_connectivity" in check_names


@pytest.mark.asyncio
async def test_smoke_tests_no_version_info():
    """Warning when device responds but returns no version."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version=None))
    client.get_config = AsyncMock(return_value={})

    result = await client.run_smoke_tests(role="switch")
    assert result.passed is False
    assert any("valid status" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_smoke_tests_ap_no_pre_update_cpes():
    """AP with no known pre-update CPEs still runs check without warning."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})
    client.get_connected_cpes = AsyncMock(return_value=[])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=0)
    assert result.passed is True  # No warning because pre_update count was 0


@pytest.mark.asyncio
async def test_smoke_tests_cpe_connectivity_exception():
    """CPE connectivity exception produces warning, not crash."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})
    client.get_connected_cpes = AsyncMock(side_effect=Exception("Network error"))

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=3)
    assert result.passed is False
    assert any("connectivity check failed" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_smoke_tests_signal_none_values():
    """CPEs with None signal values are silently skipped."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = None
    mock_cpe.last_local_rssi = None
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True  # No signal warning when values are None
    # Signal check shows OK since no bad signals detected
    signal_checks = [c for c in result.checks if c["check"] == "cpe_signal_levels"]
    assert len(signal_checks) == 1
    assert signal_checks[0]["passed"] is True


@pytest.mark.asyncio
async def test_smoke_tests_signal_string_values():
    """CPEs with string signal values are parsed correctly."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = "-72"  # String, not float
    mock_cpe.last_local_rssi = "-72"
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True  # -72 is above -80 threshold


@pytest.mark.asyncio
async def test_smoke_tests_mixed_signal_levels():
    """Mix of good and bad signals: only bad ones trigger warning."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    good_cpe = MagicMock()
    good_cpe.ip = "10.0.0.100"
    good_cpe.combined_signal = -60
    good_cpe.last_local_rssi = -60

    bad_cpe = MagicMock()
    bad_cpe.ip = "10.0.0.101"
    bad_cpe.combined_signal = -85
    bad_cpe.last_local_rssi = -85

    client.get_connected_cpes = AsyncMock(return_value=[good_cpe, bad_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=2)
    assert result.passed is False
    assert any("10.0.0.101" in w for w in result.warnings)
    # Only 1 low signal CPE
    signal_check = next(c for c in result.checks if c["check"] == "cpe_signal_levels")
    assert "1 CPEs" in signal_check["detail"]


# ── SmokeTestResult dataclass tests ──


def test_smoke_test_result_defaults():
    """SmokeTestResult starts with passed=True and empty lists."""
    result = SmokeTestResult()
    assert result.passed is True
    assert result.warnings == []
    assert result.checks == []


def test_smoke_test_result_independent_instances():
    """Each SmokeTestResult has independent mutable lists."""
    r1 = SmokeTestResult()
    r2 = SmokeTestResult()
    r1.warnings.append("test")
    assert r2.warnings == []


# ── Edge case tests ──


@pytest.mark.asyncio
async def test_smoke_tests_signal_at_boundary():
    """Signal exactly at -80 dBm should NOT trigger a warning (threshold is < -80)."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -80  # Exactly at threshold
    mock_cpe.last_local_rssi = -80
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True
    signal_checks = [c for c in result.checks if c["check"] == "cpe_signal_levels"]
    assert signal_checks[0]["passed"] is True


@pytest.mark.asyncio
async def test_smoke_tests_cpe_count_increased():
    """CPE count increasing after update is not a warning."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    cpes = []
    for i in range(5):
        cpe = MagicMock()
        cpe.ip = f"10.0.0.{100 + i}"
        cpe.combined_signal = -65
        cpe.last_local_rssi = -65
        cpes.append(cpe)
    client.get_connected_cpes = AsyncMock(return_value=cpes)

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=2)
    assert result.passed is True  # 5 > 2 is fine
    cpe_check = next(c for c in result.checks if c["check"] == "cpe_connectivity")
    assert cpe_check["passed"] is True
    assert "5 CPEs" in cpe_check["detail"]


@pytest.mark.asyncio
async def test_smoke_tests_get_device_info_returns_none():
    """get_device_info returning None entirely (not a DeviceInfo) triggers warning."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=None)
    client.get_config = AsyncMock(return_value={})

    result = await client.run_smoke_tests(role="switch")
    assert result.passed is False
    responsive_check = next(c for c in result.checks if c["check"] == "device_responsive")
    assert responsive_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_signal_unparseable_string():
    """Signal value 'N/A' or other non-numeric string is silently skipped."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = "N/A"
    mock_cpe.last_local_rssi = "unknown"
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    # No signal warning — unparseable values are skipped
    assert not any("signal" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_smoke_tests_signal_zero_treated_as_no_data():
    """Signal value 0 is treated as 'no reading' and skipped."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = 0  # Some devices report 0 for "no reading"
    mock_cpe.last_local_rssi = 0
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True  # 0 treated as no data, not as a real signal


@pytest.mark.asyncio
async def test_smoke_tests_empty_version_string():
    """Device returning empty string for current_version triggers warning."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version=""))
    client.get_config = AsyncMock(return_value={})

    result = await client.run_smoke_tests(role="cpe")
    assert result.passed is False
    responsive_check = next(c for c in result.checks if c["check"] == "device_responsive")
    assert responsive_check["passed"] is False


@pytest.mark.asyncio
async def test_smoke_tests_all_checks_run_despite_early_failures():
    """Even if device_responsive and config_intact both fail, CPE check still runs."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(side_effect=Exception("Timeout"))
    client.get_config = AsyncMock(side_effect=Exception("Timeout"))

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -65
    mock_cpe.last_local_rssi = -65
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    check_names = [c["check"] for c in result.checks]
    assert "device_responsive" in check_names
    assert "config_intact" in check_names
    assert "cpe_connectivity" in check_names
    assert len(result.warnings) >= 2  # At least device + config warnings


@pytest.mark.asyncio
async def test_smoke_tests_combined_signal_preferred_over_rssi():
    """combined_signal is used when available, last_local_rssi as fallback."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = -65   # Good signal
    mock_cpe.last_local_rssi = -90   # Bad signal (should be ignored)
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is True  # Uses combined_signal (-65), not last_local_rssi (-90)


@pytest.mark.asyncio
async def test_smoke_tests_rssi_fallback_when_combined_is_zero():
    """Falls back to last_local_rssi when combined_signal is 0 (no reading)."""
    client = TachyonClient("10.0.0.1", "admin", "pass")
    client._token = "fake"
    client.get_device_info = AsyncMock(return_value=DeviceInfo(ip="10.0.0.1", current_version="2.5.1"))
    client.get_config = AsyncMock(return_value={})

    mock_cpe = MagicMock()
    mock_cpe.ip = "10.0.0.100"
    mock_cpe.combined_signal = 0     # No reading
    mock_cpe.last_local_rssi = -85   # Bad signal via fallback
    client.get_connected_cpes = AsyncMock(return_value=[mock_cpe])

    result = await client.run_smoke_tests(role="ap", pre_update_cpe_count=1)
    assert result.passed is False
    assert any("signal" in w.lower() for w in result.warnings)


def test_smoke_checks_json_round_trip():
    """Smoke check data survives JSON serialization (as it would in stages_json)."""
    checks = [
        {"check": "device_responsive", "passed": True, "detail": "Running 2.5.1"},
        {"check": "config_intact", "passed": False, "detail": "Config returned None"},
        {"check": "cpe_connectivity", "passed": False, "detail": "0/5 CPEs connected"},
    ]
    warnings = ["AP had 5 CPEs before update, now has 0", "Could not read device configuration"]
    stage = {
        "stage": "smoke_testing",
        "started_at": "2026-03-06T10:00:00",
        "completed_at": "2026-03-06T10:00:02",
        "success": True,
        "has_warnings": True,
        "smoke_checks": checks,
        "smoke_warnings": warnings,
    }
    serialized = json.dumps([stage])
    deserialized = json.loads(serialized)

    restored = deserialized[0]
    assert restored["stage"] == "smoke_testing"
    assert restored["has_warnings"] is True
    assert len(restored["smoke_checks"]) == 3
    assert restored["smoke_checks"][0]["check"] == "device_responsive"
    assert restored["smoke_warnings"] == warnings
