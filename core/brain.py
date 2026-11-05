from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.file_extract import ATTACHMENT_HEADER
from core.logging_setup import get_logger
from core.llm_runtime import LLMRuntime
from core.runtime import RuntimeManager
from core.tooling import build_tool_router
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


#: A pasted transcript, as opposed to a line that merely begins with a word and
#: a colon. `user: admin` in a docker-compose file is not a transcript, and
#: treating it as one replaced the whole message with the word "admin"
#: (audit 2026-11-02, H1).
_ROLE_LINE_RE = re.compile(r"^(user|assistant|nova|marcus)\s*:\s*(.*)$", re.IGNORECASE)


def _sanitize_prose(text: str) -> str:
    """Clean what the USER typed. Never touches attachment contents.

    Two things this deliberately no longer does:

    * **It does not flatten whitespace.** `re.sub(r"\\s+", " ", t)` put pasted
      code and every attached file on one line, indentation and all, before the
      model ever saw it. Runs of spaces and blank lines are tidied; newlines
      survive.
    * **It does not trim on a single role-prefixed line.** A transcript needs at
      least two of them, which is what a real paste looks like, so one stray
      `user:` can no longer delete the question around it.
    """
    t = (text or "").strip()
    if not t:
        return ""

    # Strip common test tags
    t = re.sub(r"#e2e\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"#hello\b", "", t, flags=re.IGNORECASE)

    # Remove UI/editor artifacts
    t = re.sub(r"\blast edited by\s+Nova\b", "", t, flags=re.IGNORECASE)

    # A pasted conversation: keep the last thing the user said in it.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    role_lines = [ln for ln in lines if _ROLE_LINE_RE.match(ln)]
    if len(role_lines) >= 2:
        last_user = ""
        for ln in lines:
            m = _ROLE_LINE_RE.match(ln)
            if m and m.group(1).lower() in ("user", "marcus"):
                last_user = m.group(2).strip()
        if last_user:
            t = last_user

    # Tidy, don't flatten: horizontal runs collapse, line structure stays.
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _sanitize_user_text(text: str) -> str:
    """Clean the user's own words and leave their files alone.

    The attachment block is passed through verbatim. It is DATA — a file the
    user asked about — and rewriting it changed what Nova was asked to look at:
    a compose file containing `user: admin` arrived at the model as the single
    word "admin", with the question gone, and the request still returned 200.
    """
    if not text:
        return ""
    marker = "\n\n" + ATTACHMENT_HEADER
    head, sep, tail = text.partition(marker)
    if not sep:
        return _sanitize_prose(text)
    return (_sanitize_prose(head) + sep + tail).strip()


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
        identity: Any = None,
    ) -> ChatResponse:
        conv_id = conversation_id or uuid4()
        clean_message = _sanitize_user_text(message)

        res = await self._runtime.chat_turn(
            user_text=clean_message,
            conversation_id=conv_id,
            user_name=None,
            project_name="temp",
            current_location=current_location,
            identity=identity,
        )
        return ChatResponse(conversation_id=res.conversation_id, assistant_text=res.assistant_text, tool_calls=res.tool_calls)

    async def chat_stream(
        self,
        message: str,
        conversation_id: UUID | None = None,
        current_location: dict[str, Any] | None = None,
        identity: Any = None,
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
            identity=identity,
        ):
            yield event
