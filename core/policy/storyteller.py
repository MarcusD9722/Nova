from __future__ import annotations

"""Storytelling / creative-writing mode for Nova.

Before this, "story" only nudged a token budget on a generic reply. This adds a
real narrative mode: a craft-focused system prompt, a proper length budget, and
persistent story state (characters, setting, plot-so-far) kept per conversation
so a story can continue across turns and sessions — the same way conversation
summaries are persisted as facts.
"""

import re

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger

logger = get_logger(__name__)

# Strong triggers always start/continue a story. Weak ones ("keep going") only
# count when a story is already active (checked by the caller).
_STRONG_STORY_RE = re.compile(
    r"\b(?:tell|write|start|create|spin|give)\s+(?:me\s+)?(?:a|an|another|the\s+next)?\s*"
    r"(?:\w+\s+){0,3}?(?:story|tale|fable|saga|adventure)\b"
    r"|\bcontinue\s+(?:the|our|that|my|this)\s+story\b"
    r"|\bmake\s+up\s+(?:a|an|another)\s+(?:story|tale)\b",
    re.IGNORECASE,
)
_WEAK_STORY_RE = re.compile(
    r"\b(?:what\s+happens\s+next|keep\s+going|go\s+on|continue|and\s+then\??|next\s+part)\b",
    re.IGNORECASE,
)


def is_story_request(text: str, *, story_active: bool = False) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _STRONG_STORY_RE.search(t):
        return True
    if story_active and _WEAK_STORY_RE.search(t):
        return True
    return False


def story_system_prompt(user_name: str | None, story_state: str) -> str:
    who = (user_name or "").strip() or "the reader"
    base = (
        "You are Nova, telling a story to " + who + ". Right now you are a gifted storyteller, not an "
        "assistant.\n\n"
        "Craft:\n"
        "- Write vivid, immersive prose with real momentum — sensory detail, characters who want things, "
        "a scene that moves. Show, don't summarize.\n"
        "- Match what was asked (genre, tone, length, characters). If they asked to CONTINUE, pick up "
        "exactly where the story left off and stay consistent with established characters and events.\n"
        "- Write a satisfying chunk (a scene or a few paragraphs), then pause at a natural beat. Don't wrap "
        "the whole thing in one turn unless they asked for a complete short story.\n"
        "- No preamble like 'Sure, here's a story' and no meta-commentary — just tell it. End with the story, "
        "not a question, unless it's a genuine cliffhanger.\n"
        "- Do NOT write any analysis or reasoning — just the story prose.\n"
    )
    if story_state.strip():
        base += "\nStory so far (stay consistent with this):\n" + story_state.strip() + "\n"
    return base


class StorytellerLLM:
    """Distills/updates the running story state after each story turn."""

    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore

    async def update_state(self, *, prior_state: str, latest_exchange: str) -> str:
        prompt = (
            "You maintain a compact 'story bible' for an ongoing story so it can continue consistently "
            "later. Update it from the latest installment.\n\n"
            + (f"Current story bible:\n{prior_state.strip()}\n\n" if prior_state.strip() else "")
            + f"Latest installment:\n{latest_exchange[:3000]}\n\n"
            "Return a concise updated bible (<=180 words) with: SETTING, CHARACTERS (name — one line each), "
            "and PLOT SO FAR (bulleted beats). Prose only, no preamble."
        )
        try:
            async with self._sem:
                out = await self._llm.chat(
                    [{"role": "user", "content": prompt}], max_tokens=400, temperature=0.2, thinking=True
                )
            return (out or "").strip()[:1600]
        except Exception as e:  # noqa: BLE001
            logger.debug("story_state_update_failed", error=str(e))
            return prior_state
