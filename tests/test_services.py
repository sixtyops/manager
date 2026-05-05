"""Tests for updater.services."""

from unittest.mock import patch, AsyncMock
import asyncio
import json

import pytest

from datetime import datetime

from updater.services import (
    get_current_time,
    is_in_schedule_window,
    minutes_until_window_end,
    c_to_f,
    f_to_c,
    format_temperature,
    get_temperature_unit_from_location,
    validate_time_sources,
)


class TestTemperatureConversions:
    def test_c_to_f_freezing(self):
        assert c_to_f(0) == 32.0

    def test_c_to_f_boiling(self):
        assert c_to_f(100) == 212.0

    def test_c_to_f_negative(self):
        assert c_to_f(-40) == -40.0  # Same in both scales

    def test_f_to_c_freezing(self):
        assert f_to_c(32) == 0.0

    def test_f_to_c_boiling(self):
        assert f_to_c(212) == 100.0

    def test_f_to_c_negative(self):
        assert f_to_c(-40) == -40.0


class TestFormatTemperature:
    def test_format_celsius(self):
        assert format_temperature(-10.0, "c") == "-10.0°C"

    def test_format_fahrenheit(self):
        assert format_temperature(-10.0, "f") == "14.0°F"

    def test_format_zero_celsius(self):
        assert format_temperature(0.0, "c") == "0.0°C"

    def test_format_zero_fahrenheit(self):
        assert format_temperature(0.0, "f") == "32.0°F"


class TestGetTemperatureUnitFromLocation:
    def test_us_uses_fahrenheit(self):
        assert get_temperature_unit_from_location("US") == "f"

    def test_us_lowercase(self):
        assert get_temperature_unit_from_location("us") == "f"

    def test_canada_uses_celsius(self):
        assert get_temperature_unit_from_location("CA") == "c"

    def test_uk_uses_celsius(self):
        assert get_temperature_unit_from_location("GB") == "c"

    def test_empty_defaults_celsius(self):
        assert get_temperature_unit_from_location("") == "c"

    def test_none_defaults_celsius(self):
        assert get_temperature_unit_from_location(None) == "c"


class TestGetCurrentTime:
    def test_returns_expected_keys(self):
        result = get_current_time("America/Chicago")
        assert "time" in result
        assert "date" in result
        assert "timezone" in result
        assert "hour" in result
        assert "day_of_week" in result
        assert "iso" in result

    def test_timezone_fallback(self):
        result = get_current_time("Invalid/Timezone")
        assert result["timezone"] == "Invalid/Timezone"  # passed through, ZoneInfo falls back

    def test_default_timezone(self):
        result = get_current_time()
        assert result["timezone"] == "America/Chicago"


class TestIsInScheduleWindow:
    def test_inside_window(self):
        assert is_in_schedule_window(3, "tue", ["tue", "wed", "thu"], 3, 4) is True

    def test_outside_window_wrong_hour(self):
        assert is_in_schedule_window(5, "tue", ["tue", "wed", "thu"], 3, 4) is False

    def test_outside_window_wrong_day(self):
        assert is_in_schedule_window(3, "mon", ["tue", "wed", "thu"], 3, 4) is False

    def test_boundary_start(self):
        assert is_in_schedule_window(3, "wed", ["wed"], 3, 5) is True

    def test_boundary_end(self):
        # end_hour is exclusive
        assert is_in_schedule_window(4, "wed", ["wed"], 3, 4) is False

    def test_empty_days(self):
        assert is_in_schedule_window(3, "tue", [], 3, 4) is False


class TestMinutesUntilWindowEnd:
    def test_same_day_window_inside(self):
        now = datetime(2026, 5, 5, 3, 30)
        assert minutes_until_window_end(now, 3, 4) == 30

    def test_same_day_window_at_end_hour(self):
        """At exactly end_hour, minutes should be <= 0 so per-device cutoff defers."""
        now = datetime(2026, 5, 5, 4, 0)
        assert minutes_until_window_end(now, 3, 4) <= 0

    def test_same_day_window_past_end(self):
        now = datetime(2026, 5, 5, 5, 30)
        assert minutes_until_window_end(now, 3, 4) < 0

    def test_overnight_window_evening(self):
        now = datetime(2026, 5, 5, 22, 30)
        assert minutes_until_window_end(now, 20, 4) == 5 * 60 + 30

    def test_overnight_window_morning(self):
        now = datetime(2026, 5, 5, 1, 30)
        assert minutes_until_window_end(now, 20, 4) == 2 * 60 + 30

    def test_overnight_window_at_morning_end(self):
        now = datetime(2026, 5, 5, 4, 0)
        assert minutes_until_window_end(now, 20, 4) <= 0

    def test_overnight_window_past_morning_end(self):
        now = datetime(2026, 5, 5, 5, 30)
        assert minutes_until_window_end(now, 20, 4) < 0


