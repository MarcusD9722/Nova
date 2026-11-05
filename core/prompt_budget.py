from __future__ import annotations

"""Make the prompt fit, instead of letting llama.cpp refuse it.

THE FAILURE THIS EXISTS FOR (audit 2026-11-02, H3)
--------------------------------------------------
Nothing bounded the assembled prompt. Recent messages, remembered facts,
episodes and tool output all grew with the conversation, and past `n_ctx`
llama.cpp stopped accepting any of it:

    Requested tokens (13433) exceed context window of 8192

Every turn after that answered "Sorry — I hit an internal error on that one",
including "Hello?". And because the failure happened before `_finish`, the
conversation state never scrolled, so the NEXT turn built the same oversized
prompt: the conversation was dead for good, across restarts, until the 7-day TTL
expired. There was no trimming code anywhere in the repo.

WHY AN ESTIMATE AND NOT THE REAL TOKENIZER
------------------------------------------
The real tokenizer lives on the loaded model, and the whole point of C2 is that
nothing touches that object outside the lock the worker thread holds. Blocking
context assembly on a generation to count tokens would trade a crash for a
stall. So this estimates, deliberately HIGH, and leaves a margin: being wrong in
the direction of a slightly shorter prompt costs a little context, and being
wrong the other way costs the conversation.

WHAT GETS DROPPED FIRST
-----------------------
In order of what the turn can most afford to lose. History before what is on
screen, what is on screen before what she remembers, remembered facts before the
conversation itself — and the persona, who she is talking to, and live tool
results are never trimmed, because a reply without those is a different reply
rather than a shorter one.
"""

from dataclasses import dataclass

#: Characters per token, rounded DOWN so the estimate comes out high. English
#: prose runs about 4; prompts here are denser than prose (names, JSON, paths).
_CHARS_PER_TOKEN = 3.4

#: Room left for the model's own reply plus the chat template's scaffolding.
#: Generous on purpose: the cost of over-reserving is a slightly smaller prompt.
SAFETY_TOKENS = 256

KEEP = "keep"              # never trimmed
DROP = "drop"              # removed whole when space is needed
TRIM_OLDEST = "oldest"     # keep the END of the text (newest lines)
TRIM_TAIL = "tail"         # keep the START of the text


@dataclass
class Block:
    name: str
    text: str
    policy: str = KEEP
    #: Smallest useful size, in characters. A block trimmed below this is
    #: dropped instead: three words of a conversation summary is not a summary.
    floor: int = 240


def estimate_tokens(text: str) -> int:
    """A deliberately high estimate of what `text` will cost.

    The larger of characters/3.4 and the word count, because short tokens
    (code, paths, names) break the characters-per-token rule in the direction
    that matters.
    """
    if not text:
        return 0
    return max(int(len(text) / _CHARS_PER_TOKEN) + 1, len(text.split()))


def fit(blocks: list[Block], *, context_tokens: int, reply_tokens: int,
        safety_tokens: int = SAFETY_TOKENS) -> tuple[list[Block], list[str]]:
    """Trim `blocks` to fit. Returns the blocks and a note of what was cut.

    The order of `blocks` is the order they appear in the prompt; the order they
    are TRIMMED is the order they are listed in `_TRIM_ORDER` below, which is
    about value rather than position.
    """
    limit = max(512, int(context_tokens) - int(reply_tokens) - int(safety_tokens))
    total = sum(estimate_tokens(b.text) for b in blocks)
    if total <= limit:
        return blocks, []

    by_name = {b.name: b for b in blocks}
    notes: list[str] = []
    for name in _TRIM_ORDER:
        if total <= limit:
            break
        b = by_name.get(name)
        if b is None or not b.text or b.policy == KEEP:
            continue
        cost = estimate_tokens(b.text)
        need = total - limit
        if b.policy == DROP:
            b.text = ""
            total -= cost
            notes.append(f"dropped {name}")
            continue
        # A trimmable block is never DROPPED, only shortened to its floor. The
        # difference matters for live tool output: half a result is still the
        # answer to the question, and none of it is not.
        keep_chars = max(b.floor, int((cost - need - 16) * _CHARS_PER_TOKEN))
        if b.policy == TRIM_OLDEST:
            b.text = _tail(b.text, keep_chars)
        else:
            b.text = _head(b.text, keep_chars)
        total = total - cost + estimate_tokens(b.text)
        notes.append(f"trimmed {name}")
    return blocks, notes


#: Least valuable first. History is evidence about the past; the conversation in
#: front of her is the turn.
_TRIM_ORDER = (
    "episodic",       # what happened weeks ago
    "artifacts",      # what was on screen earlier
    "memory",         # remembered facts (searchable again any time)
    "summary",        # the rolling summary of this conversation
    # Live tool output is current state and outranks remembered things — but a
    # long web page must not be allowed to eat the conversation it was fetched
    # to answer. It shrinks to a floor BEFORE the conversation is touched.
    "tools",
    "recent_chat",    # the conversation itself, oldest lines first
)


def _tail(text: str, keep_chars: int) -> str:
    """Keep the END, cut on a line boundary so no line is left half-rendered."""
    if len(text) <= keep_chars:
        return text
    cut = text[-keep_chars:]
    nl = cut.find("\n")
    return cut[nl + 1:] if nl != -1 else cut


def _head(text: str, keep_chars: int) -> str:
    """Keep the START, cut on a line boundary."""
    if len(text) <= keep_chars:
        return text
    cut = text[:keep_chars]
    nl = cut.rfind("\n")
    return cut[:nl] if nl > 0 else cut
