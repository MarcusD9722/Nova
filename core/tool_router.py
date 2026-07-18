from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.event_bus import BUS, clip
from core.logging_setup import get_logger


logger = get_logger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    name: str
    ok: bool
    result: Any | None
    error: str | None


AsyncTool = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolRouter:
    def __init__(self, tools: dict[str, AsyncTool], descriptions: dict[str, str] | None = None):
        self._tools = dict(tools)
        self._descriptions = dict(descriptions or {})

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def describe_tools(self) -> dict[str, str]:
        """name -> human/LLM-readable description (used for function calling)."""
        return {name: self._descriptions.get(name, "") for name in self.list_tools()}

    def register(self, name: str, fn: AsyncTool, description: str = "") -> None:
        """Register an additional tool after construction (e.g. project builder)."""
        self._tools[str(name)] = fn
        if description:
            self._descriptions[str(name)] = description

    async def execute(self, call: ToolCall, timeout_s: float = 20.0, retries: int = 1) -> ToolResult:
        if call.name not in self._tools:
            BUS.publish("tool.error", {"tool": call.name, "error": "unknown_tool"})
            return ToolResult(name=call.name, ok=False, result=None, error=f"Unknown tool: {call.name}")

        BUS.publish("tool.started", {"tool": call.name})
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                coro = self._tools[call.name](call.args)
                result = await asyncio.wait_for(coro, timeout=timeout_s)
                BUS.publish("tool.result", {"tool": call.name, "ok": True, "summary": clip(result, 200)})
                return ToolResult(name=call.name, ok=True, result=result, error=None)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("tool_failed", tool=call.name, attempt=attempt, error=str(e))
                await asyncio.sleep(0.2 * (attempt + 1))

        BUS.publish("tool.error", {"tool": call.name, "error": clip(last_err, 200) if last_err else "tool_failed"})
        return ToolResult(name=call.name, ok=False, result=None, error=str(last_err) if last_err else "tool_failed")
