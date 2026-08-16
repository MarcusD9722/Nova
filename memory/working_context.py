from __future__ import annotations

"""Working context — Nova's model of what is happening *right now*.

Long-term memory answers "what is true about Marcus". This answers "what are we
doing", and the two need completely different machinery. Long-term recall is a
ranked search over everything ever learned; working context is a handful of
slots that must be readable in microseconds because they are consulted on every
turn, including the ones that must feel instant.

So this is emphatically **not** another vector store. It is a small bounded
record per conversation, updated as turns happen, read by deterministic lookup.

What it holds is what a person would hold: what we are talking about, what we
just did, what is on the screen, and what question is still open.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_TURN_MEMORY = 8
_TOOL_MEMORY = 6


@dataclass
class ToolTrace:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    ok: bool = True
    at: float = field(default_factory=time.time)


@dataclass
class WorkingContext:
    conversation_id: str
    started_at: float = field(default_factory=time.time)
    active_topic: str = ""
    active_project: str = ""
    open_question: str = ""
    current_selection_id: str | None = None
    current_result_set_id: str | None = None
    user_turns: deque[str] = field(default_factory=lambda: deque(maxlen=_TURN_MEMORY))
    assistant_turns: deque[str] = field(default_factory=lambda: deque(maxlen=_TURN_MEMORY))
    tools: deque[ToolTrace] = field(default_factory=lambda: deque(maxlen=_TOOL_MEMORY))
    updated_at: float = field(default_factory=time.time)

    # -- writes ---------------------------------------------------------------

    def record_user(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.user_turns.append(text)
            self.updated_at = time.time()

    def record_assistant(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.assistant_turns.append(text)
            self.updated_at = time.time()

    def record_tool(self, tool: str, args: dict[str, Any] | None = None,
                    summary: str = "", ok: bool = True) -> None:
        self.tools.append(ToolTrace(tool=tool, args=dict(args or {}), summary=summary, ok=ok))
        self.updated_at = time.time()

    def set_result_set(self, artifact_id: str | None) -> None:
        self.current_result_set_id = artifact_id
        self.current_selection_id = None
        self.updated_at = time.time()

    def select(self, artifact_id: str | None) -> None:
        self.current_selection_id = artifact_id
        self.updated_at = time.time()

    # -- reads ----------------------------------------------------------------

    def last_tool(self) -> ToolTrace | None:
        return self.tools[-1] if self.tools else None

    def tool_named(self, name: str) -> ToolTrace | None:
        for trace in reversed(self.tools):
            if trace.tool == name:
                return trace
        return None

    def recent_text(self, turns: int = 4) -> str:
        """The last few exchanges as one blob — the recall gate reads this."""
        users = list(self.user_turns)[-turns:]
        assistants = list(self.assistant_turns)[-turns:]
        merged: list[str] = []
        for i in range(max(len(users), len(assistants))):
            if i < len(users):
                merged.append(users[i])
            if i < len(assistants):
                merged.append(assistants[i])
        return "\n".join(merged)

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "active_topic": self.active_topic,
            "active_project": self.active_project,
            "open_question": self.open_question,
            "has_result_set": self.current_result_set_id is not None,
            "selection": self.current_selection_id,
            "last_tool": (self.last_tool().tool if self.last_tool() else None),
            "user_turns": len(self.user_turns),
            "age_s": round(time.time() - self.started_at, 1),
        }

    def describe_for_prompt(self) -> str:
        """One short block. Deliberately terse — this competes for prompt space
        with things the model needs more."""
        bits: list[str] = []
        if self.active_topic:
            bits.append(f"Currently discussing: {self.active_topic}")
        if self.active_project:
            bits.append(f"Active project: {self.active_project}")
        if self.open_question:
            bits.append(f"Open question: {self.open_question}")
        trace = self.last_tool()
        if trace is not None and trace.summary:
            bits.append(f"Last tool ({trace.tool}): {trace.summary[:160]}")
        return "\n".join(bits)


class WorkingContextStore:
    """One WorkingContext per conversation, bounded."""

    def __init__(self, max_conversations: int = 32) -> None:
        self._contexts: dict[str, WorkingContext] = {}
        self._order: deque[str] = deque()
        self._max = max_conversations

    @staticmethod
    def _scoped(conversation_id: str) -> str:
        """Partition per speaker (V3 P5.1 final closure).

        Recent turns, tool traces, the active topic/project, the open question,
        the result-set pointer and the current selection are all conversation-
        local — and every one of them is somebody's. The owner's key is
        unchanged.
        """
        try:
            from core.turn_identity import scoped_conversation_key
        except Exception:  # noqa: BLE001
            return str(conversation_id)
        return scoped_conversation_key(conversation_id)

    def get(self, conversation_id: str) -> WorkingContext:
        conversation_id = self._scoped(conversation_id)
        key = str(conversation_id)
        ctx = self._contexts.get(key)
        if ctx is None:
            ctx = WorkingContext(conversation_id=key)
            self._contexts[key] = ctx
            self._order.append(key)
            while len(self._order) > self._max:
                self._contexts.pop(self._order.popleft(), None)
        return ctx

    def peek(self, conversation_id: str) -> WorkingContext | None:
        conversation_id = self._scoped(conversation_id)
        return self._contexts.get(str(conversation_id))

    def drop(self, conversation_id: str) -> None:
        conversation_id = self._scoped(conversation_id)
        key = str(conversation_id)
        self._contexts.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass

    def stats(self) -> dict[str, int]:
        return {"conversations": len(self._contexts)}
