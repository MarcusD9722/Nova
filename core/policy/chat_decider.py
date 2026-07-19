from __future__ import annotations

import json
import re
from datetime import datetime

from core.llm_runtime import LLMRuntime
from core.policy.contracts import ConversationPolicyOutput, TaskToEnqueue
from core.policy.followup_generator import FollowUpGeneratorLLM


_TASK_REQUEST_RE = re.compile(
    r"(?is)^\s*(?:nova[,:\-]?\s*)?(?:"
    r"can\s+you|could\s+you|would\s+you|will\s+you|"
    r"please|help\s+me|i\s+need\s+you\s+to|"
    r"start|build|create|make|write|fix|debug|investigate|"
    r"look\s+into|search|find|set\s+up|open|run|install|"
    r"add|remove|delete|refactor|optimize|improve|update"
    r")\b"
)

_NON_TASK_CHAT_RE = re.compile(
    r"(?is)\b("
    r"what\s+is|who\s+is|what\s+are|how\s+does|how\s+do|"
    r"tell\s+me|explain|define|"
    r"short\s+story|story\s+about|joke|poem|"
    r"say\s+hi|greet|"
    r"are\s+you\s+able\s+to|are\s+you\s+connected|"
    r"how\s+far|directions?|route\s+from|navigate|what\s+time|what\s+day|weather"
    r")\b"
)


def _now_str() -> str:
    """Return the actual current local date/time as a plain string."""
    try:
        now = datetime.now().astimezone()
        return now.strftime("%A, %B %d, %Y at %I:%M %p %Z").strip()
    except Exception:
        return ""


def _extract_live_data(memory_context: str) -> tuple[str, str]:
    """Split live_* lines (tool results) from the rest of memory_context."""
    live_parts: list[str] = []
    rest_parts: list[str] = []
    for line in (memory_context or "").splitlines():
        if line.strip().startswith("live_"):
            # Pretty-print the JSON payload if possible
            try:
                prefix, payload = line.split(":", 1)
                data = json.loads(payload.strip())
                pretty = ", ".join(f"{k}={v}" for k, v in data.items() if k != "steps_count")
                live_parts.append(f"{prefix}: {pretty}")
            except Exception:
                live_parts.append(line.strip())
        else:
            rest_parts.append(line)
    return "\n".join(live_parts), "\n".join(rest_parts)


def _smalltalk_token_budget(user_text: str) -> int:
    low = (user_text or "").lower()
    if any(p in low for p in ("short story", "story about", "tell me a story", "story again", "tell the story again")):
        return 420
    if any(p in low for p in ("poem", "joke", "bedtime story")):
        return 260
    if any(p in low for p in ("what is", "who is", "explain", "how does", "how do")):
        return 220
    return 180


def _looks_like_task_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _NON_TASK_CHAT_RE.search(text):
        return False
    if _TASK_REQUEST_RE.match(text):
        return True
    low = text.lower()
    return any(
        phrase in low
        for phrase in (
            " can you ",
            " could you ",
            " would you ",
            " please ",
            " help me ",
            " i need you to ",
            " set up ",
            " look into ",
        )
    )


def _needs_task_clarification(user_text: str) -> bool:
    text = re.sub(r"\s+", " ", (user_text or "").strip())
    if not text:
        return True
    if len(text.split()) <= 2:
        return True
    vague = {"help me", "fix it", "do it", "start it", "work on it"}
    return text.lower() in vague


