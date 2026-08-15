from __future__ import annotations

"""Opaque, short-lived voice-turn handles (V3 P5.1b).

This is the SAME cache P5 part 1 shipped — it has simply been moved out of
`SpeakerService`, because that was the wrong place for it.

WHY IT MOVED
------------
The cache has no dependency on ECAPA, on SQLite, or on the speaker registry. It
is a dict of small metadata records. But it lived inside `SpeakerService`, so
when the service could not be constructed at all there was nowhere to mint a
handle — and an ENABLED subsystem failure produced `attempted=False` with no
handle, which is byte-for-byte what DISABLED mode produces.

That collapse is the bug. Once attribution is wired, those two states must mean
opposite things:

    disabled     nobody asked a speaker question. Legacy Nova. Typed semantics.
    unavailable  the question WAS asked and could not be answered. Unverified
                 voice. Personal memory must not be written.

A subsystem that fails must not be able to erase the evidence that it was
supposed to run. So the record of "this was a voice turn" now outlives the thing
that classifies voices.

This is not a second speaker subsystem and not a second cache. It is one cache,
relocated so it cannot die with its consumer. `SpeakerService` delegates here.

WHAT A HANDLE IS NOT
--------------------
Not authentication, not a session, not a capability. It carries backend-derived
metadata so the browser cannot claim to be Marcus, and it grants nothing. Every
handle is opaque, single-use, expiring and bounded.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

#: How long a classified voice turn can be redeemed for its identity.
#: Short on purpose: an integrity handle, not a session.
VOICE_TURN_TTL_S = 300.0
#: Bounded so a burst of voice turns cannot grow memory without limit.
VOICE_TURN_MAX = 256


@dataclass
class VoiceTurn:
    turn_id: str
    match: Any            # core.speaker.matcher.SpeakerMatch
    created_at: float


class VoiceTurnRegistry:
    """Mint and redeem opaque voice-turn handles. Pure metadata bookkeeping."""

    def __init__(self, *, ttl_s: float = VOICE_TURN_TTL_S,
                 max_entries: int = VOICE_TURN_MAX) -> None:
        self._turns: dict[str, VoiceTurn] = {}
        self._ttl = float(ttl_s)
        self._max = int(max_entries)

    def issue(self, match: Any) -> str | None:
        """Mint a handle for any ATTEMPTED outcome.

        `attempted` is the whole discriminator (V3 P5.1a): every outcome of a
        real attempt gets a handle, including `unavailable`, and a turn nobody
        classified gets nothing. Refusing `unavailable` was what let a
        classifier failure look like typed input.
        """
        if match is None or not getattr(match, "attempted", False):
            return None
        self._sweep()
        turn_id = f"vt-{uuid.uuid4().hex[:16]}"
        self._turns[turn_id] = VoiceTurn(turn_id, match, time.monotonic())
        while len(self._turns) > self._max:
            oldest = min(self._turns.values(), key=lambda t: t.created_at)
            self._turns.pop(oldest.turn_id, None)
        return turn_id

    def redeem(self, turn_id: str | None) -> Any | None:
        """Resolve a handle back to backend-derived metadata — ONCE.

        Redemption consumes the handle: one classification backs exactly one
        chat turn. A replayable handle would let a captured id keep asserting an
        identity across later turns its owner never spoke.
        """
        if not turn_id:
            return None
        self._sweep()
        entry = self._turns.pop(str(turn_id), None)   # pop: single use
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            return None
        return entry.match

    def _sweep(self) -> None:
        now = time.monotonic()
        for tid in [t for t, e in self._turns.items()
                    if now - e.created_at > self._ttl]:
            self._turns.pop(tid, None)

    def __len__(self) -> int:
        return len(self._turns)

    def stats(self) -> dict[str, Any]:
        return {"cached": len(self._turns), "ttl_s": self._ttl, "max": self._max}


#: One registry per process. Deliberately module-level and independent of
#: SpeakerService: it must still exist when SpeakerService does not.
VOICE_TURNS = VoiceTurnRegistry()
