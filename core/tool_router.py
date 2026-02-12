from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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
    def __init__(self, tools: dict[str, AsyncTool]):
        self._tools = dict(tools)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    async def execute(self, call: ToolCall, timeout_s: float = 20.0, retries: int = 1) -> ToolResult:
        if call.name not in self._tools:
            return ToolResult(name=call.name, ok=False, result=None, error=f"Unknown tool: {call.name}")

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                coro = self._tools[call.name](call.args)
                result = await asyncio.wait_for(coro, timeout=timeout_s)
                return ToolResult(name=call.name, ok=True, result=result, error=None)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("tool_failed", tool=call.name, attempt=attempt, error=str(e))
                await asyncio.sleep(0.2 * (attempt + 1))

        return ToolResult(name=call.name, ok=False, result=None, error=str(last_err) if last_err else "tool_failed")
