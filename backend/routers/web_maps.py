from __future__ import annotations

"""Plugin-execution, web search/fetch, and maps proxy endpoints for the UI.

Moved verbatim from backend/app.py in Phase 0.6 — behavior unchanged.
"""

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.event_bus import BUS, clip as _event_clip
from core.logging_setup import get_logger
from plugins.registry import PluginConfigError, REGISTRY

logger = get_logger(__name__)

router = APIRouter()


class PluginExecuteRequest(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/plugins/execute")
async def plugins_execute(req: PluginExecuteRequest) -> dict:
    tools = REGISTRY.get_tools()
    if req.name not in tools:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.name}")
    try:
        result = await tools[req.name].fn(req.args)
        return {"name": req.name, "ok": True, "result": result}
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("plugin_execute_failed", tool=req.name, error=str(e))
        raise HTTPException(status_code=500, detail="plugin_execute_failed") from e


# ── Web search / fetch endpoints ─────────────────────────────────────────────

@router.get("/api/web/search")
async def api_web_search(q: str, max_results: int = 8) -> dict:
    """Proxy DuckDuckGo search for the frontend WebSheet widget."""
    from plugins.web_search import web_search as _web_search  # noqa: WPS433
    try:
        BUS.publish("web.search_start", {"query": _event_clip(q, 120)})
        result = await _web_search({"query": q, "max_results": max_results})
        BUS.publish("web.search_done", {"results": len(result.get("results", []) or [])})
        return result
    except Exception as e:  # noqa: BLE001
        BUS.publish("web.search_error", {"error": _event_clip(e, 200)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/web/fetch")
async def api_web_fetch(url: str, max_chars: int = 8000) -> dict:
    """Fetch and strip a web page for the frontend WebSheet widget."""
    from plugins.web_search import web_fetch as _web_fetch  # noqa: WPS433
    try:
        BUS.publish("web.fetch_start", {"url": _event_clip(url, 200)})
        result = await _web_fetch({"url": url, "max_chars": max_chars})
        BUS.publish("web.fetch_done", {})
        return result
    except Exception as e:  # noqa: BLE001
        BUS.publish("web.fetch_error", {"error": _event_clip(e, 200)})
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── Maps endpoints ───────────────────────────────────────────────────────────

@router.get("/api/maps/directions")
async def api_maps_directions(origin: str, destination: str, mode: str = "driving") -> dict:
    """Get directions + embed_url for the frontend MapsSheet widget."""
    from plugins.google_maps import directions as _directions  # noqa: WPS433
    try:
        return await _directions({"origin": origin, "destination": destination, "mode": mode})
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/maps/geocode")
async def api_maps_geocode(address: str) -> dict:
    """Resolve a typed location/address into coordinates for the frontend MapsSheet widget."""
    from plugins.google_maps import geocode as _geocode  # noqa: WPS433
    try:
        return await _geocode({"address": address})
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/maps/nearby")
async def api_maps_nearby(query: str, lat: float, lng: float, limit: int = 6) -> dict:
    """Find nearby places around the user's current location for the frontend MapsSheet widget."""
    from plugins.google_maps import places_nearby as _places_nearby  # noqa: WPS433
    try:
        return await _places_nearby({"query": query, "lat": lat, "lng": lng, "limit": limit})
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/maps/key")
async def api_maps_key() -> dict:
    """Return the Google Maps API key so the frontend can render the embed."""
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="GOOGLE_MAPS_API_KEY not set")
    return {"key": key}
