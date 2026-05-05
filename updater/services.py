"""External services for location, weather, and time."""

import asyncio
import json
import logging
import time

import httpx
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Cache for location/timezone data with TTL and size limit
_location_cache: dict = {}
_location_cache_times: dict = {}
_LOCATION_CACHE_TTL = 3600  # 1 hour
_LOCATION_CACHE_MAX = 256  # Max entries before eviction


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
    # Evict oldest entries if cache is full
    if len(_location_cache) >= _LOCATION_CACHE_MAX and key not in _location_cache:
        oldest_key = min(_location_cache_times, key=_location_cache_times.get)
        _location_cache.pop(oldest_key, None)
        _location_cache_times.pop(oldest_key, None)
    _location_cache[key] = value
    _location_cache_times[key] = time.monotonic()


# Countries that use Fahrenheit (ISO 3166-1 alpha-2 codes)
_FAHRENHEIT_COUNTRIES = {"US", "BS", "KY", "LR", "PW", "FM", "MH"}


def get_temperature_unit_from_location(country_code: str) -> str:
    """Determine temperature unit based on country code.

    Returns 'f' for Fahrenheit countries (US and a few others), 'c' otherwise.
    """
    if country_code and country_code.upper() in _FAHRENHEIT_COUNTRIES:
        return "f"
    return "c"


