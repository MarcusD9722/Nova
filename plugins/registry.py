from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class PluginConfigError(RuntimeError):
    pass


AsyncToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


#: Whose data a plugin tool reaches (V3 P5.1d.4).
#:
#:   "shared"        public or system information. An API key pays for the
#:                   service; it does not make the answer Marcus-private.
#:                   Weather, web search, maps, the system clock.
#:   "owner_private" a connected account or channel belonging to Marcus's Nova
#:                   installation, with no per-speaker account mapping. Gmail,
#:                   his primary Calendar, the configured Discord bot.
#:
#: This is DATA ROUTING, not authorisation. It never reaches PermissionBroker
#: and never turns a recognised voice into an admin.
DATA_SCOPES: frozenset[str] = frozenset({"shared", "owner_private"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: AsyncToolFn
    #: Required. There is deliberately no default: a plugin added later must be
    #: triaged, because the failure mode being closed here is a new integration
    #: silently inheriting public access to somebody's private account.
    data_scope: str = ""


def _validate(spec: ToolSpec) -> None:
    if spec.data_scope not in DATA_SCOPES:
        raise ValueError(
            f"Plugin tool {spec.name!r} must declare data_scope="
            f"{' | '.join(sorted(DATA_SCOPES))}. Decide whose data it reaches: "
            f"a connected account belonging to Marcus is 'owner_private'; public "
            f"or system information is 'shared'."
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = asyncio.Lock()

    async def register(self, spec: ToolSpec) -> None:
        _validate(spec)
        async with self._lock:
            if spec.name in self._tools:
                raise RuntimeError(f"Tool already registered: {spec.name}")
            self._tools[spec.name] = spec

    def register_sync(self, spec: ToolSpec) -> None:
        _validate(spec)
        if spec.name in self._tools:
            raise RuntimeError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def owner_private_names(self) -> set[str]:
        return {n for n, s in self._tools.items() if s.data_scope == "owner_private"}

    def get_tools(self) -> dict[str, ToolSpec]:
        return dict(self._tools)


REGISTRY = ToolRegistry()


def tool(name: str, description: str, *, data_scope: str):
    """Register a plugin tool. `data_scope` is keyword-only and REQUIRED.

    Omitting it is a TypeError at import time rather than a quiet default —
    the whole point is that a new integration cannot inherit public access to
    somebody's private account by saying nothing.
    """
    def deco(fn: AsyncToolFn) -> AsyncToolFn:
        REGISTRY.register_sync(ToolSpec(name=name, description=description, fn=fn,
                                        data_scope=data_scope))
        return fn

    return deco