def _task_title(user_text: str) -> str:
    text = (user_text or "").strip()
    text = re.sub(r"(?is)^\s*nova[,:\-]?\s*", "", text)
    text = re.sub(
        r"(?is)^\s*(?:can\s+you|could\s+you|would\s+you|will\s+you|please|help\s+me|i\s+need\s+you\s+to)\s+",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip(" .!?")
    if not text:
        return "New task"
    if len(text) <= 72:
        return text
    clipped = text[:72].rsplit(" ", 1)[0].strip()
    return (clipped or text[:72]).rstrip(" .!?") + "..."


class ChatDecider:
    """Lightweight turn decider.

    Smalltalk uses a normal conversational prompt instead of a rigid JSON policy.
    Explicit task requests still return a structured task decision so Nova can queue work.
    """

    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore
        self._followup_gen = FollowUpGeneratorLLM(llm, llm_semaphore=llm_semaphore)

    async def _generate_clarification_reply(
        self,
        *,
        user_text: str,
        user_name: str | None,
        memory_context: str,
        conversation_summary: str,
        recent_chat: str,
        available_tools: list[str],
    ) -> str:
        system = (
            "You are Nova. The user asked you to do something, but the request is still too vague to act on. "
            "Ask for the smallest missing detail in one natural sentence. "
            "Sound conversational, not like a form letter or ticket system. "
            "Do not mention internal limits, policies, or tools unless the request itself requires it."
        )

        user_payload = (
            f"user_name: {((user_name or '').strip() or 'unknown')}\n\n"
            f"available_tools:\n{', '.join(available_tools) if available_tools else '(none)'}\n\n"
            f"memory_context:\n{(memory_context or '').strip() or '(none)'}\n\n"
            f"conversation_summary:\n{(conversation_summary or '').strip() or '(none)'}\n\n"
            f"recent_chat:\n{(recent_chat or '').strip() or '(none)'}\n\n"
            f"vague_task_request:\n{user_text}"
        )

        async with self._sem:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=90,
                temperature=0.25,
                stop=None,
            )

        return (reply or "").strip()

    async def decide(
        self,
        *,
        user_text: str,
        user_name: str | None,
        memory_context: str,
        conversation_summary: str,
        recent_chat: str,
        project_name: str,
        available_tools: list[str],
    ) -> ConversationPolicyOutput:
        del project_name

        text = (user_text or "").strip()
        if _looks_like_task_request(text):
            return await self._decide_task(
                user_text=text,
                user_name=user_name,
                memory_context=memory_context,
                conversation_summary=conversation_summary,
                recent_chat=recent_chat,
                available_tools=available_tools,
            )

        return await self._decide_smalltalk(
            user_text=text,
            user_name=user_name,
            memory_context=memory_context,
            conversation_summary=conversation_summary,
            recent_chat=recent_chat,
            available_tools=available_tools,
        )

    async def _decide_smalltalk(
        self,
        *,
        user_text: str,
        user_name: str | None,
        memory_context: str,
        conversation_summary: str,
        recent_chat: str,
        available_tools: list[str],
    ) -> ConversationPolicyOutput:
        now = _now_str()
        live_data, cleaned_memory = _extract_live_data(memory_context)

        system = (
            f"Today is {now}. Use this exact time when the user asks what time or date it is — never guess.\n"
            "You are Nova. Talk like a real person, not a helpdesk bot. "
            "Reply to the latest user message naturally and stay grounded in the current thread. "
            "Use recent_chat as the main continuity source. conversation_summary is older compressed context. "
            "memory_context is a structured JSON blob containing stable facts and capability notes.\n\n"
            "Live data rules (CRITICAL — never guess when live data is present):\n"
            "- memory_context.current_datetime contains the exact real current time. Always use it for time/date questions — never invent or estimate the time.\n"
            "- If memory_context starts with live_weather: ..., use those exact figures (temp_f, condition, humidity_pct) when asked about weather. Do not invent conditions.\n"
            "- If memory_context starts with live_directions: ..., use the exact distance and duration from that field when asked about travel. Do not invent times or distances.\n\n"
            "Style rules:\n"
            "- Sound calm, direct, and human.\n"
            "- Answer what the user actually said; do not pivot into generic coaching.\n"
            "- If the user asks for information, answer the question directly and stop cleanly when the answer is done.\n"
            "- If the user asks for something creative like a short story, deliver the thing they asked for instead of talking about doing it.\n"
            "- If the user asks about identity or memory, answer from memory_context when possible.\n"
            "- Treat names listed in memory_context arrays as complete lists unless the user says otherwise.\n"
            "- If the user asks about device integrations or capabilities, only claim what memory_context or available_tools supports.\n"
            "- When memory_context lists multiple names, include all relevant names instead of dropping one.\n"
            "- If capability.smart_home_control = unavailable, say you are not connected right now and stop there. Do not add promises about future work.\n"
            "- If the user gives simple positive feedback, thank them plainly and stop.\n"
            "- If the user asks to retell a story with a changed name, retell the full story with the new name and finish it cleanly.\n"
            "- Do not force a follow-up question. If a question is unnecessary, end with a natural statement.\n"
            "- Avoid canned lines like 'That's great to hear', 'I'm glad', or 'What's on your mind?' unless they genuinely fit.\n"
            "- Avoid coachy phrasing like 'let's focus on', 'we can work on that', or tag questions like 'okay?' unless the user explicitly wants planning.\n"
            "- If the user critiques your tone or flow, acknowledge it plainly and speak normally instead of turning it into a pep talk.\n"
            "- Do not mirror the user's greeting back with 'Nova' in it.\n"
            "- Keep it to 1-3 sentences unless the user clearly wants more."
        )

        user_payload = (
            f"user_name: {((user_name or '').strip() or 'unknown')}\n\n"
            + (f"LIVE DATA (verified real values — use these exact numbers, do not substitute your own):\n{live_data}\n\n" if live_data else "")
            + f"available_tools:\n{', '.join(available_tools) if available_tools else '(none)'}\n\n"
            f"memory_context:\n{(cleaned_memory or '').strip() or '(none)'}\n\n"
            f"conversation_summary:\n{(conversation_summary or '').strip() or '(none)'}\n\n"
            f"recent_chat:\n{(recent_chat or '').strip() or '(none)'}\n\n"
            f"latest_user_message:\n{user_text}"
        )

        async with self._sem:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=_smalltalk_token_budget(user_text),
                temperature=0.3,
                stop=None,
            )

        reply_text = (reply or "").strip()

        # Generate a contextual follow-up question when the reply doesn't already
        # end with one and the exchange isn't simple feedback/greeting.
        follow_up: str | None = None
        _skip_phrases = ("short story", "story about", "poem", "joke", "bedtime story", "tell me a story")
        if not reply_text.endswith("?") and not any(p in (user_text or "").lower() for p in _skip_phrases):
            avoid: list[str] = []
            if recent_chat:
                avoid = [line.strip() for line in recent_chat.splitlines() if line.strip().endswith("?")][-5:]
            try:
                fu = await self._followup_gen.regenerate(user_text=user_text, avoid=avoid)
                q = (fu.follow_up_question or "").strip()
                if q and q != "?":
                    follow_up = q
            except Exception:
                pass

        return ConversationPolicyOutput(
            mode="smalltalk",
            assistant_reply=reply_text,
            follow_up_question=follow_up,
            should_enqueue_task=False,
            task_to_enqueue=None,
            tool_plan=[],
            memory_facts=[],
        )

    async def _decide_task(
        self,
        *,
        user_text: str,
        user_name: str | None,
        memory_context: str,
        conversation_summary: str,
        recent_chat: str,
        available_tools: list[str],
    ) -> ConversationPolicyOutput:
        if _needs_task_clarification(user_text):
            clarify_reply = await self._generate_clarification_reply(
                user_text=user_text,
                user_name=user_name,
                memory_context=memory_context,
                conversation_summary=conversation_summary,
                recent_chat=recent_chat,
                available_tools=available_tools,
            )
            return ConversationPolicyOutput(
                mode="clarify",
                assistant_reply=clarify_reply,
                follow_up_question=None,
                should_enqueue_task=False,
                task_to_enqueue=None,
                tool_plan=[],
                memory_facts=[],
            )

        task_now = _now_str()
        task_live, task_cleaned_memory = _extract_live_data(memory_context)

        system = (
            f"Today is {task_now}.\n"
            "You are Nova. The user is asking you to take on a task. "
            "Reply naturally in 1-2 sentences. "
            "Acknowledge the work directly without sounding like a ticketing system. "
            "Do not promise results you do not have yet. "
            "Do not add a follow-up question unless the request is genuinely ambiguous."
        )

        user_payload = (
            f"user_name: {((user_name or '').strip() or 'unknown')}\n\n"
            + (f"LIVE DATA (verified real values):\n{task_live}\n\n" if task_live else "")
            + f"available_tools:\n{', '.join(available_tools) if available_tools else '(none)'}\n\n"
            f"memory_context:\n{(task_cleaned_memory or '').strip() or '(none)'}\n\n"
            f"conversation_summary:\n{(conversation_summary or '').strip() or '(none)'}\n\n"
            f"recent_chat:\n{(recent_chat or '').strip() or '(none)'}\n\n"
            f"task_request:\n{user_text}"
        )

        async with self._sem:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=140,
                temperature=0.25,
                stop=None,
            )

        return ConversationPolicyOutput(
            mode="task",
            assistant_reply=(reply or "").strip(),
            follow_up_question=None,
            should_enqueue_task=True,
            task_to_enqueue=TaskToEnqueue(title=_task_title(user_text), details=user_text),
            tool_plan=[],
            memory_facts=[],
        )