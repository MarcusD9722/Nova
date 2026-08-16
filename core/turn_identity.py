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


#: Is a human turn actually in scope on this task?
#:
#: Separate from `_CURRENT` because `_CURRENT` has a legacy DEFAULT, and a
#: default is indistinguishable from a real value once you read it. Off the turn
#: path — a background worker, a scheduled build, a bus publish from a timer —
#: `current_identity()` returns typed Marcus, which reads as "Marcus is here"
#: and is simply false (V3 P5.1e.1). Measured: an off-turn `project.completed`
#: was persisted as `speaker_label="Marcus"` next to a summary that said "Nova
#: finished …".
_IN_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nova_turn_active", default=False)


def current_identity() -> TurnIdentity:
    """Identity of the turn on this task. Typed/legacy when nothing is set.

    Unchanged on purpose. A great deal of code relies on the legacy default —
    typed Marcus is the right answer for every pre-P5 caller — and making this
    return None would be a far larger change than the bug warrants. Callers that
    genuinely need to know whether a human is present ask
    `current_identity_or_none()`.
    """
    return _CURRENT.get()


def has_active_turn() -> bool:
    """True only inside `active_turn(...)`. False for the legacy default."""
    return _IN_TURN.get()


def current_identity_or_none() -> TurnIdentity | None:
    """The identity of a REAL turn, or None when nothing is in scope.

    Use this anywhere the answer "nobody" is meaningfully different from
    "Marcus" — attribution, provenance, anything that will be written down.
    """
    return _CURRENT.get() if _IN_TURN.get() else None


@contextmanager
def active_turn(identity: TurnIdentity | None) -> Iterator[TurnIdentity]:
    """Scope an identity to one logical turn, always restoring it afterwards."""
    ident = identity or TurnIdentity.typed()
    token = _CURRENT.set(ident)
    marker = _IN_TURN.set(True)
    try:
        yield ident
    finally:
        _IN_TURN.reset(marker)
        _CURRENT.reset(token)


# ── Read scope (V3 P5.1d) ────────────────────────────────────────────────────
#
# Blocking writes was only half the boundary. Three independent read paths were
# measured handing Marcus's private data to an unrecognised speaker:
#
#   * `_direct_live_reply` answering "what is my name?" with his name
#   * `memory.search` surfacing a private fact into the prompt
#   * the `memory.recall` TOOL returning it when the model asked
#
# The last one matters most architecturally: a privacy boundary enforced only in
# grounding is one tool call wide. So the policy lives here, once, and every
# reader consults it.

#: Entities any speaker may read. Deliberately tiny and explicit.
#:
#: `note` is NOT here. It is free-form and routinely holds personal material —
#: "not stored under `user`" is not the same as "public", and treating it as
#: public is exactly how a leak gets rationalised.
#:
#: Neither are `project:` / `projects`. They were, briefly, on the theory that a
#: project is collaborative context. Measuring item E settled it: what Marcus is
#: building, and the names of everything he has built, is a personal detail a
#: stranger in the room has no claim on. Only genuinely impersonal knowledge
#: stays here.
SHARED_ENTITY_ROOTS: tuple[str, ...] = (
    "world",        # general knowledge Nova looked up
    "system",       # how Nova herself is configured
    "capability",   # what she can do
)

#: Kept as an alias: the old name said "prefixes", which is exactly the mistake
#: `under_root` fixes. These are roots, and only `:` descends from them.
SHARED_ENTITY_PREFIXES = SHARED_ENTITY_ROOTS

#: The one delimiter that means "inside". Everything here is exact about it.
SEP = ":"

#: The complete set of beside-the-root child namespaces P5.1d could write
#: (`lesson:speaker:p-alice` and friends). Finite and closed on purpose: the
#: compatibility rule matches these exact shapes and nothing else.
LEGACY_SPEAKER_CHILD_ROOTS: tuple[str, ...] = ("lesson", "mood", "wellbeing", "session")

#: What a speaker means when they say "me".
SELF_ALIASES: frozenset[str] = frozenset({"user", "me", "myself", ""})


