"""External services for location, weather, and time."""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Cache for location/timezone data with TTL
_location_cache: dict = {}
_location_cache_times: dict = {}
_LOCATION_CACHE_TTL = 3600  # 1 hour


def _cache_get(key: str) -> Optional[any]:
    """Get a value from the location cache if not expired."""
    if key in _location_cache:
        cached_at = _location_cache_times.get(key, 0)
        if time.monotonic() - cached_at < _LOCATION_CACHE_TTL:
            return _location_cache[key]
        # Expired - remove
        _location_cache.pop(key, None)
        _location_cache_times.pop(key, None)
    return None


def _cache_set(key: str, value):
    """Set a value in the location cache with current timestamp."""
    _location_cache[key] = value
    _location_cache_times[key] = time.monotonic()


async def get_location_from_ip() -> Optional[dict]:
    """Get location data from public IP using ip-api.com."""
    cached = _cache_get("ip_location")
    if cached is not None:
        return cached

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", "http://ip-api.com/json/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            if data.get("status") == "success":
                _cache_set("ip_location", data)
                logger.info(f"Detected location: {data.get('city')}, {data.get('regionName')}")
                return data
    except Exception as e:
        logger.error(f"Failed to get location from IP: {e}")

    return None


async def get_location_from_zip(zip_code: str) -> Optional[dict]:
    """Get location data from zip code using zippopotam.us."""
    cached = _cache_get(f"zip_{zip_code}")
    if cached is not None:
        return cached

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", f"https://api.zippopotam.us/us/{zip_code}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            if "places" in data and len(data["places"]) > 0:
                place = data["places"][0]
                result = {
                    "zip": zip_code,
                    "city": place.get("place name"),
                    "state": place.get("state abbreviation"),
                    "lat": float(place.get("latitude", 0)),
                    "lon": float(place.get("longitude", 0)),
                }
                _cache_set(f"zip_{zip_code}", result)
                logger.info(f"Location from zip {zip_code}: {result['city']}, {result['state']}")
                return result
    except Exception as e:
        logger.error(f"Failed to get location from zip {zip_code}: {e}")

    return None


async def get_timezone() -> str:
    """Get timezone string, defaulting to America/Chicago."""
    location = await get_location_from_ip()
    if location and location.get("timezone"):
        return location["timezone"]
    return "America/Chicago"


async def get_coordinates(zip_code: str = None) -> Optional[Tuple[float, float]]:
    """Get lat/lon coordinates from IP or zip code."""
    if zip_code:
        location = await get_location_from_zip(zip_code)
        if location:
            return (location["lat"], location["lon"])

    location = await get_location_from_ip()
    if location:
        return (location.get("lat"), location.get("lon"))

    return None


async def get_weather_forecast(lat: float, lon: float) -> Optional[dict]:
    """Get weather forecast from weather.gov API."""
    try:
        # First, get the forecast grid endpoint for this location
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "15",
            "-H", "User-Agent: TachyonManagementSystem/1.0",
            points_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        points_data = json.loads(stdout.decode())
        forecast_url = points_data.get("properties", {}).get("forecastHourly")

        if not forecast_url:
            logger.error("No forecast URL in weather.gov response")
            return None

        # Get the hourly forecast
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "15",
            "-H", "User-Agent: TachyonManagementSystem/1.0",
            forecast_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        forecast_data = json.loads(stdout.decode())
        periods = forecast_data.get("properties", {}).get("periods", [])

        if periods:
            # Return current/next period
            current = periods[0]
            temp_f = current.get("temperature")
            temp_c = (temp_f - 32) * 5 / 9 if temp_f is not None else None

            return {
                "temperature_f": temp_f,
                "temperature_c": round(temp_c, 1) if temp_c is not None else None,
                "description": current.get("shortForecast"),
                "wind": current.get("windSpeed"),
                "time": current.get("startTime"),
            }

    except Exception as e:
        logger.error(f"Failed to get weather forecast: {e}")

    return None


