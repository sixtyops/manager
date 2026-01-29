"""Tests for updater.services."""

from unittest.mock import patch, AsyncMock
import asyncio
import json

import pytest

from updater.services import get_current_time, is_in_schedule_window


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