def under_root(entity: Any, root: str) -> bool:
    """Is `entity` the namespace `root`, or something nested inside it?

    `world` and `world:weather` are. `worldsecret` and `world_private` are NOT —
    they are unrelated entities that merely start with the same letters.

    This existed as `startswith(root)`, which meant an allow-list of three roots
    silently admitted every entity whose name began with one of them. An
    allow-list that matches on substring is not an allow-list.
    """
    e = str(entity or "").strip().lower()
    r = str(root or "").strip().lower().rstrip(SEP)
    if not e or not r:
        return False
    return e == r or e.startswith(r + SEP)


def is_shared_entity(entity: Any) -> bool:
    """Is this entity safe for ANY speaker to read?"""
    return any(under_root(entity, r) for r in SHARED_ENTITY_ROOTS)


def entity_belongs_to_speaker(entity: Any, memory_entity: Any) -> bool:
    """Does `entity` live inside the personal namespace `memory_entity` owns?

    One canonical hierarchy per person, so the policy is a single containment
    check rather than a list of special cases:

        speaker:p-alice                    their root
        speaker:p-alice:lesson             what they asked Nova to do differently
        speaker:p-alice:mood               how they have seemed
        speaker:p-alice:person:sarah       someone THEY know

    `speaker:p-alice2` is not inside `speaker:p-alice`, which is precisely why
    this must not be a `startswith`.
    """
    own = str(memory_entity or "").strip().lower()
    if not own:
        return False
    if under_root(entity, own):
        return True
    # Back-compat: P5.1d wrote the child namespaces the other way round
    # (`lesson:speaker:p-alice`). Nothing live produced those — the frontend has
    # never sent a speaker — but recognising them avoids stranding any a test
    # run or manual write left behind.
    #
    # It must match the EXACT shapes P5.1d could produce, not a suffix. An
    # `endswith(":" + own)` rule read `speaker:p-bob:lesson:speaker:p-alice` as
    # Alice's, which is Bob's namespace with her name appended — the same
    # substring-for-structure mistake `under_root` exists to prevent, smuggled
    # back in through the compatibility path (P5.1d.2).
    e = str(entity or "").strip().lower()
    if not own.startswith("speaker" + SEP):
        return False
    return any(e == f"{root}{SEP}{own}" for root in LEGACY_SPEAKER_CHILD_ROOTS)


def personal_tail(entity: Any) -> str:
    """What `entity` would be called if the speaker were the owner.

    `speaker:p-alice` -> `user`, `speaker:p-alice:note` -> `note`,
    `speaker:p-alice:person:sarah` -> `person:sarah`. Anything else is itself.

    This is what makes person-quality memory parity a property of the namespace
    rather than a rule duplicated into salience, decay and singleton handling —
    each of which had drifted apart. A guest's own name is as much a core
    identity fact as Marcus's; a guest's passing note is as forgettable as his.
    """
    e = str(entity or "").strip().lower()
    if not e.startswith("speaker" + SEP):
        return e
    rest = e.split(SEP, 2)          # ["speaker", "<id>", "<tail>"?]
    if len(rest) < 2 or not rest[1]:
        return e
    return rest[2] if len(rest) > 2 and rest[2] else OWNER_ENTITY


def may_read_entity(entity: Any, identity: "TurnIdentity | None" = None) -> bool:
    """May the current speaker see a fact stored under `entity`?

    Conservative by construction: anything not positively recognised as shared,
    and not inside the speaker's own namespace, is refused. A new personal
    entity added later is private by default rather than public by oversight.
    """
    ident = identity or current_identity()
    e = str(entity or "").strip().lower()
    if is_shared_entity(e):
        return True
    own = ident.memory_entity
    if own is None:
        # Unverified: shared knowledge only.
        return False
    if own == OWNER_ENTITY:
        # The owner reads everything, exactly as before P5.
        return True
    # A known guest reads their own namespace ENTIRELY — root and children —
    # and nothing else personal, including no other guest's.
    return entity_belongs_to_speaker(e, own)


