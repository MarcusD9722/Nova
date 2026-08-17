from __future__ import annotations

"""Canonical shape of every semantic record: id, embedded text, metadata.

ONE builder per record class, used by BOTH live indexing and
`rebuild_semantic_index()`. The alternative — two hand-written formatters that
are supposed to agree — was tried, and it silently drifted:

  live document chunk:     "FILE notes.txt (part 1/4): ..."
  rebuilt document chunk:  "FILE notes.txt (part 1): ..."

  live turn:               id "turn:<uuid>",  text "Marcus said: ..."
  rebuilt turn:            id "<uuid>",       text "..."

Different embedded text means a different vector for the same source row, so a
"rebuild" quietly changed what recall would match. The turn case was worse than
cosmetic: the id had no `turn:` prefix, so a rebuild wrote records that live code
could neither find nor delete, under ids in the same flat namespace as facts.

The comment above the rebuild claimed "the same ids and text shape
`index_document` writes, so a rebuild is indistinguishable from live indexing."
It was not. Hence this module: the claim is now structural rather than asserted.

Metadata `created_at` is deliberately NOT set here. For facts, people, events and
turns it is the row's own creation time, which a rebuild must preserve; passing it
in keeps that explicit at both call sites.

DOCUMENT CHUNKS ARE THE ONE EXCEPTION, and the honest statement is narrower than
"identical": `document_chunks` in SQLite does not persist the Chroma metadata
`created_at`, so live indexing stamps the moment it wrote and a rebuild stamps the
moment it rebuilt. IDs and embedded text are identical; metadata structure is
identical; that one document field is intentionally volatile. Nothing reads it —
verified: no code anywhere reads `created_at` out of Chroma metadata — so it
carries no behaviour, and adding a migration to persist it would be aesthetics.
The equality test filters exactly this field and nothing else, which is why the
claim in the docs is "identical except for intentionally volatile document
`created_at`" rather than "byte-identical metadata".
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "TURN_MIN_INDEX_CHARS",
    "SemanticRecord",
    "document_chunk_record",
    "event_record",
    "fact_record",
    "person_record",
    "turn_is_indexable",
    "turn_record",
    "legacy_turn_speaker_label",
]

#: A turn shorter than this is a greeting or an acknowledgement; indexing it
#: dilutes the index without adding recall. Live indexing and rebuild MUST use
#: the same threshold or a rebuild silently changes the corpus.
TURN_MIN_INDEX_CHARS = 25


@dataclass(frozen=True)
class SemanticRecord:
    """Exactly what gets written to the semantic index for one source row."""

    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_kwargs(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "text": self.text, "metadata": self.metadata}


def fact_record(*, fact_id: Any, entity: str, attribute: str, value: Any,
                created_at: str) -> SemanticRecord:
    return SemanticRecord(
        doc_id=str(fact_id),
        text=f"FACT {entity} {attribute} = {value}",
        metadata={"kind": "fact", "entity": str(entity),
                  "attribute": str(attribute), "created_at": str(created_at)},
    )


def person_record(*, person_id: Any, name: str, attributes_json: Any,
                  created_at: str) -> SemanticRecord:
    return SemanticRecord(
        doc_id=str(person_id),
        text=f"PERSON {name} {attributes_json}",
        metadata={"kind": "person", "name": str(name), "created_at": str(created_at)},
    )


def event_record(*, event_id: Any, date: str, note: str,
                 created_at: str) -> SemanticRecord:
    return SemanticRecord(
        doc_id=str(event_id),
        text=f"EVENT {date}: {note}",
        metadata={"kind": "event", "date": str(date), "created_at": str(created_at)},
    )


def turn_is_indexable(content: str | None) -> bool:
    return len((content or "").strip()) >= TURN_MIN_INDEX_CHARS


def legacy_turn_speaker_label(role: str) -> str:
    """The label for a turn stored before speaker identity existed.

    Mirrors `_turn_speaker_meta`'s own fallback and the `speaker_label` column
    default documented in the schema: every pre-P5.1d.1 row WAS Marcus.
    """
    return "Marcus" if str(role) == "user" else "Nova"


def turn_record(*, turn_id: Any, role: str, content: str, created_at: str,
                conversation_id: Any, speaker_entity: str,
                speaker_label: str = "") -> SemanticRecord:
    """A conversation turn.

    `speaker_label` is persisted in SQLite, so a rebuild reproduces the live text
    exactly. Rows predating that column carry '' and fall back to the same legacy
    label live code would have used.
    """
    label = str(speaker_label or "").strip() or legacy_turn_speaker_label(role)
    return SemanticRecord(
        doc_id=f"turn:{turn_id}",
        text=f"{label} said: {content}",
        # `speaker_entity` is what lets a read decide whose conversation this
        # was. Without it the index is a flat pile of sentences with Marcus's
        # name on all of them.
        metadata={"kind": "turn", "role": str(role), "created_at": str(created_at),
                  "conversation_id": str(conversation_id),
                  "speaker_entity": str(speaker_entity or "user")},
    )


def document_chunk_record(*, path: str, chunk_index: int, chunk_total: int,
                          text: str, created_at: str) -> SemanticRecord:
    """One chunk of an indexed file.

    `chunk_total` is part of the embedded text, so a rebuild MUST know how many
    chunks the file has - reconstructing chunks one at a time without it is what
    produced "(part 1)" against a live "(part 1/4)".
    """
    return SemanticRecord(
        doc_id=f"doc:{path}#{int(chunk_index)}",
        text=f"FILE {Path(path).name} (part {int(chunk_index) + 1}/{int(chunk_total)}): {text}",
        metadata={"kind": "document", "path": str(path),
                  "chunk_index": int(chunk_index), "created_at": str(created_at)},
    )