class TestValidateTimeSources:
    @pytest.mark.asyncio
    async def test_external_time_unavailable_blocks(self):
        with patch("updater.services.get_external_time", new_callable=AsyncMock, return_value=None):
            ok, result = await validate_time_sources("America/Chicago")
        assert ok is False
        assert "verify trusted time source" in result.lower()

    @pytest.mark.asyncio
    async def test_drift_uses_post_request_clock_sample(self, monkeypatch):
        """system_now must be sampled after the external response so that
        request latency does not register as drift (regression for P1-7)."""

        external_time = datetime(2026, 5, 5, 12, 0, 0)
        sample_calls = []

        from updater import services as svc

        async def fake_get_external_time(tz):
            await asyncio.sleep(0)
            return external_time

        real_now = svc.datetime.now

        class _DT(svc.datetime):
            @classmethod
            def now(cls, tz=None):
                sample_calls.append("now")
                return external_time.replace(tzinfo=tz) if tz else external_time

        monkeypatch.setattr(svc, "get_external_time", fake_get_external_time)
        monkeypatch.setattr(svc, "datetime", _DT)

        ok, result = await svc.validate_time_sources("America/Chicago", max_drift=300)

        # Should have called datetime.now exactly once (after external fetch).
        assert sample_calls == ["now"]
        assert ok is True
        # Sanity: real_now still callable, no leak
        assert real_now() is not None


class TestGetLocationFromIP:
    @pytest.mark.asyncio
    async def test_success(self):
        from updater.services import get_location_from_ip, _location_cache
        _location_cache.clear()

        mock_data = json.dumps({"status": "success", "city": "Chicago", "regionName": "Illinois", "timezone": "America/Chicago"})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(mock_data.encode(), b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await get_location_from_ip()
            assert result["city"] == "Chicago"

        _location_cache.clear()

    @pytest.mark.asyncio
    async def test_failure(self):
        from updater.services import get_location_from_ip, _location_cache
        _location_cache.clear()

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await get_location_from_ip()
            assert result is None

        _location_cache.clear()

    @pytest.mark.asyncio
    async def test_caching(self):
        from updater.services import get_location_from_ip, _location_cache
        _location_cache.clear()
        _location_cache["ip_location"] = {"city": "Cached"}

        result = await get_location_from_ip()
        assert result["city"] == "Cached"

        _location_cache.clear()


class TestCheckWeatherOk:
    @pytest.mark.asyncio
    async def test_below_threshold(self):
        from updater.services import check_weather_ok

        mock_weather = {"temperature_c": -15.0, "temperature_f": 5.0, "description": "Cold"}

        with patch("updater.services.get_coordinates", new_callable=AsyncMock, return_value=(40.0, -90.0)), \
             patch("updater.services.get_weather_forecast", new_callable=AsyncMock, return_value=mock_weather):
            ok, data = await check_weather_ok(min_temp_c=-10)
            assert ok is False
            assert data["temperature_c"] == -15.0

    @pytest.mark.asyncio
    async def test_above_threshold(self):
        from updater.services import check_weather_ok

        mock_weather = {"temperature_c": 5.0, "temperature_f": 41.0, "description": "Mild"}

        with patch("updater.services.get_coordinates", new_callable=AsyncMock, return_value=(40.0, -90.0)), \
             patch("updater.services.get_weather_forecast", new_callable=AsyncMock, return_value=mock_weather):
            ok, data = await check_weather_ok(min_temp_c=-10)
            assert ok is True

    @pytest.mark.asyncio
    async def test_no_coords(self):
        from updater.services import check_weather_ok

        with patch("updater.services.get_coordinates", new_callable=AsyncMock, return_value=None):
            ok, data = await check_weather_ok()
            assert ok is True
            assert data is None
