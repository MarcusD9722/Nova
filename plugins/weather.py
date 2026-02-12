from __future__ import annotations

import os

import httpx

from plugins.registry import PluginConfigError, tool


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise PluginConfigError(f"Missing required .env key: {key}")
    return val


@tool(name="weather.current", description="Get current weather by city name using OpenWeather.")
async def current_weather(args: dict) -> dict:
    api_key = _require("OPENWEATHER_API_KEY")
    city = str(args.get("city") or "").strip()
    if not city:
        raise ValueError("weather.current requires 'city'")

    units = str(args.get("units") or "metric").strip()
    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": api_key, "units": units},
        )
        r.raise_for_status()
        data = r.json()

    main = data.get("main", {})
    weather = (data.get("weather") or [{}])[0]
    return {
        "city": city,
        "units": units,
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "humidity": main.get("humidity"),
        "description": weather.get("description"),
    }
