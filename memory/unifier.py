from __future__ import annotations

import asyncio

import aiosqlite
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.logging_setup import get_logger
from memory.backends.chroma_backend import ChromaMemoryBackend
from memory.backends.diskcache_backend import DiskCacheBackend
from memory.backends.json_backend import JsonAuditBackend
from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.schemas import FactRecord, MemoryHit


logger = get_logger(__name__)

IGNORE_TURN_KINDS = {"turn", "turn_user", "turn_assistant"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryUnifier:
    def __init__(self, memory_dir: Path):
        self._dir = memory_dir
        self._sqlite = SQLiteMemoryBackend(memory_dir / "sqlite" / "nova.sqlite3")
        self._diskcache = DiskCacheBackend(memory_dir / "diskcache")
        self._chroma = ChromaMemoryBackend(memory_dir / "chroma")
        self._json = JsonAuditBackend(memory_dir / "json")
        self._write_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._sqlite.initialize()
            await self._json.initialize()

            # Best-effort: ensure semantic index exists after cold start.
            try:
                await self._ensure_semantic_index()
            except Exception as e:  # noqa: BLE001
                logger.debug("semantic_index_ensure_failed", error=str(e))

            self._initialized = True

    async def _ensure_semantic_index(self) -> None:
        """Ensure Chroma has at least the stable facts/people/events.

        SQLite is the source-of-truth. If Chroma is empty (e.g., deleted), rebuild it.
        """
        sqlite_counts = await self._sqlite.count_records()
        try:
            chroma_count = await self._chroma.count()
        except Exception:
            chroma_count = 0

        # If there is nothing stable to index, don't touch Chroma.
        stable_total = int(sqlite_counts.get("facts", 0)) + int(sqlite_counts.get("people", 0)) + int(sqlite_counts.get("events", 0))
        if stable_total <= 0:
            return

        # Only rebuild when Chroma looks empty.
        if chroma_count <= 0:
            await self.rebuild_semantic_index()

    async def rebuild_semantic_index(self) -> dict[str, int]:
        """Rebuild Chroma documents from SQLite facts/people/events."""
        await self._sqlite.initialize()
        await self._json.initialize()

        await self._chroma.reset()

        facts = await self._sqlite.all_facts(limit=None)
        people = await self._sqlite.all_people(limit=None)
        events = await self._sqlite.all_events(limit=None)

        # Upsert in small batches to avoid large thread payloads.
        fact_n = 0
        for row in facts:
            fact_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"FACT {row['entity']} {row['attribute']} = {row['value']}",
                metadata={
                    "kind": "fact",
                    "entity": str(row["entity"]),
                    "attribute": str(row["attribute"]),
                    "created_at": str(row.get("created_at") or ""),
                },
            )

        person_n = 0
        for row in people:
            person_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"PERSON {row['name']} {row['attributes_json']}",
                metadata={"kind": "person", "name": str(row["name"]), "created_at": str(row.get("created_at") or "")},
            )

        event_n = 0
        for row in events:
            event_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"EVENT {row['date']}: {row['note']}",
                metadata={"kind": "event", "date": str(row["date"]), "created_at": str(row.get("created_at") or "")},
            )

        await self._json.append_snapshot(
            {"kind": "semantic_index_rebuild", "facts": fact_n, "people": person_n, "events": event_n, "ts": _now().isoformat()}
        )
        return {"facts": fact_n, "people": person_n, "events": event_n}

    async def ingest_turn(self, conversation_id: UUID, role: str, content: str) -> UUID:
        await self.initialize()
        turn_id = uuid4()
        created_at = _now().isoformat()

        async with self._write_lock:
            await asyncio.gather(
                self._sqlite.add_turn(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    created_at_iso=created_at,
                ),
                self._json.append_audit(
                    {
                        "kind": "turn",
                        "id": str(turn_id),
                        "conversation_id": str(conversation_id),
                        "role": role,
                        "content": content,
                        "created_at": created_at,
                    }
                ),
                self._diskcache.set(
                    f"turn:{turn_id}",
                    {"conversation_id": str(conversation_id), "role": role, "content": content, "created_at": created_at},
                    ttl_s=86400,
                ),
            )

        return turn_id

    async def add_fact(self, entity: str, attribute: str, value: str, confidence: float = 0.7) -> UUID:

        # ---- write guard: prevent storing non-name tokens as relationship values ----
        deny_by_attr = {
            "spouse": {"name", "names", "spouse", "wife", "husband", "partner"},
            "child": {"name", "names", "child", "children", "kid", "kids", "son", "sons", "daughter", "daughters"},
            "parent": {"name", "names", "parent", "parents", "mom", "mother", "dad", "father"},
            "mother": {"name", "names", "mom", "mother"},
            "father": {"name", "names", "dad", "father"},
            "sibling": {"name", "names", "sibling", "siblings", "brother", "brothers", "sister", "sisters"},
            "cousin": {"name", "names", "cousin", "cousins"},
            "friend": {"name", "names", "friend", "friends", "buddy", "buddies"},
            "pet": {"name", "names", "pet", "pets", "dog", "dogs", "cat", "cats"},
            "coworker": {"name", "names", "coworker", "coworkers", "colleague", "colleagues"},
        }
        attr_key = (attribute or "").strip().lower()
        v_raw = str(value).strip() if value is not None else ""
        v_norm = v_raw.lower()
        if attr_key in deny_by_attr:
            if (not v_raw) or (v_norm in deny_by_attr[attr_key]) or (len(v_raw) < 2):
                logger.debug("fact_rejected_denylist", entity=entity, attribute=attribute, value=value)
                return uuid4()  # preserve return type while skipping write
            if not any(ch.isalpha() for ch in v_raw):
                logger.debug("fact_rejected_denylist", entity=entity, attribute=attribute, value=value)
                return uuid4()

        await self.initialize()
        fact_id = uuid4()
        created_at = _now().isoformat()

        async with self._write_lock:
            await asyncio.gather(
                self._sqlite.add_fact(fact_id, entity=entity, attribute=attribute, value=value, confidence=confidence),
                self._json.append_audit(
                    {
                        "kind": "fact",
                        "id": str(fact_id),
                        "entity": entity,
                        "attribute": attribute,
                        "value": value,
                        "confidence": confidence,
                        "created_at": created_at,
                    }
                ),
                self._chroma.upsert_text(
                    doc_id=str(fact_id),
                    text=f"FACT {entity} {attribute} = {value}",
                    metadata={"kind": "fact", "entity": entity, "attribute": attribute, "created_at": created_at},
                ),
                self._diskcache.set(
                    f"fact:{fact_id}",
                    {"entity": entity, "attribute": attribute, "value": value, "confidence": confidence, "created_at": created_at},
                    ttl_s=86400,
                ),
            )

        return fact_id

    async def upsert_person(self, name: str, attributes: dict[str, str]) -> UUID:
        await self.initialize()
        person_id = uuid4()
        created_at = _now().isoformat()
        attributes_json = json.dumps(attributes, ensure_ascii=False, sort_keys=True)

        async with self._write_lock:
            await asyncio.gather(
                self._sqlite.upsert_person(person_id, name=name, attributes_json=attributes_json),
                self._json.append_audit(
                    {
                        "kind": "person",
                        "id": str(person_id),
                        "name": name,
                        "attributes": attributes,
                        "created_at": created_at,
                    }
                ),
                self._chroma.upsert_text(
                    doc_id=str(person_id),
                    text=f"PERSON {name} {attributes_json}",
                    metadata={"kind": "person", "name": name, "created_at": created_at},
                ),
                self._diskcache.set(f"person:{name.lower()}", attributes, ttl_s=86400),
            )

        return person_id

    async def add_event(self, date: str, note: str) -> UUID:
        await self.initialize()
        event_id = uuid4()
        created_at = _now().isoformat()

        async with self._write_lock:
            await asyncio.gather(
                self._sqlite.add_event(event_id, date=date, note=note),
                self._json.append_audit(
                    {
                        "kind": "event",
                        "id": str(event_id),
                        "date": date,
                        "note": note,
                        "created_at": created_at,
                    }
                ),
                self._chroma.upsert_text(
                    doc_id=str(event_id),
                    text=f"EVENT {date}: {note}",
                    metadata={"kind": "event", "date": date, "created_at": created_at},
                ),
                self._diskcache.set(
                    f"event:{event_id}",
                    {"date": date, "note": note, "created_at": created_at},
                    ttl_s=86400,
                ),
            )

        return event_id

    def _fact_from_row(self, row: dict[str, Any]) -> FactRecord:
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_dt = _now()
        elif isinstance(created_at, datetime):
            created_dt = created_at
        else:
            created_dt = _now()

        return FactRecord(
            id=UUID(str(row["id"])),
            entity=str(row["entity"]),
            attribute=str(row["attribute"]),
            value=str(row["value"]),
            confidence=float(row.get("confidence", 0.7)),
            created_at=created_dt,
        )

    async def get_facts(
        self,
        entity: str,
        attribute: str | None = None,
        limit: int = 25,
        newest_first: bool = True,
    ) -> list[FactRecord]:
        """Deterministic fact retrieval (NOT semantic search)."""
        await self.initialize()
        ent = (entity or "").strip()
        attr = (attribute or "").strip() if attribute else None
        order = "DESC" if newest_first else "ASC"

        where = "WHERE entity = ?"
        params: list[Any] = [ent]
        if attr:
            where += " AND attribute = ?"
            params.append(attr)

        sql = (
            "SELECT id, entity, attribute, value, confidence, created_at "
            f"FROM facts {where} ORDER BY created_at {order} LIMIT ?"
        )
        params.append(int(limit))

        async with aiosqlite.connect(self._sqlite._db_path) as db:  # type: ignore[attr-defined]
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()

        return [self._fact_from_row(dict(r)) for r in rows]

    async def get_latest_fact(self, entity: str, attribute: str) -> FactRecord | None:
        hits = await self.get_facts(entity=entity, attribute=attribute, limit=1, newest_first=True)
        return hits[0] if hits else None

    async def purge_facts(
        self,
        entity: str,
        attribute: str | None = None,
        value_in: list[str] | None = None,
        value_ilike: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Delete facts matching simple filters (SQLite only)."""
        await self.initialize()
        ent = (entity or "").strip()
        attr = (attribute or "").strip() if attribute else None
        vin = [v.strip() for v in (value_in or []) if isinstance(v, str) and v.strip()]
        vilike = (value_ilike or "").strip() or None

        where = ["entity = ?"]
        params: list[Any] = [ent]
        if attr:
            where.append("attribute = ?")
            params.append(attr)
        if vin:
            placeholders = ",".join(["?"] * len(vin))
            where.append(f"value IN ({placeholders})")
            params.extend(vin)
        if vilike:
            where.append("value LIKE ?")
            params.append(vilike.replace("*", "%"))

        where_sql = " AND ".join(where)
        select_sql = f"SELECT id FROM facts WHERE {where_sql}"
        delete_sql = f"DELETE FROM facts WHERE {where_sql}"

        async with aiosqlite.connect(self._sqlite._db_path) as db:  # type: ignore[attr-defined]
            db.row_factory = aiosqlite.Row
            async with db.execute(select_sql, tuple(params)) as cur:
                id_rows = await cur.fetchall()
            ids = [str(r["id"]) for r in id_rows]
            matched = len(ids)
            deleted = 0
            if not dry_run and matched:
                await db.execute(delete_sql, tuple(params))
                await db.commit()
                deleted = matched

        return {
            "entity": ent,
            "attribute": attr,
            "value_in": vin,
            "value_ilike": vilike,
            "dry_run": bool(dry_run),
            "matched": matched,
            "deleted": deleted,
            "ids": ids,
        }

    async def search(self, q: str, conversation_id: UUID | None = None, limit: int = 12) -> list[MemoryHit]:
        await self.initialize()
        q = (q or "").strip()
        if not q:
            return []

        # Normalize query into searchable terms (helps simple LIKE-based backends).
        q_norm = q.lower()
        terms = [t for t in re.findall(r"[a-z0-9']+", q_norm) if len(t) >= 3]

        # Keep a few high-signal short terms.
        if "ai" in q_norm:
            terms.append("ai")
        if "id" in q_norm:
            terms.append("id")

        # De-duplicate while preserving order.
        seen: set[str] = set()
        terms = [t for t in terms if not (t in seen or seen.add(t))]

        cache_key = f"search:{conversation_id}:{'|'.join(terms) or q_norm}:{limit}"
        cached = await self._diskcache.get(cache_key)
        if isinstance(cached, list) and cached:
            return [MemoryHit.model_validate(x) for x in cached]

        # Gather signals (run LIKE searches over multiple terms and merge)
        recent = await self._sqlite.recent_turns(conversation_id=conversation_id, limit=60)

        fact_rows: list[dict[str, Any]] = []
        people_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []

        if terms:
            for t in terms[:8]:
                fact_rows.extend(await self._sqlite.search_facts(t, limit=12))
                people_rows.extend(await self._sqlite.search_people(t, limit=8))
                event_rows.extend(await self._sqlite.search_events(t, limit=8))
        else:
            fact_rows = await self._sqlite.search_facts(q, limit=12)
            people_rows = await self._sqlite.search_people(q, limit=8)
            event_rows = await self._sqlite.search_events(q, limit=8)

        # Semantic recall is optional; do not fail the request.
        try:
            chroma_hits = await self._chroma.query(q, limit=limit)
        except Exception as e:
            logger.debug("chroma_query_failed", error=str(e))
            chroma_hits = []

        hits: list[MemoryHit] = []

        # --- Recent turns (STRICT FILTERING) ---
        # Only include recent turns for longer, contextual queries; this prevents
        # irrelevant recent chatter (e.g., e2e "hello" spam) from polluting the prompt.
        if len(q_norm) >= 8 and terms:
            now = _now()
            recent_terms = set(terms)
            for row in recent:
                content_l = (row.get("content") or "").lower()

                # Require strong lexical overlap.
                if not any(t in content_l for t in recent_terms):
                    continue

                created_at = datetime.fromisoformat(row["created_at"])
                age_s = max(1.0, (now - created_at).total_seconds())

                # Hard cap: ignore turns older than 2 hours.
                if age_s > 7200:
                    continue

                recency_score = 1.0 / (1.0 + age_s / 1800.0)
                hits.append(
                    MemoryHit(
                        id=row["id"],
                        kind="turn",
                        text=f"{row['role']}: {row['content']}",
                        score=0.15 * recency_score,
                        provenance={"backend": "sqlite", "table": "turns", "conversation_id": row["conversation_id"]},
                    )
                )

        # Facts (highest priority for identity and stable attributes)
        for row in fact_rows:
            text = f"FACT {row['entity']} {row['attribute']} = {row['value']}"
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="fact",
                    text=text,
                    score=0.95,
                    provenance={"backend": "sqlite", "table": "facts"},
                )
            )

        # People (very high priority)
        for row in people_rows:
            text = f"PERSON {row['name']} {row['attributes_json']}"
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="person",
                    text=text,
                    score=0.90,
                    provenance={"backend": "sqlite", "table": "people"},
                )
            )

        # Events
        for row in event_rows:
            text = f"EVENT {row['date']}: {row['note']}"
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="event",
                    text=text,
                    score=0.60,
                    provenance={"backend": "sqlite", "table": "events"},
                )
            )

        # Chroma: distance is cosine distance; convert to similarity-ish
        for ch in chroma_hits:
            meta = ch.get("metadata") or {}
            # Never treat raw turns as long-term semantic memory.
            k = str(meta.get("kind", "")).lower()
            r = str(meta.get("role", "")).lower()
            if k == "turn" or k.startswith("turn") or r == "assistant":
                continue
            kind = str(meta.get("kind", "chroma"))
            dist = float(ch.get("distance", 1.0))
            sim = max(0.0, 1.0 - dist)
            hits.append(
                MemoryHit(
                    id=str(ch.get("id")),
                    kind=kind,
                    text=str(ch.get("text", "")),
                    score=0.85 * sim,
                    provenance={"backend": "chroma", **{k: str(v) for k, v in meta.items()}},
                )
            )

        # Merge by id taking max score
        merged: dict[str, MemoryHit] = {}
        for h in hits:
            prev = merged.get(h.id)
            if prev is None or h.score > prev.score:
                merged[h.id] = h

        ranked = sorted(merged.values(), key=lambda x: x.score, reverse=True)[: int(limit)]

        await self._diskcache.set(cache_key, [r.model_dump() for r in ranked], ttl_s=120)
        await self._json.append_snapshot(
            {"kind": "search", "q": q, "conversation_id": str(conversation_id) if conversation_id else None, "results": [r.model_dump() for r in ranked]}
        )

        return ranked
