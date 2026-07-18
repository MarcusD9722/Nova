from __future__ import annotations

import math
import os
import re
import urllib.parse
from typing import Any

import httpx

from plugins.registry import PluginConfigError, tool


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise PluginConfigError(f"Missing required .env key: {key}")
    return val


def _parse_coords(value: str) -> dict[str, float] | None:
    text = (value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except Exception:
        return None
    return {"lat": lat, "lng": lng}


def _place_maps_url(*, place_id: str | None, query: str) -> str:
    if place_id:
        return f"https://www.google.com/maps/search/?api=1&query_place_id={urllib.parse.quote(place_id)}"
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    aa = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_m * (2 * math.atan2(math.sqrt(aa), math.sqrt(1 - aa)))


@tool(name="maps.geocode", description="Geocode an address to coordinates. args: {address}")
async def geocode(args: dict) -> dict:
    api_key = _require("GOOGLE_MAPS_API_KEY")
    address = str(args.get("address") or "").strip()
    if not address:
        raise ValueError("maps.geocode requires 'address'")

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": api_key},
        )
        response.raise_for_status()
        data = response.json()

    status = data.get("status")
    if status != "OK":
        return {"status": status, "results": data.get("results", [])}

    top = data["results"][0]
    loc = top.get("geometry", {}).get("location", {})
    formatted_address = top.get("formatted_address")
    place_id = top.get("place_id")
    maps_url = _place_maps_url(place_id=place_id, query=formatted_address or address)
    return {
        "kind": "places",
        "status": status,
        "query": address,
        "formatted_address": formatted_address,
        "location": {"lat": loc.get("lat"), "lng": loc.get("lng")},
        "place_id": place_id,
        "maps_url": maps_url,
        "places": [
            {
                "name": address,
                "address": formatted_address,
                "location": {"lat": loc.get("lat"), "lng": loc.get("lng")},
                "place_id": place_id,
                "rating": None,
                "user_ratings_total": None,
                "open_now": None,
                "distance_meters": None,
                "maps_url": maps_url,
            }
        ],
    }


@tool(
    name="maps.place_search",
    description=(
        "Search for a specific place, company, or address using Google Places Text Search. "
        "Useful for queries like 'Where is GXO Logistics?'. args: {query, limit?}"
    ),
)
async def place_search(args: dict[str, Any]) -> dict[str, Any]:
    api_key = _require("GOOGLE_MAPS_API_KEY")
    query = str(args.get("query") or args.get("address") or "").strip()
    if not query:
        raise ValueError("maps.place_search requires 'query'")

    limit = max(1, min(int(args.get("limit") or 6), 10))

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": query, "key": api_key},
        )
        response.raise_for_status()
        data = response.json()

    status = data.get("status")
    if status not in {"OK", "ZERO_RESULTS"}:
        return {"kind": "places", "status": status, "query": query, "error": data.get("error_message", "")}

    places: list[dict[str, Any]] = []
    for result in (data.get("results") or [])[:limit]:
        geometry = result.get("geometry", {}).get("location", {})
        place_lat = geometry.get("lat")
        place_lng = geometry.get("lng")
        place_id = result.get("place_id")
        maps_url = _place_maps_url(place_id=place_id, query=result.get("formatted_address") or result.get("name") or query)
        places.append(
            {
                "name": result.get("name") or query,
                "address": result.get("formatted_address") or result.get("vicinity"),
                "location": {"lat": place_lat, "lng": place_lng},
                "place_id": place_id,
                "rating": result.get("rating"),
                "user_ratings_total": result.get("user_ratings_total"),
                "open_now": (result.get("opening_hours") or {}).get("open_now"),
                "distance_meters": None,
                "maps_url": maps_url,
            }
        )

    return {
        "kind": "places",
        "status": status,
        "query": query,
        "places": places,
    }


