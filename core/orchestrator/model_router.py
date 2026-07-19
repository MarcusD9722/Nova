from __future__ import annotations

"""ModelRouter — role → model-instance mapping (Phase 2.4 of docs/ROADMAP.md).

Every cognitive job in Nova (chat reply, tool decisions, planning, criticism,
coding, small utility calls) is a *role*. A role resolves to a ModelHandle:
an LLMRuntime paired with the asyncio.Semaphore that serializes access to the
physical GPU that runtime lives on.

Why the semaphore lives here: on one GPU, all LLM calls must serialize, so
today every role shares ONE handle with ONE semaphore. When a second model
arrives (RTX 3080 timeshare, or a Coder-14B swap), it registers as a second
handle with its OWN semaphore — and any role remapped to it immediately runs
concurrently with the primary model, no caller changes. Scaling up is a
config edit (NOVA_MODEL_ROLES), not a rewrite. That property is the whole
point of this indirection existing before the hardware does.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.logging_setup import get_logger

if TYPE_CHECKING:
    from core.llm_runtime import LLMRuntime

logger = get_logger(__name__)

# The canonical roles. Callers use these names; typos are caught in tests.
ROLES = ("chat", "decider", "planner", "critic", "coder", "utility")


@dataclass(frozen=True)
class ModelHandle:
    """A model instance plus the semaphore guarding its GPU."""
    name: str                    # config key for this model, e.g. "primary"
    runtime: "LLMRuntime"
    semaphore: asyncio.Semaphore


class ModelRouter:
    def __init__(self, handles: dict[str, ModelHandle], default: str, role_map: dict[str, str] | None = None) -> None:
        if default not in handles:
            raise ValueError(f"default handle {default!r} not among {sorted(handles)}")
        self._handles = dict(handles)
        self._default = default
        # role -> handle-name; unknown handles are dropped with a warning so a
        # stale config never silently routes a role into the void.
        self._role_map: dict[str, str] = {}
        for role, handle_name in (role_map or {}).items():
            if handle_name in self._handles:
                self._role_map[role] = handle_name
            else:
                logger.warning("model_role_unknown_handle", role=role, handle=handle_name,
                               note=f"{handle_name!r} is not a registered model; role {role!r} stays on default.")

    @classmethod
    def single(cls, runtime: "LLMRuntime", semaphore: asyncio.Semaphore,
               *, name: str = "primary", role_map: dict[str, str] | None = None) -> "ModelRouter":
        """The current reality: one model, one GPU, every role on it."""
        handle = ModelHandle(name=name, runtime=runtime, semaphore=semaphore)
        return cls({name: handle}, default=name, role_map=role_map)

    def for_role(self, role: str) -> ModelHandle:
        """The model+semaphore a role should use (default if unmapped)."""
        return self._handles[self._role_map.get(role, self._default)]

    def describe(self) -> dict[str, str]:
        """role -> model-name, for /status and operator visibility."""
        return {role: self._role_map.get(role, self._default) for role in ROLES}

    @property
    def handles(self) -> dict[str, ModelHandle]:
        return dict(self._handles)


def parse_role_map(raw: str) -> dict[str, str]:
    """Parse NOVA_MODEL_ROLES ("coder=secondary,planner=secondary") into a
    role->handle dict. Unknown role names are ignored (validated in tests);
    malformed entries are skipped rather than crashing boot."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        role, handle = (s.strip() for s in part.split("=", 1))
        if role in ROLES and handle:
            out[role] = handle
    return out
