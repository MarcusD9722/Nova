from __future__ import annotations

"""Turn identity and cancellation.

Nova's async work — generation, tool calls, memory lookups, synthesis, playback
— all outlives the moment it was requested. Without a turn identity, "the user
interrupted" has no way to mean "and therefore none of that work counts any
more", and a clip synthesised for turn 105 will happily play over turn 106's
answer.

So every asynchronous artefact carries a `turn_id`, and exactly one rule
governs it:

    Nothing produced for a cancelled turn may become visible or audible.

The registry is the authority on which turns are cancelled. It is deliberately
small, synchronous and dependency-free: cancellation checks happen on the hot
path, and anything that needs a lock or an await would be tempting to skip.
"""

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

#: How many finished turns stay queryable. Late results can arrive seconds after
#: a turn ends; beyond this horizon they are not "late", they are a bug.
_HISTORY = 64


@dataclass
class SpokenSegment:
    """A piece of text Nova actually sent to the voice, and when."""
    text: str
    queued_at: float
    spoken_at: float | None = None
    audio_ms: float | None = None


@dataclass
class Turn:
    turn_id: str
    conversation_id: str
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    cancelled: bool = False
    cancel_reason: str = ""
    spoken: list[SpokenSegment] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.ended_at is None and not self.cancelled

    def spoken_text(self) -> str:
        return " ".join(s.text for s in self.spoken)


class TurnRegistry:
    """Which turn is live, which are dead, and what each one said."""

    def __init__(self, history: int = _HISTORY) -> None:
        self._turns: OrderedDict[str, Turn] = OrderedDict()
        self._active: dict[str, str] = {}  # conversation_id -> turn_id
        self._history = history

    def start(self, conversation_id: str, *, turn_id: str | None = None) -> Turn:
        """Open a new turn, superseding whatever was live in that conversation.

        Superseding rather than rejecting is deliberate: a user who starts
        talking again has, by that act, ended the previous turn. The old turn is
        marked cancelled so its in-flight work stops counting immediately.
        """
        conversation_id = str(conversation_id)
        previous = self._active.get(conversation_id)
        if previous is not None:
            self.cancel(previous, reason="superseded")

        turn = Turn(turn_id=turn_id or uuid.uuid4().hex, conversation_id=conversation_id)
        self._turns[turn.turn_id] = turn
        self._active[conversation_id] = turn.turn_id
        while len(self._turns) > self._history:
            self._turns.popitem(last=False)
        return turn

    def get(self, turn_id: str) -> Turn | None:
        return self._turns.get(str(turn_id))

    def active_turn(self, conversation_id: str) -> Turn | None:
        turn_id = self._active.get(str(conversation_id))
        if turn_id is None:
            return None
        turn = self._turns.get(turn_id)
        return turn if turn is not None and turn.active else None

    def cancel(self, turn_id: str, reason: str = "user_interrupt") -> bool:
        turn = self._turns.get(str(turn_id))
        if turn is None or turn.cancelled:
            return False
        turn.cancelled = True
        turn.cancel_reason = reason
        turn.ended_at = time.monotonic()
        if self._active.get(turn.conversation_id) == turn.turn_id:
            self._active.pop(turn.conversation_id, None)
        return True

    def cancel_active(self, conversation_id: str, reason: str = "user_interrupt") -> str | None:
        """Cancel whatever is live in a conversation. Returns its id, if any."""
        turn = self.active_turn(conversation_id)
        if turn is None:
            return None
        self.cancel(turn.turn_id, reason=reason)
        return turn.turn_id

    def finish(self, turn_id: str) -> None:
        turn = self._turns.get(str(turn_id))
        if turn is None:
            return
        if turn.ended_at is None:
            turn.ended_at = time.monotonic()
        if self._active.get(turn.conversation_id) == turn.turn_id:
            self._active.pop(turn.conversation_id, None)

    def is_cancelled(self, turn_id: str) -> bool:
        """Unknown turns count as cancelled.

        A turn id we have never seen, or one already evicted from history, is
        not something we can vouch for — and the failure mode of speaking a
        stale clip is worse than the failure mode of dropping one.
        """
        if not turn_id:
            return False
        turn = self._turns.get(str(turn_id))
        if turn is None:
            return True
        return turn.cancelled

    def record_spoken(self, turn_id: str, text: str, *, audio_ms: float | None = None) -> None:
        """Remember what the voice was given, for echo suppression."""
        turn = self._turns.get(str(turn_id))
        if turn is None or not text.strip():
            return
        turn.spoken.append(SpokenSegment(text=text.strip(), queued_at=time.monotonic(),
                                         audio_ms=audio_ms))

    def mark_playing(self, turn_id: str, text: str) -> None:
        turn = self._turns.get(str(turn_id))
        if turn is None:
            return
        for seg in turn.spoken:
            if seg.text == text and seg.spoken_at is None:
                seg.spoken_at = time.monotonic()
                return

    def recent_spoken(self, *, within_s: float = 12.0, conversation_id: str | None = None
                      ) -> list[SpokenSegment]:
        """Segments Nova voiced recently — the candidate source of any echo."""
        now = time.monotonic()
        out: list[SpokenSegment] = []
        for turn in self._turns.values():
            if conversation_id is not None and turn.conversation_id != str(conversation_id):
                continue
            for seg in turn.spoken:
                reference = seg.spoken_at if seg.spoken_at is not None else seg.queued_at
                if now - reference <= within_s:
                    out.append(seg)
        return out

    def stats(self) -> dict[str, int]:
        return {
            "tracked": len(self._turns),
            "active": len(self._active),
            "cancelled": sum(1 for t in self._turns.values() if t.cancelled),
        }