@tool(
    name="maps.directions",
    description=(
        "Get turn-by-turn directions between two places using Google Maps Directions API. "
        "args: {origin, destination, mode?} where mode is driving|walking|bicycling|transit."
    ),
)
async def directions(args: dict) -> dict:
    api_key = _require("GOOGLE_MAPS_API_KEY")
    origin = str(args.get("origin") or args.get("from") or "").strip()
    destination = str(args.get("destination") or args.get("to") or "").strip()
    mode = str(args.get("mode") or "driving").strip().lower()
    if not origin:
        raise ValueError("maps.directions requires 'origin'")
    if not destination:
        raise ValueError("maps.directions requires 'destination'")

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={"origin": origin, "destination": destination, "mode": mode, "key": api_key},
        )
        response.raise_for_status()
        data = response.json()

    status = data.get("status")
    if status != "OK":
        return {"status": status, "error": data.get("error_message", "")}

    route = data["routes"][0]
    leg = route["legs"][0]
    steps = [
        {
            "instruction": re.sub(r"<[^>]+>", "", step.get("html_instructions", "")),
            "distance": step.get("distance", {}).get("text"),
            "duration": step.get("duration", {}).get("text"),
        }
        for step in leg.get("steps", [])
    ]

    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={urllib.parse.quote(origin)}"
        f"&destination={urllib.parse.quote(destination)}"
        f"&travelmode={mode}"
    )
    embed_url = (
        "https://www.google.com/maps/embed/v1/directions"
        f"?key={api_key}"
        f"&origin={urllib.parse.quote(origin)}"
        f"&destination={urllib.parse.quote(destination)}"
        f"&mode={mode}"
    )

    return {
        "kind": "route",
        "status": status,
        "origin": leg.get("start_address"),
        "destination": leg.get("end_address"),
        "origin_query": origin,
        "destination_query": destination,
        "origin_coords": _parse_coords(origin),
        "distance": leg.get("distance", {}).get("text"),
        "distance_meters": leg.get("distance", {}).get("value"),
        "duration": leg.get("duration", {}).get("text"),
        "duration_seconds": leg.get("duration", {}).get("value"),
        "mode": mode,
        "steps": steps,
        "maps_url": maps_url,
        "embed_url": embed_url,
        "overview_polyline": route.get("overview_polyline", {}).get("points"),
    }


@tool(
    name="maps.places_nearby",
    description=(
        "Find nearby places around a latitude/longitude using Google Places Nearby Search. "
        "args: {query, lat, lng, limit?}. Useful for nearest gas stations, restaurants, and similar."
    ),
)
async def places_nearby(args: dict[str, Any]) -> dict[str, Any]:
    api_key = _require("GOOGLE_MAPS_API_KEY")
    query = str(args.get("query") or args.get("keyword") or "").strip()
    if not query:
        raise ValueError("maps.places_nearby requires 'query'")

    lat = args.get("lat")
    lng = args.get("lng")
    if lat is None or lng is None:
        location_text = str(args.get("location") or "").strip()
        coords = _parse_coords(location_text)
        if coords is not None:
            lat = coords["lat"]
            lng = coords["lng"]
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception as exc:
        raise ValueError("maps.places_nearby requires numeric 'lat' and 'lng'") from exc

    limit = max(1, min(int(args.get("limit") or 6), 10))

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params={
                "location": f"{lat},{lng}",
                "rankby": "distance",
                "keyword": query,
                "key": api_key,
            },
        )
        response.raise_for_status()
        data = response.json()

    status = data.get("status")
    if status not in {"OK", "ZERO_RESULTS"}:
        return {"kind": "places", "status": status, "error": data.get("error_message", "")}

    places: list[dict[str, Any]] = []
    for result in (data.get("results") or [])[:limit]:
        geometry = result.get("geometry", {}).get("location", {})
        place_lat = geometry.get("lat")
        place_lng = geometry.get("lng")
        distance_meters = None
        if place_lat is not None and place_lng is not None:
            distance_meters = round(_haversine_meters(lat, lng, float(place_lat), float(place_lng)))
        place_id = result.get("place_id")
        maps_url = _place_maps_url(place_id=place_id, query=result.get("vicinity") or result.get("formatted_address") or result.get("name") or query)
        places.append(
            {
                "name": result.get("name"),
                "address": result.get("vicinity") or result.get("formatted_address"),
                "location": {"lat": place_lat, "lng": place_lng},
                "place_id": place_id,
                "rating": result.get("rating"),
                "user_ratings_total": result.get("user_ratings_total"),
                "open_now": (result.get("opening_hours") or {}).get("open_now"),
                "distance_meters": distance_meters,
                "maps_url": maps_url,
            }
        )

    return {
        "kind": "places",
        "status": status,
        "query": query,
        "origin_location": {"lat": lat, "lng": lng},
        "places": places,
    }
