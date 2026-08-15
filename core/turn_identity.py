from __future__ import annotations

"""Who is this turn from, and what may Nova do with that? (V3 P5.1)

P5 can tell who is speaking. This is the piece that decides what that means —
and its whole job is to make the unsafe answer impossible to reach by accident.

THE RULE
--------
Nova historically assumes `entity="user"` means Marcus, because until now only
Marcus could talk to her. Once a guest can speak, that assumption silently files
a stranger's statements into Marcus's profile. So every personal-memory write
and every personal-grounding read now asks this object first, and the default
answer is NO.

    typed / disabled      -> `user`                  (legacy Marcus semantics)
    voice, known owner    -> `user`                  (same as typed)
    voice, known other    -> `speaker:<profile_id>`  (their own namespace)
    voice, anything else  -> None                    (NO personal write target)

"Anything else" is deliberately broad: unknown, ambiguous, too_short,
unavailable, and — critically — a voice turn whose handle was missing, expired,
replayed or invented. Those all fail toward `None`, never toward Marcus.

NOT AUTHENTICATION
------------------
`stored_role` is a memory-routing and personalisation label. It is not an
authorisation level and is never consulted by `PermissionBroker`. A recognised
owner at 0.99 similarity gets exactly the permission decision typed Marcus gets.
There is deliberately no method here that answers "is this allowed".

CONCURRENCY
-----------
The active identity lives in a `ContextVar`, so concurrent turns cannot see each
other's speaker and a background worker cannot inherit a stale human. It is set
and reset around one logical turn, in a `finally`.
"""

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# Input sources.
SOURCE_TYPED = "typed"
SOURCE_VOICE = "voice"

# The legacy personal-memory entity. Everything that predates P5 writes here,
# and typed turns must keep doing exactly that.
OWNER_ENTITY = "user"

#: Role stored on an enrolled profile that maps to the legacy owner namespace.
ROLE_OWNER = "owner"


def speaker_entity(profile_id: str) -> str:
    """The personal-memory namespace for a known non-owner speaker."""
    return f"speaker:{profile_id}"


