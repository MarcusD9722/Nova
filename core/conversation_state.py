from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.logging_setup import get_logger
from memory.backends.diskcache_backend import DiskCacheBackend


logger = get_logger(__name__)


def _norm_q(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" \t\n\r\f\v\"'“”‘’")
    return s


@dataclass
class ConversationState:
    last_user_messages: list[str]
    last_assistant_replies: list[str]
    last_follow_up_questions: list[str]
    last_mode: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "last_user_messages": self.last_user_messages,
                "last_assistant_replies": self.last_assistant_replies,
                "last_follow_up_questions": self.last_follow_up_questions,
                "last_mode": self.last_mode,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_obj(obj: Any) -> "ConversationState":
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                obj = {}
        if not isinstance(obj, dict):
            obj = {}
        return ConversationState(
            last_user_messages=[str(x) for x in (obj.get("last_user_messages") or [])][-50:],
            last_assistant_replies=[str(x) for x in (obj.get("last_assistant_replies") or [])][-50:],
            last_follow_up_questions=[str(x) for x in (obj.get("last_follow_up_questions") or [])][-50:],
            last_mode=(str(obj.get("last_mode")) if obj.get("last_mode") is not None else None),
        )


class ConversationStateStore:
    """Tracks per-conversation recent turns & follow-ups.

    Persisted via DiskCache (thread-safe via asyncio.to_thread in DiskCacheBackend).
    """

    def __init__(self, cache: DiskCacheBackend, *, max_turns: int = 12, followup_window: int = 8) -> None:
        self._cache = cache
        self._max_turns = int(max_turns)
        self._followup_window = int(followup_window)
        # record_turn is read-modify-write. Unguarded, two overlapping turns on
        # the same conversation both read the same state and the second write
        # wins — the first turn vanishes from "Recent messages:" with no error
        # anywhere. Measured: 10 concurrent record_turn calls kept 1. Only one
        # production path writes today (chat_turn_stream._finish), but /chat and
        # /chat/stream are separate endpoints, so a double-send or an overlapping
        # voice+text turn reaches it. One lock is enough — writes are rare and
        # sub-millisecond, so contention is irrelevant.
        self._write_lock = asyncio.Lock()

    def _key(self, conversation_id: UUID) -> str:
        return f"conv_state:{conversation_id}"

    async def load(self, conversation_id: UUID) -> ConversationState:
        raw = await self._cache.get(self._key(conversation_id))
        return ConversationState.from_obj(raw)

    async def was_followup_recent(self, *, conversation_id: UUID, question: str) -> bool:
        qn = _norm_q(question)
        if not qn:
            return False
        st = await self.load(conversation_id)
        recent = [_norm_q(q) for q in st.last_follow_up_questions[-self._followup_window :]]
        return qn in recent

    async def record_turn(
        self,
        *,
        conversation_id: UUID,
        user_message: str,
        assistant_reply: str,
        follow_up_question: str | None,
        mode: str | None,
    ) -> None:
        async with self._write_lock:
            st = await self.load(conversation_id)

            st.last_user_messages.append((user_message or "").strip())
            st.last_assistant_replies.append((assistant_reply or "").strip())
            if follow_up_question:
                st.last_follow_up_questions.append((follow_up_question or "").strip())
            st.last_mode = mode

            st.last_user_messages = st.last_user_messages[-self._max_turns :]
            st.last_assistant_replies = st.last_assistant_replies[-self._max_turns :]
            st.last_follow_up_questions = st.last_follow_up_questions[-max(self._max_turns, self._followup_window) :]

            await self._cache.set(self._key(conversation_id), json.loads(st.to_json()), ttl_s=7 * 24 * 3600)

    async def recent_chat_text(self, conversation_id: UUID) -> str:
        st = await self.load(conversation_id)
        lines: list[str] = []
        # Interleave approximate last turns
        n = max(len(st.last_user_messages), len(st.last_assistant_replies))
        for i in range(max(0, n - self._max_turns), n):
            if i < len(st.last_user_messages):
                um = st.last_user_messages[i]
                if um:
                    lines.append(f"User: {um}")
            if i < len(st.last_assistant_replies):
                am = st.last_assistant_replies[i]
                if am:
                    lines.append(f"Assistant: {am}")
        return "\n".join(lines[-2 * self._max_turns :]).strip()
