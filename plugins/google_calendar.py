from __future__ import annotations

"""Read-only Google Calendar awareness (PI1).

Direct REST calls via httpx (matching every other plugin's style) using a
bearer token from plugins/_google_oauth.py. Read-only scope only — Nova
never creates, edits, or deletes calendar events.
"""

from datetime import datetime, timedelta, timezone

import httpx

from plugins._google_oauth import get_access_token
from plugins.registry import tool

_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _fmt_event(item: dict) -> dict:
    start = item.get("start") or {}
    when = start.get("dateTime") or start.get("date") or ""
    return {
        "summary": str(item.get("summary") or "(no title)"),
        "when": when,
        "all_day": "date" in start and "dateTime" not in start,
        "location": item.get("location"),
    }


async def _list_events(*, time_min: datetime, time_max: datetime, max_results: int) -> list[dict]:
    token = await get_access_token()
    params = {
        "timeMin": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(max_results),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        r = await client.get(_EVENTS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()
    return [_fmt_event(item) for item in (data.get("items") or [])]


@tool(name="calendar.today",
      description=("Get today's events from MARCUS'S connected primary Google Calendar "
                   "(read-only). args: {}"),
      data_scope="owner_private")
async def calendar_today(args: dict) -> dict:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events = await _list_events(time_min=start, time_max=end, max_results=20)
    return {"date": start.strftime("%Y-%m-%d"), "events": events, "count": len(events)}


@tool(name="calendar.upcoming",
      description=("Get upcoming events over the next N days from MARCUS'S connected primary "
                   "Google Calendar (read-only). args: {days?}"),
      data_scope="owner_private")
async def calendar_upcoming(args: dict) -> dict:
    days = max(1, min(int(args.get("days") or 7), 30))
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    events = await _list_events(time_min=start, time_max=end, max_results=50)
    return {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"), "events": events, "count": len(events)}
