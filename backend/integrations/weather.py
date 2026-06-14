import logging
from dataclasses import dataclass

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


@dataclass
class WeatherData:
    condition: str = "Unknown"
    temp_f: float = 0.0
    feels_like_f: float = 0.0
    high_f: float = 0.0
    low_f: float = 0.0
    precip_chance_pct: int = 0
    wind_mph: float = 0.0
    summary: str = "Weather unavailable"


def _k_to_f(k: float) -> float:
    return round((k - 273.15) * 9/5 + 32, 1)


@async_ttl_cache(60)  # weather changes slowly; one shared fetch per minute
async def fetch() -> WeatherData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        api_key = settings.openweather_api_key
    except Exception:
        raise Exception("OPENWEATHER_API_KEY not configured")

    lat = settings.weather_lat
    lon = settings.weather_lon

    async with httpx.AsyncClient(timeout=5) as client:
        # Current weather
        resp = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": api_key},
        )
        resp.raise_for_status()
        current = resp.json()

        # Forecast for high/low and precip
        forecast_resp = await client.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": api_key, "cnt": 4},
        )
        forecast = forecast_resp.json() if forecast_resp.status_code == 200 else {"list": []}

    condition = current["weather"][0]["main"] if current.get("weather") else "Unknown"
    temp_f = _k_to_f(current["main"]["temp"])
    feels_f = _k_to_f(current["main"]["feels_like"])
    wind_mph = round(current.get("wind", {}).get("speed", 0) * 2.237, 1)

    forecast_list = forecast.get("list", [])
    temps = [_k_to_f(f["main"]["temp"]) for f in forecast_list]
    high_f = max(temps) if temps else temp_f
    low_f = min(temps) if temps else temp_f

    # Precip probability from 12h forecast
    precip_chance = 0
    for f in forecast_list[:4]:
        pop = f.get("pop", 0)
        precip_chance = max(precip_chance, int(pop * 100))

    summary = f"{condition}, {temp_f}°F"
    if precip_chance > 20:
        summary += f", {precip_chance}% chance of rain"

    return WeatherData(
        condition=condition,
        temp_f=temp_f,
        feels_like_f=feels_f,
        high_f=high_f,
        low_f=low_f,
        precip_chance_pct=precip_chance,
        wind_mph=wind_mph,
        summary=summary,
    )


async def health_check() -> bool:
    try:
        await fetch()
        return True
    except Exception:
        return False
