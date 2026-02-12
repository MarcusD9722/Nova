from __future__ import annotations

import os

import httpx

from plugins.registry import PluginConfigError, tool


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise PluginConfigError(f"Missing required .env key: {key}")
    return val


@tool(name="maps.geocode", description="Geocode an address using Google Maps Geocoding API.")
async def geocode(args: dict) -> dict:
    api_key = _require("GOOGLE_MAPS_API_KEY")
    address = str(args.get("address") or "").strip()
    if not address:
        raise ValueError("maps.geocode requires 'address'")

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": api_key},
        )
        r.raise_for_status()
        data = r.json()

    status = data.get("status")
    if status != "OK":
        return {"status": status, "results": data.get("results", [])}

    top = data["results"][0]
    loc = top.get("geometry", {}).get("location", {})
    return {
        "status": status,
        "formatted_address": top.get("formatted_address"),
        "location": {"lat": loc.get("lat"), "lng": loc.get("lng")},
        "place_id": top.get("place_id"),
    }
