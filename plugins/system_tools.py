from __future__ import annotations

from datetime import datetime, timezone

from plugins.registry import tool


@tool(
    name="system.time",
    description=(
        "Get the current local date and time from the system clock. "
        "Use this whenever the user asks what time or date it is."
    ),
)
async def system_time(args: dict) -> dict:
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    return {
        "datetime_local": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now_local.strftime("%A, %B %d, %Y"),
        "time": now_local.strftime("%I:%M %p"),
        "timezone": str(now_local.tzinfo),
        "datetime_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "unix_timestamp": int(now_utc.timestamp()),
    }