async def check_weather_ok(zip_code: str = None, min_temp_c: float = -10) -> Tuple[bool, Optional[dict]]:
    """Check if weather conditions are OK for updates.

    Returns (is_ok, weather_data).
    """
    coords = await get_coordinates(zip_code)
    if not coords:
        logger.warning("Could not get coordinates for weather check, allowing update")
        return (True, None)

    weather = await get_weather_forecast(coords[0], coords[1])
    if not weather:
        logger.warning("Could not get weather data, allowing update")
        return (True, None)

    temp_c = weather.get("temperature_c")
    if temp_c is not None and temp_c < min_temp_c:
        logger.warning(f"Temperature {temp_c}°C is below minimum {min_temp_c}°C, blocking update")
        return (False, weather)

    return (True, weather)


def get_current_time(timezone: str = "America/Chicago") -> dict:
    """Get current time info for display."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("America/Chicago")

    now = datetime.now(tz)

    return {
        "time": now.strftime("%I:%M %p"),
        "date": now.strftime("%A, %B %d, %Y"),
        "timezone": timezone,
        "hour": now.hour,
        "day_of_week": now.strftime("%a").lower(),
        "iso": now.isoformat(),
    }


def is_in_schedule_window(
    current_hour: int,
    current_day: str,
    schedule_days: list[str],
    start_hour: int,
    end_hour: int,
) -> bool:
    """Check if current time is within the scheduled update window.

    Handles overnight windows (e.g., 20:00-04:00) where start_hour > end_hour.
    For overnight windows, the day check applies to the start of the window.
    """
    if start_hour < end_hour:
        # Same-day window (e.g., 3:00-4:00)
        if not (start_hour <= current_hour < end_hour):
            return False
        return current_day in schedule_days
    else:
        # Overnight window (e.g., 20:00-04:00)
        if current_hour >= start_hour:
            # Evening portion (e.g., 20:00-23:59) - current day must be in schedule
            return current_day in schedule_days
        elif current_hour < end_hour:
            # Morning portion (e.g., 00:00-03:59) - previous day started the window
            day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            try:
                idx = day_order.index(current_day)
                prev_day = day_order[(idx - 1) % 7]
                return prev_day in schedule_days
            except ValueError:
                return False
        else:
            # Outside the window (between end_hour and start_hour)
            return False


async def get_external_time(timezone: str) -> Optional[datetime]:
    """Fetch current time from worldtimeapi.org for the given timezone.

    Returns datetime or None on failure.
    """
    try:
        url = f"http://worldtimeapi.org/api/timezone/{timezone}"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            dt_str = data.get("datetime")
            if dt_str:
                return datetime.fromisoformat(dt_str)
    except Exception as e:
        logger.error(f"Failed to get external time: {e}")

    return None


async def validate_time_sources(timezone: str, max_drift: int = 300) -> Tuple[bool, object]:
    """Compare system clock vs external time source.

    Args:
        timezone: IANA timezone string
        max_drift: Maximum allowed drift in seconds (default 300 = 5 min)

    Returns:
        (True, system_datetime) if valid or external unavailable (fail-open),
        (False, error_string) if drift exceeds max_drift.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("America/Chicago")

    system_now = datetime.now(tz)

    external_now = await get_external_time(timezone)
    if external_now is None:
        logger.warning("External time source unavailable, allowing update (fail-open)")
        return (True, system_now)

    # Make both offset-aware for comparison
    if external_now.tzinfo is None:
        external_now = external_now.replace(tzinfo=tz)

    drift = abs((system_now - external_now).total_seconds())
    logger.info(f"Time validation: system={system_now.isoformat()}, external={external_now.isoformat()}, drift={drift:.0f}s")

    if drift > max_drift:
        return (False, f"Clock drift too large: {drift:.0f}s (max {max_drift}s). System: {system_now.strftime('%H:%M')}, External: {external_now.strftime('%H:%M')}")

    return (True, system_now)