@dataclass(frozen=True)
class TurnIdentity:
    """Structured identity for one logical turn. Never derived from the client."""

    input_source: str = SOURCE_TYPED
    #: Was speaker identification attempted for this turn? False means the
    #: feature is off or the turn was typed — NOT that it failed.
    speaker_attempted: bool = False
    #: known | unknown | ambiguous | too_short | unavailable | "" for typed
    speaker_status: str = ""
    profile_id: str | None = None
    display_name: str | None = None
    #: Memory/personalisation label from the enrolled profile. NOT authority.
    stored_role: str | None = None
    #: True only when a backend-derived match was redeemed for this turn.
    backend_verified: bool = False
    #: Diagnostics only. Never rendered into a prompt.
    similarity: float | None = None
    #: Why identity is missing, when it is.
    reason: str = ""

    # ── the two questions everything else asks ───────────────────────────────

    @property
    def memory_entity(self) -> str | None:
        """Where personal facts from this turn belong — or None.

        None means "nowhere". It does not mean "the default", and callers must
        not substitute one: that substitution is the bug this whole phase
        exists to prevent.
        """
        if self.input_source == SOURCE_TYPED:
            return OWNER_ENTITY
        if not self.speaker_attempted:
            # Voice with speaker ID disabled: legacy Nova, legacy semantics.
            return OWNER_ENTITY
        if self.speaker_status == "known" and self.profile_id:
            if (self.stored_role or "").strip().lower() == ROLE_OWNER:
                return OWNER_ENTITY
            return speaker_entity(self.profile_id)
        # unknown / ambiguous / too_short / unavailable / unredeemed handle
        return None

    @property
    def may_write_personal(self) -> bool:
        return self.memory_entity is not None

    @property
    def is_owner(self) -> bool:
        """Does this turn get Marcus's personal grounding and personality?"""
        return self.memory_entity == OWNER_ENTITY

    @property
    def is_known_other(self) -> bool:
        return self.may_write_personal and not self.is_owner

    @property
    def is_unverified(self) -> bool:
        """A voice turn Nova could not attribute to anybody."""
        return not self.may_write_personal

    # ── rendering ────────────────────────────────────────────────────────────

    def addressee(self) -> str:
        """How the system prompt should refer to whoever is speaking.

        Identity only — no similarity, no thresholds. The model needs to know
        who it is talking to, not how a cosine turned out.
        """
        if self.is_owner:
            return ""                       # Marcus: existing prompt, unchanged
        if self.is_known_other and self.display_name:
            return (f"You are speaking with {self.display_name}, not Marcus. "
                    f"Do not treat Marcus's personal details as theirs.")
        return ("You are speaking with someone Nova does not recognise. "
                "Do not assume this is Marcus and do not share his personal "
                "details.")

    def describe(self) -> dict[str, Any]:
        """For /status and diagnostics. No embeddings, ever."""
        return {
            "input_source": self.input_source,
            "speaker_attempted": self.speaker_attempted,
            "speaker_status": self.speaker_status or None,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "stored_role": self.stored_role,
            "memory_entity": self.memory_entity,
            "may_write_personal": self.may_write_personal,
            "backend_verified": self.backend_verified,
        }

    # ── constructors ─────────────────────────────────────────────────────────

    @classmethod
    def typed(cls) -> "TurnIdentity":
        """Legacy Nova. Exactly what every pre-P5 turn was."""
        return cls(input_source=SOURCE_TYPED)

    @classmethod
    def voice_unverified(cls, reason: str = "no identity") -> "TurnIdentity":
        """A voice turn Nova could not attribute.

        Used for every failure of the handle: missing, invalid, expired,
        already redeemed. `speaker_attempted=True` records that the question WAS
        asked, which is what stops this collapsing into typed semantics.
        """
        return cls(input_source=SOURCE_VOICE, speaker_attempted=True,
                   speaker_status="unavailable", reason=reason)

    @classmethod
    def voice_legacy(cls) -> "TurnIdentity":
        """Voice with speaker identification disabled.

        Distinct from `voice_unverified`: nobody asked a speaker question, so
        pre-P5 behaviour applies and personal memory still goes to `user`.
        """
        return cls(input_source=SOURCE_VOICE, speaker_attempted=False,
                   reason="speaker id disabled")

    @classmethod
    def from_match(cls, match: Any, *, profile: Any = None) -> "TurnIdentity":
        """Build from a redeemed backend `SpeakerMatch`.

        `profile` supplies `stored_role`, which comes from the durable enrolled
        profile and never from the request — a client that could assert
        `role=owner` would have defeated the entire namespace separation.
        """
        if match is None:
            return cls.voice_unverified("handle not redeemed")
        if not getattr(match, "attempted", False):
            return cls.voice_legacy()
        return cls(
            input_source=SOURCE_VOICE,
            speaker_attempted=True,
            speaker_status=getattr(match, "status", "") or "",
            profile_id=getattr(match, "profile_id", None),
            display_name=getattr(match, "display_name", None),
            stored_role=(getattr(profile, "role", None) if profile is not None else None),
            backend_verified=True,
            similarity=getattr(match, "similarity", None),
            reason=getattr(match, "reason", "") or "",
        )


#: The identity of the turn currently executing on this task.
#:
#: A ContextVar rather than an attribute: concurrent turns each get their own
#: view, and a background worker that never entered `active_turn` sees the typed
#: default rather than whoever spoke last.
_CURRENT: contextvars.ContextVar[TurnIdentity] = contextvars.ContextVar(
    "nova_turn_identity", default=TurnIdentity.typed())


def current_identity() -> TurnIdentity:
    """Identity of the turn on this task. Typed/legacy when nothing is set."""
    return _CURRENT.get()


@contextmanager
def active_turn(identity: TurnIdentity | None) -> Iterator[TurnIdentity]:
    """Scope an identity to one logical turn, always restoring it afterwards."""
    ident = identity or TurnIdentity.typed()
    token = _CURRENT.set(ident)
    try:
        yield ident
    finally:
        _CURRENT.reset(token)
