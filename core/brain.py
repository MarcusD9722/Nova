from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.logging_setup import get_logger
from core.llm_runtime import LLMRuntime
from core.runtime import RuntimeManager
from core.tooling import build_tool_router
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


def _sanitize_user_text(text: str) -> str:
    """Remove known UI artifacts and collapse whitespace."""
    if not text:
        return ""
    t = text.strip()

    # Strip common test tags
    t = re.sub(r"#e2e\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"#hello\b", "", t, flags=re.IGNORECASE)

    # Remove UI/editor artifacts
    t = re.sub(r"\blast edited by\s+Nova\b", "", t, flags=re.IGNORECASE)

    # If the user pasted a transcript, keep only the last user line.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if any(ln.lower().startswith("assistant:") or ln.lower().startswith("user:") for ln in lines):
        last_user = ""
        for ln in lines:
            if ln.lower().startswith("user:"):
                last_user = ln.split(":", 1)[1].strip()
        if last_user:
            t = last_user

    t = re.sub(r"\s+", " ", t).strip()
    return t


@dataclass
class ChatResponse:
    conversation_id: UUID
    assistant_text: str
    tool_calls: list[dict[str, Any]]


class Brain:
    """Thin orchestrator.

    All heavy work (LLM policy, memory ingest, autonomy supervision) lives in `core/runtime.py`.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        projects_dir: Path,
        memory: MemoryUnifier,
        llm: LLMRuntime,
        runtime: RuntimeManager | None = None,
        memory_dir: Path | None = None,
    ) -> None:
        self._memory = memory
        self._llm = llm

        if runtime is not None:
            self._runtime = runtime
        else:
            router = build_tool_router(repo_root=repo_root, projects_dir=projects_dir, memory=memory)
            md = memory_dir or (repo_root / "memory_data")
            self._runtime = RuntimeManager(
                repo_root=repo_root,
                projects_dir=projects_dir,
                memory=memory,
                llm=llm,
                router=router,
                memory_dir=md,
            )

    @property
    def runtime(self) -> RuntimeManager:
        return self._runtime

    def start(self) -> None:
        self._runtime.start()

    async def stop(self) -> None:
        await self._runtime.stop()

    async def chat(
        self,
        message: str,
        conversation_id: UUID | None = None,
        current_location: dict[str, Any] | None = None,
    ) -> ChatResponse:
        conv_id = conversation_id or uuid4()
        clean_message = _sanitize_user_text(message)

        res = await self._runtime.chat_turn(
            user_text=clean_message,
            conversation_id=conv_id,
            user_name=None,
            project_name="temp",
            current_location=current_location,
        )
        return ChatResponse(conversation_id=res.conversation_id, assistant_text=res.assistant_text, tool_calls=res.tool_calls)

    async def chat_stream(
        self,
        message: str,
        conversation_id: UUID | None = None,
        current_location: dict[str, Any] | None = None,
    ):
        """Streaming chat: yields {"type": "token"|"done", ...} dicts.

        Uses the function-calling pipeline in RuntimeManager.chat_turn_stream.
        """
        conv_id = conversation_id or uuid4()
        clean_message = _sanitize_user_text(message)

        async for event in self._runtime.chat_turn_stream(
            user_text=clean_message,
            conversation_id=conv_id,
            user_name=None,
            project_name="temp",
            current_location=current_location,
        ):
            yield event
