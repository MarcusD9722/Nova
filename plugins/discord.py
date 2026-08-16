from __future__ import annotations

import os

import httpx

from plugins.registry import PluginConfigError, tool


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise PluginConfigError(f"Missing required .env key: {key}")
    return val


@tool(name="discord.send",
      description=("Send a message through MARCUS'S configured Discord bot, to his channel. "
                   "args: {content, channel_id?}"),
      data_scope="owner_private")
async def send_message(args: dict) -> dict:
    token = _require("DISCORD_BOT_TOKEN")
    default_channel = os.getenv("DISCORD_CHANNEL_ID", "").strip()

    channel_id = str(args.get("channel_id") or default_channel).strip()
    content = str(args.get("content") or "").strip()
    if not channel_id:
        raise PluginConfigError("Missing DISCORD_CHANNEL_ID (or pass channel_id in args)")
    if not content:
        raise ValueError("discord.send requires 'content'")

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json={"content": content},
        )
        r.raise_for_status()
        data = r.json()

    return {"id": data.get("id"), "channel_id": channel_id, "content": data.get("content")}


@tool(name="discord.read",
      description=("Read recent messages from MARCUS'S configured Discord channel. "
                   "args: {limit?, channel_id?}"),
      data_scope="owner_private")
async def read_messages(args: dict) -> dict:
    token = _require("DISCORD_BOT_TOKEN")
    default_channel = os.getenv("DISCORD_CHANNEL_ID", "").strip()

    channel_id = str(args.get("channel_id") or default_channel).strip()
    if not channel_id:
        raise PluginConfigError("Missing DISCORD_CHANNEL_ID (or pass channel_id in args)")

    limit = max(1, min(int(args.get("limit") or 10), 50))

    headers = {"Authorization": f"Bot {token}"}
    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": limit},
        )
        r.raise_for_status()
        data = r.json()

    messages = [
        {
            "id": m.get("id"),
            "author": ((m.get("author") or {}).get("global_name") or (m.get("author") or {}).get("username")),
            "content": m.get("content"),
            "timestamp": m.get("timestamp"),
        }
        for m in (data if isinstance(data, list) else [])
    ]
    return {"channel_id": channel_id, "count": len(messages), "messages": messages}
