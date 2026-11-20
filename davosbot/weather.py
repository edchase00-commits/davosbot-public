"""Weather tool helpers backed by Open-Meteo."""

import logging

import requests


logger = logging.getLogger(__name__)

_WEATHER_CODE_LABELS = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    95: "thunderstorms",
}


def _geocode_location(location: str) -> tuple[str, float, float] | None:
    query = (location or "").strip() or "Belltown, Seattle, WA"
    lower = query.lower()
    if lower in {"seattle", "seattle wa", "seattle, wa"}:
        return ("Seattle, WA", 47.6062, -122.3321)
    if "belltown" in lower:
        return ("Belltown, Seattle, WA", 47.6132, -122.3454)

    resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": query, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    result = (resp.json().get("results") or [None])[0]
    if not result:
        return None
    admin = result.get("admin1") or result.get("country") or ""
    label = ", ".join(part for part in (result.get("name"), admin) if part)
    return (label or query, float(result["latitude"]), float(result["longitude"]))


def _get_weather(location: str = "") -> str:
    """Live weather via Open-Meteo; no API key and no stale hardcoded forecast."""
    try:
        geocoded = _geocode_location(location)
        if not geocoded:
            return f"I couldn't resolve weather for '{location}'. Try a city/neighborhood like Seattle or Belltown."
        label, lat, lon = geocoded
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        current = resp.json().get("current") or {}
        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        precip = current.get("precipitation")
        wind = current.get("wind_speed_10m")
        code = current.get("weather_code")
        label_code = _WEATHER_CODE_LABELS.get(code, f"code {code}") if code is not None else "unknown"
        fmt = lambda value, suffix="", places=0: "unknown" if value is None else f"{float(value):.{places}f}{suffix}"
        return (
            f"Weather for {label}: {fmt(temp, 'F')}, feels {fmt(feels, 'F')}, {label_code}, "
            f"wind {fmt(wind, ' mph')}, precip {fmt(precip, ' in', 2)}."
        )
    except Exception as e:
        logger.warning("get_weather failed for %r: %s", location, e)
        return "Weather lookup failed live. Try again in a minute or give me a more specific place."