def remap_entity_for(entity: Any, identity: "TurnIdentity | None" = None) -> str | None:
    """Where a fact extracted from THIS speaker's words actually belongs.

    The background extractor emits `entity="user"` for anything phrased in the
    first person, because for Nova's whole life "user" meant Marcus. Left alone,
    a guest saying "my wife is a nurse" becomes a durable claim about Marcus's
    marriage — written minutes later, by a worker, with nobody watching.

        owner        -> unchanged (legacy semantics, byte for byte)
        known guest  -> "user" becomes THEIR namespace; shared entities pass
                        through; anything else personal is nested under them
        unverified   -> None: nothing this speaker said is written anywhere

    None means discard. As everywhere else in this module, it must never be
    turned back into a default.
    """
    ident = identity or current_identity()
    e = str(entity or "").strip()
    if not e:
        return None
    own = ident.memory_entity
    if own is None:
        return None
    if own == OWNER_ENTITY:
        return e
    if e.lower() == OWNER_ENTITY:
        return own
    if is_shared_entity(e):
        # World knowledge and shared project state are not anyone's personal
        # history, and a recognised person may contribute to them.
        return e
    # Their people, their notes, their projects — kept under their own root so
    # "person:sarah" from a guest never merges with Marcus's Sarah.
    return f"{own}:{e}"


def resolve_write_target(entity: Any,
                         identity: "TurnIdentity | None" = None,
                         *, allow_shared: bool = False) -> tuple[str | None, str]:
    """Where an EXPLICITLY NAMED entity may be written by the current speaker.

    `remap_entity_for` answers "where do this speaker's words belong" for text
    the extractor parsed. This answers the harder question: the model has handed
    us an entity string and we must not trust it. The model does not know who is
    in the room, and asking it to pick a safe target would make a privacy
    boundary probabilistic (P5.1d.2).

    Returns `(target, reason)`. A `None` target means refuse — never write.

        owner                  -> (entity, "owner")          unchanged, always
        unverified             -> (None,   "unverified_speaker")
        guest, "me"/"user"     -> (their root, "self")
        guest, own namespace   -> (entity, "own")
        guest, another speaker -> (None,   "other_speaker")   <- the real attack
        guest, shared entity   -> (entity, "shared") if allowed, else refuse
        guest, anything else   -> (nested under them, "nested")

    The `other_speaker` case is why this is a refusal rather than a remap:
    measured on `d1ec5a9`, Alice calling memory.correct with
    `entity="speaker:p-bob"` changed Bob's stored fact from blue to red.
    """
    ident = identity or current_identity()
    e = str(entity or "").strip()
    own = ident.memory_entity
    if own is None:
        return (None, "unverified_speaker")
    if own == OWNER_ENTITY:
        # Typed / legacy voice / recognised owner: byte-for-byte legacy.
        return (e or OWNER_ENTITY, "owner")

    low = e.lower()
    if low in SELF_ALIASES:
        return (own, "self")
    if entity_belongs_to_speaker(low, own):
        return (e, "own")
    if low.startswith("speaker" + SEP):
        # Names a speaker root that is not theirs. Refuse outright: nesting it
        # under them would silently invent `speaker:p-alice:speaker:p-bob`,
        # which reads like a claim about Bob and belongs to nobody.
        return (None, "other_speaker")
    if is_shared_entity(low):
        return (e, "shared") if allow_shared else (None, "shared_write_refused")
    return (f"{own}{SEP}{e}", "nested")


def turn_speaker_label(identity: "TurnIdentity | None" = None) -> str:
    """How to name the human in an indexed conversation turn.

    Conversation turns were indexed as "Marcus said: ..." unconditionally, which
    made every guest sentence retrievable as something Marcus had said — a
    fabricated quote, not merely a misfiled one.
    """
    ident = identity or current_identity()
    if ident.is_owner:
        return "Marcus"
    if ident.is_known_other and ident.display_name:
        return ident.display_name
    return "An unidentified speaker"


def personal_scope_note(identity: "TurnIdentity | None" = None) -> str:
    """One line for diagnostics/logs describing the active read scope."""
    ident = identity or current_identity()
    if ident.is_owner:
        return "owner: full personal memory"
    if ident.is_known_other:
        return f"guest {ident.display_name or ident.profile_id}: own namespace + shared"
    return "unverified: shared knowledge only"