def c_to_f(temp_c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return round(temp_c * 9 / 5 + 32, 1)


def f_to_c(temp_f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return round((temp_f - 32) * 5 / 9, 1)


def format_temperature(temp_c: float, unit: str) -> str:
    """Format a temperature (stored as Celsius) for display.

    Args:
        temp_c: Temperature in Celsius
        unit: 'c' for Celsius, 'f' for Fahrenheit

    Returns:
        Formatted string like '-10.0°C' or '14°F'
    """
    if unit == "f":
        return f"{c_to_f(temp_c)}°F"
    return f"{temp_c}°C"


async def get_location_from_ip() -> Optional[dict]:
    """Get location data from public IP using ipwho.is (HTTPS)."""
    cached = _cache_get("ip_location")
    if cached is not None:
        return cached

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", "https://ipwho.is/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            if data.get("success") is True or data.get("status") == "success":
                tz_value = data.get("timezone")
                if isinstance(tz_value, dict):
                    tz_value = tz_value.get("id")
                normalized = {
                    "status": "success",
                    "city": data.get("city"),
                    "regionName": data.get("regionName") or data.get("region"),
                    "countryCode": data.get("countryCode") or data.get("country_code"),
                    "timezone": tz_value,
                    "lat": data.get("lat") if data.get("lat") is not None else data.get("latitude"),
                    "lon": data.get("lon") if data.get("lon") is not None else data.get("longitude"),
                }
                _cache_set("ip_location", normalized)
                logger.info(f"Detected location: {normalized.get('city')}, {normalized.get('regionName')}")
                return normalized
    except Exception as e:
        logger.error(f"Failed to get location from IP: {e}")

    return None


async def get_location_from_postal_code(postal_code: str) -> Optional[dict]:
    """Get location data from postal code using zippopotam.us.

    Uses the country detected from IP for the lookup.
    """
    # Get country from IP (default to US if unavailable)
    ip_location = await get_location_from_ip()
    country_code = "us"
    if ip_location:
        country_code = ip_location.get("countryCode", "US").lower()

    cache_key = f"postal_{country_code}_{postal_code}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        url = f"https://api.zippopotam.us/{country_code}/{postal_code}"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            if "places" in data and len(data["places"]) > 0:
                place = data["places"][0]
                result = {
                    "postal_code": postal_code,
                    "country": country_code.upper(),
                    "city": place.get("place name"),
                    "state": place.get("state abbreviation") or place.get("state"),
                    "lat": float(place.get("latitude", 0)),
                    "lon": float(place.get("longitude", 0)),
                }
                _cache_set(cache_key, result)
                logger.info(f"Location from postal code {postal_code} ({country_code.upper()}): {result['city']}, {result['state']}")
                return result
    except Exception as e:
        logger.error(f"Failed to get location from postal code {postal_code}: {e}")

    return None


# Alias for backwards compatibility
async def get_location_from_zip(zip_code: str) -> Optional[dict]:
    """Alias for get_location_from_postal_code (backwards compatibility)."""
    return await get_location_from_postal_code(zip_code)


async def resolve_temperature_unit(setting: str) -> str:
    """Resolve the temperature unit setting to 'c' or 'f'.

    Args:
        setting: 'auto', 'c', or 'f'

    Returns:
        'c' for Celsius, 'f' for Fahrenheit
    """
    if setting in ("c", "f"):
        return setting

    # Auto-detect from location
    location = await get_location_from_ip()
    if location:
        country_code = location.get("countryCode", "")
        return get_temperature_unit_from_location(country_code)

    # Default to Fahrenheit (US-centric for weather.gov API)
    return "f"


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
    """Get current weather from Open-Meteo API (works internationally, no API key)."""
    try:
        # Open-Meteo returns temperature in Celsius by default
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,wind_speed_10m"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "15", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        data = json.loads(stdout.decode())
        current = data.get("current", {})

        if current:
            temp_c = current.get("temperature_2m")
            temp_f = c_to_f(temp_c) if temp_c is not None else None
            weather_code = current.get("weather_code", 0)
            wind_speed = current.get("wind_speed_10m")

            # Map WMO weather codes to descriptions
            description = _weather_code_to_description(weather_code)

            return {
                "temperature_c": round(temp_c, 1) if temp_c is not None else None,
                "temperature_f": temp_f,
                "description": description,
                "wind": f"{wind_speed} km/h" if wind_speed else None,
                "time": current.get("time"),
            }

    except Exception as e:
        logger.error(f"Failed to get weather forecast: {e}")

    return None


def _weather_code_to_description(code: int) -> str:
    """Convert WMO weather code to human-readable description."""
    # https://open-meteo.com/en/docs (WMO Weather interpretation codes)
    descriptions = {
        0: "Clear sky",
        1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Depositing rime fog",
        51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
        61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
        66: "Light freezing rain", 67: "Heavy freezing rain",
        71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
        77: "Snow grains",
        80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
        85: "Slight snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
    }
    return descriptions.get(code, "Unknown")


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


def minutes_until_window_end(now: datetime, start_hour: int, end_hour: int) -> int:
    """Compute minutes remaining in the active schedule window.

    Returns 0 or a negative value if `now` is at or past the window end.
    Callers can compare against a buffer to decide whether to defer work.
    """
    if start_hour < end_hour:
        # Same-day window (e.g., 03:00-04:00). Negative when past end_hour.
        return (end_hour - now.hour) * 60 - now.minute
    # Overnight window (e.g., 20:00-04:00).
    if now.hour >= start_hour:
        # Evening portion: end is tomorrow morning.
        return (24 - now.hour + end_hour) * 60 - now.minute
    # Morning portion (or past it). Negative when past end_hour.
    return (end_hour - now.hour) * 60 - now.minute


async def get_external_time(timezone: str) -> Optional[datetime]:
    """Fetch current time from an external API.

    Tries worldtimeapi.org first, then timeapi.io as fallback.
    Returns datetime or None on failure.
    """
    sources = [
        (f"https://worldtimeapi.org/api/timezone/{timezone}", "datetime"),
        (f"https://timeapi.io/api/time/current/zone?timeZone={timezone}", "dateTime"),
    ]
    for url, key in sources:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                dt_str = data.get(key)
                if dt_str:
                    return datetime.fromisoformat(dt_str)
        except Exception as e:
            logger.warning(f"Time source {url} failed: {e}")
            continue
    return None


async def validate_time_sources(timezone: str, max_drift: int = 300) -> Tuple[bool, object]:
    """Compare system clock vs external time source.

    Args:
        timezone: IANA timezone string
        max_drift: Maximum allowed drift in seconds (default 300 = 5 min)

    Returns:
        (True, system_datetime) if valid,
        (False, error_string) if external time is unavailable or drift exceeds max_drift.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("America/Chicago")

    external_now = await get_external_time(timezone)
    # Sample the system clock after the external response so the comparison
    # reflects the actual offset, not the request latency.
    system_now = datetime.now(tz)
    if external_now is None:
        return (False, "Unable to verify trusted time source")

    # Make both offset-aware for comparison
    if external_now.tzinfo is None:
        external_now = external_now.replace(tzinfo=tz)

    drift = abs((system_now - external_now).total_seconds())
    logger.info(f"Time validation: system={system_now.isoformat()}, external={external_now.isoformat()}, drift={drift:.0f}s")

    if drift > max_drift:
        return (False, f"Clock drift too large: {drift:.0f}s (max {max_drift}s). System: {system_now.strftime('%H:%M')}, External: {external_now.strftime('%H:%M')}")

    return (True, system_now)
