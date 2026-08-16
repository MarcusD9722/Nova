from __future__ import annotations

import os

import httpx

from plugins.registry import PluginConfigError, tool


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise PluginConfigError(f"Missing required .env key: {key}")
    return val


@tool(
    name="weather.current",
    description="Get live current weather for a city OR coordinates. args: {city} or {lat, lon}, optional {units}",
    data_scope="shared",
)
async def current_weather(args: dict) -> dict:
    api_key = _require("OPENWEATHER_API_KEY")
    units = str(args.get("units") or "imperial").strip()
    city = str(args.get("city") or "").strip()
    lat = args.get("lat")
    lon = args.get("lon")

    params: dict = {"appid": api_key, "units": units}
    # Coordinates win when provided — they work for any location (street
    # addresses, landmarks) that the city-name lookup would 404 on.
    if lat is not None and lon is not None:
        try:
            params["lat"] = float(lat)
            params["lon"] = float(lon)
        except Exception as exc:
            raise ValueError("weather.current 'lat'/'lon' must be numeric") from exc
    elif city:
        params["q"] = city
    else:
        raise ValueError("weather.current requires 'city' or numeric 'lat'+'lon'")

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get("https://api.openweathermap.org/data/2.5/weather", params=params)
        r.raise_for_status()
        data = r.json()

    main = data.get("main", {})
    weather = (data.get("weather") or [{}])[0]
    return {
        "city": city or data.get("name"),
        "units": units,
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "humidity": main.get("humidity"),
        "description": weather.get("description"),
    }
