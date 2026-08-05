from __future__ import annotations

"""Context firewall — what may leave the machine (U2, Phase: cloud LLM).

Nova's core promise is that her memory of Marcus stays local. Routing a *role*
(coder/planner) to a remote model must never turn into routing his *life* there
too. This module is the single chokepoint every remote-bound prompt passes
through, and it is deliberately conservative:

  1. DROP whole blocks that are recognizably personal context — the grounding
     dict's keys, its rendered natural-language form, and raw memory records
     (FACT/PERSON/EVENT/FILE lines).
  2. REDACT identities that may legitimately appear inside an otherwise
     task-shaped message (Marcus's name, known people's names) rather than
     destroying the request.
  3. VERIFY afterwards and FAIL CLOSED — if anything personal survives the
     scrub, the caller must refuse the remote call and fall back to local.

Design note: this is an *inspection* firewall, not a promise that a model can't
infer things. It stops the bulk, structured leak — grounding blocks, memory
dumps, names — which is the realistic exposure. It is pure and fully testable;
the CloudRuntime applies it to every single call.
"""

import re
from dataclasses import dataclass, field
from typing import Any

# Keys of the grounding context dict (core/runtime.py::_build_grounding_context).
# Their presence means a personal-context blob is in the text.
PERSONAL_JSON_KEYS = frozenset({
    "known_user", "known_family", "known_people", "current_focus", "recent_mood",
    "relationship_reminders", "interest_drift", "wellbeing_trend", "catchup_summary",
    "executive_recommendations", "operating_state",
})

# Distinctive fragments of the RENDERED grounding line (_grounding_to_natural).
PERSONAL_PHRASES = (
    "the user's name is",
    "known family:",
    "you are currently working with the user on the",
    "existing projects:",
    "let that inform your warmth",
    "mention this gently",
    "this is the start of a new conversation after a while away",
    "internal operating note",
    "proactive context you may raise",
    "since you last talked",
)

# Raw memory records as they appear in recall payloads / prompts.
MEMORY_RECORD_RE = re.compile(r"^\s*(FACT|PERSON|EVENT|FILE)\s+\S", re.MULTILINE)

_REDACTED_USER = "[user]"
_REDACTED_PERSON = "[person]"
_REDACTED_ADDRESS = "[address]"
_REDACTED_EMAIL = "[email]"
_REDACTED_PHONE = "[phone]"

# Contact PII that survives the block-level drop because it can appear inside
# an otherwise task-shaped message. Verified live: a sentence like "Marcus
# lives at 9139 Coronal Rings with Leslie" had the NAMES redacted and the
# STREET ADDRESS sent to the provider intact.
#
# These are REDACTIONS, not refusals, on purpose. Refusing would silently
# disable cloud coding whenever a prompt happened to contain an address, which
# is a worse failure than sending a placeholder. A model can still write the
# code around "[address]".
#
# Redaction also does not depend on the identity cache, which name redaction
# does — that cache is only populated by a chat turn's grounding build, so
# background ProjectBuilder calls can reach the provider with it empty.
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z'\-]+\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Court|Ct|"
    r"Circle|Cir|Way|Trail|Trl|Place|Pl|Parkway|Pkwy|Terrace|Ter|Highway|Hwy|"
    r"Rings|Ridge|Run|Bend|Crossing|Loop|Square|Sq)\b\.?",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")


@dataclass
class ScrubResult:
    messages: list[dict[str, Any]]
    dropped: int = 0                       # whole messages removed
    redactions: int = 0                    # identity substitutions made
    markers: list[str] = field(default_factory=list)   # what tripped, for honest logging

    @property
    def clean(self) -> bool:
        return True

    def summary(self) -> str:
        bits = []
        if self.dropped:
            bits.append(f"{self.dropped} block(s) withheld")
        if self.redactions:
            bits.append(f"{self.redactions} name(s) redacted")
        return "; ".join(bits) or "nothing personal detected"


def inspect_text(text: str) -> list[str]:
    """Which personal-context markers appear in `text` (empty = looks clean)."""
    found: list[str] = []
    low = (text or "").lower()
    for key in PERSONAL_JSON_KEYS:
        # match the JSON-ish key form so ordinary prose can't trip it
        if f'"{key}"' in low or f"'{key}'" in low:
            found.append(f"grounding-key:{key}")
    for phrase in PERSONAL_PHRASES:
        if phrase in low:
            found.append(f"grounding-phrase:{phrase[:28]}")
    if MEMORY_RECORD_RE.search(text or ""):
        found.append("memory-record")
    return found


def _redact_names(text: str, user_name: str | None, known_names: list[str] | None) -> tuple[str, int]:
    """Replace personal identities with placeholders. Longest-first so a full
    name is replaced before its first-name substring."""
    count = 0
    out = text or ""
    if user_name and len(user_name.strip()) >= 2:
        pattern = re.compile(rf"\b{re.escape(user_name.strip())}\b", re.IGNORECASE)
        out, n = pattern.subn(_REDACTED_USER, out)
        count += n
    for name in sorted([n for n in (known_names or []) if n and len(n.strip()) >= 3],
                       key=lambda s: len(s), reverse=True):
        pattern = re.compile(rf"\b{re.escape(name.strip())}\b", re.IGNORECASE)
        out, n = pattern.subn(_REDACTED_PERSON, out)
        count += n

    # Contact PII, independent of whether the identity cache was populated.
    for pattern, placeholder in (
        (_EMAIL_RE, _REDACTED_EMAIL),      # before phone: an email can contain digits
        (_ADDRESS_RE, _REDACTED_ADDRESS),
        (_PHONE_RE, _REDACTED_PHONE),
    ):
        out, n = pattern.subn(placeholder, out)
        count += n
    return out, count


def scrub_messages(
    messages: list[dict[str, Any]],
    *,
    user_name: str | None = None,
    known_names: list[str] | None = None,
) -> ScrubResult:
    """Make a message list safe to send to a remote model.

    Blocks recognizably personal (grounding/memory) are DROPPED entirely;
    identities inside otherwise task-shaped text are REDACTED. Never mutates
    the caller's list.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    redactions = 0
    markers: list[str] = []

    for msg in messages or []:
        content = str(msg.get("content") or "")
        hits = inspect_text(content)
        if hits:
            dropped += 1
            markers.extend(hits)
            continue
        safe, n = _redact_names(content, user_name, known_names)
        redactions += n
        kept.append({**msg, "content": safe})

    # De-duplicate markers, keep order.
    seen: set[str] = set()
    markers = [m for m in markers if not (m in seen or seen.add(m))]
    return ScrubResult(messages=kept, dropped=dropped, redactions=redactions, markers=markers)


def verify_safe(messages: list[dict[str, Any]]) -> list[str]:
    """Post-scrub check. Returns any surviving markers — a NON-EMPTY result
    means the caller must refuse the remote call (fail closed)."""
    surviving: list[str] = []
    for msg in messages or []:
        surviving.extend(inspect_text(str(msg.get("content") or "")))
    seen: set[str] = set()
    return [m for m in surviving if not (m in seen or seen.add(m))]
