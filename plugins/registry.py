from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class PluginConfigError(RuntimeError):
    pass


AsyncToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: AsyncToolFn


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = asyncio.Lock()

    async def register(self, spec: ToolSpec) -> None:
        async with self._lock:
            if spec.name in self._tools:
                raise RuntimeError(f"Tool already registered: {spec.name}")
            self._tools[spec.name] = spec

    def register_sync(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise RuntimeError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get_tools(self) -> dict[str, ToolSpec]:
        return dict(self._tools)


REGISTRY = ToolRegistry()


def tool(name: str, description: str):
    def deco(fn: AsyncToolFn) -> AsyncToolFn:
        REGISTRY.register_sync(ToolSpec(name=name, description=description, fn=fn))
        return fn

    return deco
