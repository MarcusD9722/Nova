from __future__ import annotations

import re
from uuid import UUID

from core.conversation_state import ConversationStateStore
from core.policy.contracts import ConversationPolicyOutput


_BOILERPLATE_RE = re.compile(
    r"(?is)\b("
    r"how\s+can\s+i\s+(?:help|assist)\b[^\n\.\!\?]*[\.!\?]?|"
    r"i\s*(?:am|'m)\s+(?:just\s+)?here\s+to\s+(?:help|assist)[^\n\.\!\?]*[\.!\?]?|"
    r"i\s+can\s+help\s+with\s+any\s+(?:tasks?|questions?)[^\n\.\!\?]*[\.!\?]?|"
    r"is\s+there\s+anything\s+else\s+i\s+can\s+(?:help|assist)[^\n\.\!\?]*[\.!\?]?|"
    r"if\s+you\s+have\s+more\s+questions?[^\n\.\!\?]*[\.!\?]?|"
    r"if\s+you\s+need\s+further\s+clarification[^\n\.\!\?]*[\.!\?]?|"
    r"feel\s+free\s+to\s+ask[^\n\.\!\?]*[\.!\?]?|"
    r"let\s+me\s+know\s+if\s+you\s+need\s+any\s+help[^\n\.\!\?]*[\.!\?]?|"
    r"i\s+can\s+help\s+with\s+other\s+tasks[^\n\.\!\?]*[\.!\?]?"
    r")\s*",
)

_GREET_ECHO_RE = re.compile(r"^(\s*(hey|hi|hello)\s+nova\b[\s\!\.?]*)", re.IGNORECASE)
_CHATBOT_DENY_RE = re.compile(r"(?is)\b(i\s*(?:don't|do\s+not)\s+have\s+memory|i\s*am\s+just\s+a\s+chatbot|as\s+an\s+ai\s+language\s+model)\b[^\n\.\!\?]*[\.!\?]?\s*")


def _clean(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""

    t = re.sub(r"^\s*:\s*", "", t).strip()

    t = _BOILERPLATE_RE.sub("", t).strip()
    t = _GREET_ECHO_RE.sub("", t).strip()
    t = _CHATBOT_DENY_RE.sub("", t).strip()
    t = re.sub(r"(?is)\s+(If you have more questions?[^\n]*)$", "", t).strip()
    t = re.sub(r"\s+", " ", t).strip()

    if t and not re.search(r"[.!?\"]$", t):
        last_stop = max(t.rfind("."), t.rfind("!"), t.rfind("?"))
        if last_stop > 0:
            t = t[: last_stop + 1].strip()

    return t

def _user_is_greeting(user_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return True
    t2 = re.sub(r"[^a-z0-9\s]", " ", t)
    t2 = re.sub(r"\s+", " ", t2).strip()
    return bool(re.match(r"^(hi|hey|hello|yo|sup)\b", t2))


class ResponseComposer:
    """Light cleanup over the model reply.

    This intentionally avoids steering the structure of the conversation.
    """

    def __init__(
        self,
        *,
        state_store: ConversationStateStore,
        llm=None,
        llm_semaphore=None,
    ) -> None:
        self._state = state_store
        self._llm = llm
        self._sem = llm_semaphore

    async def _regenerate_reply(
        self,
        *,
        decision: ConversationPolicyOutput,
        user_name: str | None,
        user_text: str,
        conversation_id: UUID,
    ) -> str:
        if self._llm is None or self._sem is None:
            return ""

        recent_chat = await self._state.recent_chat_text(conversation_id)
        system = (
            "You are Nova. Produce a short natural reply to the user's latest message. "
            "Stay consistent with the conversation mode and the existing thread. "
            "Do not sound scripted, do not add generic help-offers, and do not ask a follow-up question unless it is necessary to move the request forward."
        )
        user_payload = (
            f"mode: {(decision.mode or 'smalltalk').strip()}\n\n"
            f"user_name: {((user_name or '').strip() or 'unknown')}\n\n"
            f"recent_chat:\n{(recent_chat or '').strip() or '(none)'}\n\n"
            f"latest_user_message:\n{(user_text or '').strip()}\n\n"
            f"draft_reply_that_failed_cleanup:\n{(decision.assistant_reply or '').strip() or '(empty)'}"
        )

        async with self._sem:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=120,
                temperature=0.3,
                stop=None,
            )
        return _clean(reply or "")

    async def compose(
        self,
        *,
        decision: ConversationPolicyOutput,
        user_name: str | None,
        user_text: str,
        conversation_id: UUID,
    ) -> tuple[str, str | None]:
        reply = _clean(decision.assistant_reply)

        # Greeting echo prevention: if user greeted "Nova", don't mirror it back.
        if "nova" in (user_text or "").lower():
            reply = _GREET_ECHO_RE.sub("", reply).strip()
            if reply.lower().startswith("hey nova") or reply.lower().startswith("hi nova"):
                reply = re.sub(r"(?is)^\s*(hey|hi|hello)\s+nova\b\s*", "", reply).strip()

        if decision.mode == "smalltalk":
            # If user didn't greet, don't restart with a greeting.
            if reply and not _user_is_greeting(user_text):
                reply2 = re.sub(r"^(hey|hi|hello)\b[^.!?]{0,80}[.!?]\s*", "", reply, flags=re.IGNORECASE).strip()
                reply = reply2 or reply

            if not reply:
                reply = await self._regenerate_reply(
                    decision=decision,
                    user_name=user_name,
                    user_text=user_text,
                    conversation_id=conversation_id,
                )

            return reply, None

        # task/clarify/refuse: no enforced follow-up
        if not reply:
            reply = await self._regenerate_reply(
                decision=decision,
                user_name=user_name,
                user_text=user_text,
                conversation_id=conversation_id,
            )
        return reply, None
