from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from core.turn_identity import TurnIdentity


@dataclass(frozen=True)
class MemoryIngestEvent:
    conversation_id: UUID
    user_message: str
    assistant_message: str
    timestamp: datetime
    policy_memory_facts: list[dict[str, Any]] = field(default_factory=list)
    #: Who said `user_message` (V3 P5.1d). Snapshotted where the turn ran,
    #: because this event is handled later, on a worker task that never entered
    #: `active_turn` — a ContextVar would read the typed default there and file
    #: every guest's words under Marcus. Defaults to None so the worker can tell
    #: "pre-P5.1d event" from "identity resolved to nobody"; None is treated as
    #: legacy owner semantics, which is what those events actually were.
    identity: TurnIdentity | None = None


@dataclass(frozen=True)
class SummarizeHintEvent:
    conversation_id: UUID
    timestamp: datetime
    reason: str = "periodic"


@dataclass(frozen=True)
class AutonomyHintEvent:
    conversation_id: UUID
    timestamp: datetime
    hint: str


@dataclass(frozen=True)
class EpisodicPersistEvent:
    """One thing that happened, on its way to durable memory (V3 P4.1).

    Carries the LIVE artifact objects rather than their ids. The hot store is
    bounded and evicts, so an id could be dangling by the time the worker drains
    the queue — and re-capturing from the tool result would be a second capture
    path, which is exactly what P4.1 must not create.

    `artifact_id` is the parent's id, generated once at capture. It is the
    stable identity that makes persistence idempotent: the same logical event
    observed twice writes the same episode row twice, not two episodes.
    """

    # A string, not a UUID: this comes off the artifact, which stores the
    # conversation id as text. Annotating it UUID would be a quiet lie.
    conversation_id: UUID | str
    turn_id: str
    timestamp: datetime
    # Artifact-backed events (P4.1) carry the live objects. Events that are not
    # artifact-backed — a correction, a project milestone, a recurring failure —
    # carry None and describe themselves in the fields below. Both shapes go
    # through the same queue and the same worker (V3 P4.2).
    artifact: Any = None                # memory.artifacts.Artifact (the parent)
    children: list[Any] = field(default_factory=list)
    user_text: str = ""
    reason: str = ""
    kind: str = "tool_result"
    project: str | None = None
    importance: float = 0.5

    # -- non-artifact events (V3 P4.2) ---------------------------------------
    #: Stable identity. REQUIRED when `artifact` is None, because there is no
    #: artifact id to derive one from and a random id would make every
    #: redelivery a new episode.
    episode_id: str | None = None
    summary: str = ""
    entities: list[str] = field(default_factory=list)
    trust: str = ""
    freshness: str = ""
    source_tool: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None
    #: Episode ids to reinforce alongside this write — how a selection credits
    #: the result set it came from instead of duplicating it.
    reinforce: list[str] = field(default_factory=list)
    #: Result-set id whose EARLIER decisions this one replaces (V3 P4.2.1).
    #: Set only for selections. Scoped to the choice context, so an unrelated
    #: comparison's live choice is untouched.
    supersede_scope: str | None = None


@dataclass(frozen=True)
class AssistantPostEvent:
    conversation_id: UUID
    timestamp: datetime
    message: str
    reason: str = "autonomy"
