from __future__ import annotations

"""Navigation — routes, nearby places, map lookups (U10 strangler step 1).

Lifted out of `core/runtime.py`, which had reached 2,209 lines and picked up a
new reason to change from every feature round. Navigation goes first for two
reasons: it is genuinely self-contained, and it is where the bug shipped —
"we all just got home and we are about to go to sleep" was read as a routing
request and answered with "Happy to route you to sleep — where are you
starting from?".

This move changes NO behavior. It is pinned from both ends:
  * tests/test_destination_misroute.py — the extraction patterns
  * tests/test_it_navigation.py        — the live decisions, on a real backend

## Why two entry points instead of one

`RuntimeManager._direct_live_reply` runs its deterministic pre-passes in a
deliberate order, and navigation sits on BOTH sides of the others:

    resolve_pending()   ← must run FIRST. An open "where are you starting
                          from?" owns the next message, before the identity,
                          clock and weather passes get to look at it.
    ...identity / clock / weather...
    handle()            ← nearest / place lookup / destination / directions

Collapsing those into one call would silently reorder the pipeline, so the
seam keeps them apart.

## The two rules this file exists to protect

1. **A declarative is not a request.** People say "go to bed", "go to work",
   "go to sleep" constantly. A real navigation request LEADS with the verb;
   `_TO_DESTINATION_PATTERNS` is anchored accordingly, with `_NON_DESTINATIONS`
   as defense in depth.
2. **A call that could not run is not a call that found nothing.** A missing
   API key must be reported as a missing API key (`maps_unavailable_reply`),
   never as "I couldn't find that address" — which sends Marcus round in
   circles retyping addresses that were never going to resolve.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any

from core.intent import is_question, strip_preamble
from core.logging_setup import get_logger
from core.tool_router import ToolCall, ToolRouter

logger = get_logger(__name__)

#: (reply text, tool calls made, conversation mode) — the shape every
#: deterministic pre-pass in RuntimeManager returns.
Reply = tuple[str, list[dict[str, Any]], str]

__all__ = [
    "Navigation", "Reply",
    "extract_directions", "extract_nearest_query", "extract_place_lookup_query",
    "extract_destination_from_here", "current_coords_text", "wants_device_location",
    "looks_like_location_answer", "format_directions_reply", "maps_unavailable_reply",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Language patterns ────────────────────────────────────────────────────────

_DIRS_KEYWORD_RE = re.compile(
    r'\b(?:directions?|route|navigate|how\s+(?:do\s+i\s+)?(?:get|go)|how\s+far)\b',
    re.IGNORECASE,
)
_DIRS_FROM_TO_RE = re.compile(
    r'\bfrom\b\s+(.+?)\s+\bto\b\s+(.+?)(?:\s+\bby\b\s+(\w+))?(?:\?|$)',
    re.IGNORECASE,
)
FROM_HERE_RE = re.compile(r"\b(?:from\s+here|from\s+my\s+location|near\s+me|around\s+me)\b", re.IGNORECASE)
_NEAREST_QUERY_RE = re.compile(r"\b(?:nearest|closest)\s+(.+?)(?:\?|$)", re.IGNORECASE)
_PLACE_LOOKUP_PATTERNS = [
    re.compile(r"\bwhere\s+is\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bwhere's\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bfind\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\blocate\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bshow\s+me\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
]
_TO_DESTINATION_PATTERNS = [
    re.compile(r"\bhow\s+do\s+i\s+get\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    re.compile(r"\bdirections?\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    re.compile(r"\bhow\s+long\s+will\s+it\s+take(?:\s+to\s+get)?\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    # ANCHORED to the start of the message (after an optional address/politeness
    # lead). Unanchored, this matched ANY mid-sentence "go to": "we are about to
    # go to sleep" extracted "sleep" as a destination and Nova offered to route
    # Marcus there. A real navigation request leads with the verb; a declarative
    # about your evening does not.
    re.compile(
        r"^(?:\s*(?:hey\s+)?nova[,:\s]+)?"
        r"(?:(?:can|could|would)\s+you\s+|please\s+|let'?s\s+)?"
        r"(?:get|go|drive|walk|navigate|head)\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)",
        re.IGNORECASE,
    ),
    re.compile(r"\btake\s+me\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
]

# Things you "go to" that are not places on a map. Defense in depth behind the
# anchoring above — a routing offer for one of these is always wrong.
_NON_DESTINATIONS = {
    "sleep", "bed", "rest", "work out", "sleep now", "bed now",
    "the bathroom", "bathroom", "town",
}

_USE_DEVICE_LOCATION_RE = re.compile(
    r"\b(?:use\s+my\s+(?:current\s+)?location|my\s+(?:current\s+)?location|from\s+here|near\s+me|around\s+me|current\s+location|where\s+i\s+am)\b",
    re.IGNORECASE,
)
# Calling off an open "where are you starting from?". Matched as a LEADING
# phrase, not an exact string: observed live, "nevermind, forget the directions"
# missed the exact-match set entirely and got geocoded as an address, so Nova
# answered "I couldn't find 'nevermind, forget the directions' on the map".
# No address opens with these words — `\b` keeps "North Austin" safe from "no".
_CANCEL_RE = re.compile(
    r"^\s*(?:no\s+thanks|nope|nah|nevermind|never\s+mind|cancel|forget\s+it|"
    r"drop\s+it|skip\s+it|stop|no)\b",
    re.IGNORECASE,
)

READ_STEPS_RE = re.compile(
    r"\b(?:read|say|tell\s+me|give\s+me|what\s+are)\b.{0,30}\b(?:steps|directions|turn[\s-]?by[\s-]?turn|route|them)\b",
    re.IGNORECASE,
)

# How long an unanswered "where are you starting from?" stays live, and how
# long the last route stays readable aloud.
PENDING_TTL_S = 300      # 5 min
LAST_ROUTE_TTL_S = 1800  # 30 min


# ── Pure extraction / formatting ─────────────────────────────────────────────

def extract_directions(text: str) -> tuple[str, str, str] | None:
    if not _DIRS_KEYWORD_RE.search(text):
        return None
    m = _DIRS_FROM_TO_RE.search(text)
    if not m:
        return None
    origin = m.group(1).strip().rstrip(" ,.")
    dest = m.group(2).strip().rstrip(" ,.")
    mode_raw = (m.group(3) or "driving").strip().lower()
    valid = {"driving", "walking", "bicycling", "transit"}
    mode = mode_raw if mode_raw in valid else "driving"
    # Guard against overly long/garbage extractions
    if len(origin.split()) > 6 or len(dest.split()) > 6:
        return None
    return origin, dest, mode


def extract_nearest_query(text: str) -> str | None:
    match = _NEAREST_QUERY_RE.search(text)
    if not match:
        return None
    query = match.group(1).strip(" .,!?")
    query = re.sub(r"^(?:the|a|an)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\b(?:to\s+me|near\s+me|around\s+me|from\s+here)\b.*$", "", query, flags=re.IGNORECASE).strip(" .,!?")
    return query or None


def extract_place_lookup_query(text: str) -> str | None:
    for pattern in _PLACE_LOOKUP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        query = match.group(1).strip(" .,!?")
        query = re.sub(r"\b(?:nearby|near\s+me|around\s+me|from\s+here)\b.*$", "", query, flags=re.IGNORECASE).strip(" .,!?")
        if not query:
            return None
        lowered = query.lower()
        if lowered.startswith(("the nearest ", "nearest ", "the closest ", "closest ")):
            return None
        if lowered in {"it", "this", "that", "there"}:
            return None
        return query
    return None


def extract_destination_from_here(text: str) -> str | None:
    for pattern in _TO_DESTINATION_PATTERNS:
        match = pattern.search(text)
        if match:
            destination = match.group(1).strip(" .,!?")
            if not destination or destination.lower() in _NON_DESTINATIONS:
                return None
            return destination
    return None


def current_coords_text(current_location: dict[str, Any] | None) -> str | None:
    if not current_location:
        return None
    try:
        lat = float(current_location.get("lat"))
        lng = float(current_location.get("lng"))
    except Exception:
        return None
    return f"{lat},{lng}"


def wants_device_location(text: str) -> bool:
    return bool(_USE_DEVICE_LOCATION_RE.search(text or ""))


def looks_like_location_answer(text: str) -> bool:
    """Heuristic: is this short message plausibly the user answering 'where are
    you?' with a place/address, rather than a new question or command? Lenient
    — geocoding rejects genuine garbage, so we only bail on clear non-answers."""
    t = (text or "").strip()
    if not t or _CANCEL_RE.match(t):
        return False
    # A question is never the answer to one. This used the same first-word
    # anchor that caused the "I meant what other improvements..." misroute:
    # "actually, what did the kids have for lunch" hid its WH-word behind a
    # preamble, so an open route request geocoded THAT sentence — and, because
    # a failed geocode deliberately keeps the request pending, it then hijacked
    # the turn after that one too. core.intent sees through the preamble.
    if is_question(t):
        return False
    core = strip_preamble(t).lower()
    if re.match(r"^(can|could|would|please|make|build|create|remind|set|play|open the|show me the)\b", core):
        return False
    return len(core.split()) <= 12


def format_directions_reply(payload: dict[str, Any]) -> str:
    origin = str(payload.get("origin") or "the starting point").strip()
    destination = str(payload.get("destination") or "the destination").strip()
    distance = str(payload.get("distance") or "unknown distance").strip()
    duration = str(payload.get("duration") or "unknown travel time").strip()
    mode = str(payload.get("mode") or "driving").strip().lower()
    base = f"From {origin} to {destination}, it is about {distance} and takes around {duration} by {mode}."
    if payload.get("steps"):
        base += " I've got the full route on the map — want me to read the turn-by-turn, or scan the QR to send it to your phone?"
    return base


def maps_unavailable_reply(error: str | None) -> str:
    """Honest text for a maps call that could not RUN at all.

    Invariant #1 is that every capability reports real state. Telling Marcus
    "I couldn't find that place on the map — give me a full address" when the
    truth is "GOOGLE_MAPS_API_KEY isn't in .env" sends him round in circles
    retyping addresses that were never going to resolve. A call that ran and
    found nothing still gets the "couldn't find it" wording — that one is true.
    """
    err = str(error or "").strip()
    m = re.search(r"Missing required \.env key:\s*(\S+)", err)
    if m:
        return (f"Maps isn't set up on my end — {m.group(1)} isn't in my .env, so I can't look anything "
                "up on the map yet.")
    if not err:
        return "The map service didn't come back just now."
    return f"I couldn't reach the map service just now: {err[:160]}"


# ── The capability ───────────────────────────────────────────────────────────

class Navigation:
    """Maps, routes and nearby places, with its own short-lived session state.

    Per Marcus's choice, Nova never silently assumes his location for a
    nearby/route request — she asks (or takes an explicit "use my current
    location"), remembers the pending request, and completes it once he
    answers. State is a short-lived session fact so it survives the turn
    boundary without touching the conversation-state plumbing.
    """

    def __init__(self, *, router: ToolRouter, memory: Any) -> None:
        self._router = router
        self._memory = memory

    # ── Pending-request state ────────────────────────────────────────────

    async def _set_pending(self, data: dict[str, Any]) -> None:
        payload = {**data, "ts": _now().isoformat()}
        await self._memory.add_fact(
            entity="session", attribute="pending_map_request", value=json.dumps(payload), confidence=1.0
        )

    async def _get_pending(self) -> dict[str, Any] | None:
        f = await self._memory.get_latest_fact(entity="session", attribute="pending_map_request")
        if not f or not f.value:
            return None
        try:
            data = json.loads(f.value)
            ts = datetime.fromisoformat(str(data.get("ts", "")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        if (_now() - ts).total_seconds() > PENDING_TTL_S:
            return None
        return data

    async def _clear_pending(self) -> None:
        await self._memory.add_fact(
            entity="session", attribute="pending_map_request", value="", confidence=1.0
        )

    async def _save_last_route(self, route: dict[str, Any]) -> None:
        """Remember the last route's steps so Nova can read the turn-by-turn
        aloud on request (WS-D desktop narration)."""
        steps = [
            str(s.get("instruction") or "").strip()
            for s in (route.get("steps") or [])
            if str(s.get("instruction") or "").strip()
        ]
        if not steps:
            return
        payload = {"destination": route.get("destination"), "steps": steps[:25], "ts": _now().isoformat()}
        await self._memory.add_fact(entity="session", attribute="last_route", value=json.dumps(payload), confidence=1.0)

    async def _get_last_route(self) -> dict[str, Any] | None:
        f = await self._memory.get_latest_fact(entity="session", attribute="last_route")
        if not f or not f.value:
            return None
        try:
            data = json.loads(f.value)
            ts = datetime.fromisoformat(str(data.get("ts", "")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        if (_now() - ts).total_seconds() > LAST_ROUTE_TTL_S:
            return None
        return data

    # ── Tool dispatch ────────────────────────────────────────────────────

    async def run_nearby(self, query: str, lat: Any, lng: Any) -> Reply:
        call = ToolCall(name="maps.places_nearby", args={"query": query, "lat": lat, "lng": lng, "limit": 6})
        res = await self._router.execute(call, timeout_s=15.0, retries=0)
        tool_calls = [{"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result}]
        if res.ok and isinstance(res.result, dict) and res.result.get("places"):
            top = res.result["places"][0]
            name = str(top.get("name") or "that place").strip()
            address = str(top.get("address") or "").strip()
            dist = top.get("distance_meters")
            miles = f" ({round(float(dist) / 1609.344, 1)} mi away)" if isinstance(dist, (int, float)) else ""
            where = f" at {address}" if address else ""
            return (
                f"Closest {query}: {name}{where}{miles}. I put the options on the map — tap one and I'll route you there.",
                tool_calls, "smalltalk",
            )
        if not res.ok:
            return maps_unavailable_reply(res.error), tool_calls, "smalltalk"
        return f"I couldn't find any {query} near there right now.", tool_calls, "smalltalk"

    async def run_route(self, origin: str, destination: str, mode: str = "driving") -> Reply:
        call = ToolCall(name="maps.directions", args={"origin": origin, "destination": destination, "mode": mode})
        res = await self._router.execute(call, timeout_s=15.0, retries=0)
        tool_calls = [{"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result}]
        if res.ok and isinstance(res.result, dict) and res.result.get("status") == "OK":
            await self._save_last_route(res.result)
            return format_directions_reply(res.result), tool_calls, "smalltalk"
        if not res.ok:
            return maps_unavailable_reply(res.error), tool_calls, "smalltalk"
        return f"I couldn't pull directions to {destination} right now.", tool_calls, "smalltalk"

    async def _dispatch_pending(self, pending: dict[str, Any], *, lat: Any, lng: Any, coords_text: str) -> Reply | None:
        kind = pending.get("kind")
        if kind == "nearest":
            return await self.run_nearby(str(pending.get("query") or ""), lat, lng)
        if kind == "route":
            return await self.run_route(coords_text, str(pending.get("dest") or ""), str(pending.get("mode") or "driving"))
        return None

    async def _apply_pending(self, pending: dict[str, Any], text: str,
                             current_location: dict[str, Any] | None) -> Reply | None:
        # (a) explicit opt-in to device location
        if wants_device_location(text):
            if current_location is None:
                return ("I don't have a device location right now — what's your address or city?", [], "smalltalk")
            await self._clear_pending()
            return await self._dispatch_pending(
                pending, lat=current_location.get("lat"), lng=current_location.get("lng"),
                coords_text=current_coords_text(current_location) or "",
            )
        # (b) not a location answer -> abandon the pending request, let normal handling take over
        if not looks_like_location_answer(text):
            await self._clear_pending()
            return None
        # (c) treat the message as a stated location -> geocode it
        geo = await self._router.execute(ToolCall(name="maps.geocode", args={"address": text}), timeout_s=15.0, retries=0)
        # The geocode used to be dropped from the returned tool_calls, so a
        # failure here was invisible to the UI, the event log and the caller.
        tool_calls = [{"tool": "maps.geocode", "ok": geo.ok, "error": geo.error, "result": geo.result}]
        if not geo.ok:
            # The lookup never ran. Keep the request pending (it may just be a
            # missing key he can fix) but say what actually went wrong.
            return maps_unavailable_reply(geo.error), tool_calls, "smalltalk"
        loc = (geo.result or {}).get("location") if isinstance(geo.result, dict) else None
        if not loc or loc.get("lat") is None or loc.get("lng") is None:
            return (f"I couldn't find '{text}' on the map — can you give me a city or a full address?",
                    tool_calls, "smalltalk")
        await self._clear_pending()
        return await self._dispatch_pending(
            pending, lat=loc["lat"], lng=loc["lng"], coords_text=f"{loc['lat']},{loc['lng']}"
        )

    # ── Entry points (order matters — see the module docstring) ──────────

    async def resolve_pending(self, text: str, *, current_location: dict[str, Any] | None = None) -> Reply | None:
        """Runs BEFORE the identity/clock/weather pre-passes.

        Covers the two cases where an earlier navigation turn owns this
        message: an unanswered "where are you starting from?", and "read me
        the turn-by-turn" for the route just given.
        """
        text = (text or "").strip()
        if not text:
            return None

        pending = await self._get_pending()
        if pending is not None:
            resolved = await self._apply_pending(pending, text, current_location)
            if resolved is not None:
                return resolved

        # "Read me the turn-by-turn" for the most recent route (WS-D narration).
        if READ_STEPS_RE.search(text):
            last = await self._get_last_route()
            if last and last.get("steps"):
                steps = last["steps"]
                dest = str(last.get("destination") or "your destination").strip()
                spoken = " ".join(f"Step {i + 1}: {s}." for i, s in enumerate(steps[:12]))
                more = f" That's the first 12 of {len(steps)} steps." if len(steps) > 12 else ""
                return f"Here's the route to {dest}. {spoken}{more}", [], "smalltalk"

        return None

    async def handle(self, text: str, *, current_location: dict[str, Any] | None = None) -> Reply | None:
        """Runs AFTER the identity/clock/weather pre-passes.

        Returns None when the message isn't a navigation request at all — the
        common case, and the one the misroute got wrong.
        """
        text = (text or "").strip()
        if not text:
            return None
        coords_text = current_coords_text(current_location)

        nearest_query = extract_nearest_query(text)
        if nearest_query is not None:
            # Always ask where to search from (never silently assume the device
            # location) unless Marcus explicitly opts into it.
            if wants_device_location(text) and current_location is not None:
                return await self.run_nearby(nearest_query, current_location.get("lat"), current_location.get("lng"))
            await self._set_pending({"kind": "nearest", "query": nearest_query})
            return (
                f"Sure — where should I look for the nearest {nearest_query}? Give me a city or address, "
                "or say 'use my current location'.",
                [], "smalltalk",
            )

        place_query = extract_place_lookup_query(text)
        if place_query is not None:
            tool_calls: list[dict[str, Any]] = []

            place_call = ToolCall(name="maps.place_search", args={"query": place_query, "limit": 6})
            place_res = await self._router.execute(place_call, timeout_s=15.0, retries=0)
            tool_calls.append({"tool": place_call.name, "ok": place_res.ok, "error": place_res.error, "result": place_res.result})
            if place_res.ok and isinstance(place_res.result, dict) and place_res.result.get("places"):
                top = place_res.result["places"][0]
                name = str(top.get("name") or place_query).strip()
                address = str(top.get("address") or "").strip()
                if address:
                    return f"I found {name} at {address}. I opened it on the map.", tool_calls, "smalltalk"
                return f"I found {name}. I opened it on the map.", tool_calls, "smalltalk"

            geocode_call = ToolCall(name="maps.geocode", args={"address": place_query})
            geocode_res = await self._router.execute(geocode_call, timeout_s=15.0, retries=0)
            tool_calls.append({"tool": geocode_call.name, "ok": geocode_res.ok, "error": geocode_res.error, "result": geocode_res.result})
            if geocode_res.ok and isinstance(geocode_res.result, dict) and geocode_res.result.get("formatted_address"):
                address = str(geocode_res.result.get("formatted_address") or place_query).strip()
                return f"I found {place_query} at {address}. I opened it on the map.", tool_calls, "smalltalk"

            if not place_res.ok and not geocode_res.ok:
                # Neither lookup ran — that's a configuration/service state, not
                # "this place doesn't exist".
                return maps_unavailable_reply(place_res.error or geocode_res.error), tool_calls, "smalltalk"
            return f"I could not find a map result for {place_query} right now.", tool_calls, "smalltalk"

        local_destination = extract_destination_from_here(text)
        if local_destination is not None or (FROM_HERE_RE.search(text) and " to " in text.lower()):
            destination = local_destination or text
            # Always ask where he's starting from (never silently assume the
            # device location) unless he explicitly opts into it.
            if wants_device_location(text) and current_location is not None and coords_text is not None:
                return await self.run_route(coords_text, destination)
            await self._set_pending({"kind": "route", "dest": destination, "mode": "driving"})
            return (
                f"Happy to route you to {destination} — where are you starting from? A city or address, "
                "or say 'use my current location'.",
                [], "smalltalk",
            )

        dirs = extract_directions(text)
        if dirs:
            origin, destination, mode = dirs
            return await self.run_route(origin, destination, mode)

        return None
