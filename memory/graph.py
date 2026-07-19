from __future__ import annotations

"""Knowledge graph over Nova's existing memory (Phase 1.1 of docs/ROADMAP.md).

Typed edges between things she already stores — people, projects, facts,
events, documents — so "how is X connected to Y" becomes answerable instead
of everything living as disconnected rows. Design points:

- SQLite `edges` table in the same database (source of truth, like all
  memory). Re-observing an edge REINFORCES it (weight +1, confidence nudged
  up, last_reinforced_at touched) rather than duplicating.
- Extraction is deterministic and cheap (no LLM calls on the hot path):
  structured facts and co-mentions in conversation turns. A smarter LLM
  extraction pass can layer on later without schema changes.
- Keys are normalized lowercase; the special key "user" is Marcus himself
  (stable even if the display name changes).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from core.logging_setup import get_logger

logger = get_logger(__name__)

# user.<attribute> facts that encode a relationship to Marcus.
FAMILY_ATTR_PREDICATES = {
    "spouse": "spouse_of",
    "mother": "mother_of",
    "father": "father_of",
    "child": "child_of",
    "sibling": "sibling_of",
    "cousin": "cousin_of",
    "friend": "friend_of",
    "pet": "pet_of",
}


def norm_key(value: str) -> str:
    v = re.sub(r"\s+", " ", (value or "").strip().lower())
    return v[:120]


@dataclass(frozen=True)
class Edge:
    src_kind: str
    src_key: str
    predicate: str
    dst_kind: str
    dst_key: str


class GraphStore:
    def __init__(self, db_path) -> None:
        self._db_path = db_path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def upsert_edge(self, edge: Edge, confidence: float = 0.6) -> None:
        """Insert or reinforce. Never raises to callers on the ingest path."""
        src, dst = norm_key(edge.src_key), norm_key(edge.dst_key)
        if not src or not dst or src == dst:
            return
        now = self._now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO edges(id, src_kind, src_key, predicate, dst_kind, dst_key,
                                  weight, confidence, created_at, last_reinforced_at)
                VALUES(?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?)
                ON CONFLICT(src_kind, src_key, predicate, dst_kind, dst_key) DO UPDATE SET
                    weight = weight + 1.0,
                    confidence = MIN(0.95, confidence + 0.05),
                    last_reinforced_at = excluded.last_reinforced_at
                """,
                (uuid4().hex, edge.src_kind, src, edge.predicate, edge.dst_kind, dst,
                 float(confidence), now, now),
            )
            await db.commit()

    async def edges_for(self, key: str, *, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """All edges touching `key` (either end), strongest first."""
        k = norm_key(key)
        where = "(src_key = ? OR dst_key = ?)"
        params: list[Any] = [k, k]
        if kind:
            where += " AND (src_kind = ? OR dst_kind = ?)"
            params += [kind, kind]
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT src_kind, src_key, predicate, dst_kind, dst_key, weight, confidence, "
                f"created_at, last_reinforced_at FROM edges WHERE {where} "
                f"ORDER BY weight DESC, last_reinforced_at DESC LIMIT ?",
                (*params, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def related(self, key: str, *, limit: int = 20) -> dict[str, Any]:
        """1-hop neighbors plus weight-ranked 2-hop connections (the 'how do
        these connect' answer). Origin is excluded from its own results."""
        origin = norm_key(key)
        direct = await self.edges_for(origin, limit=limit)

        def other_end(row: dict[str, Any]) -> tuple[str, str]:
            if row["src_key"] == origin:
                return row["dst_kind"], row["dst_key"]
            return row["src_kind"], row["src_key"]

        neighbors = []
        seen: set[str] = {origin}
        for row in direct:
            nk_kind, nk = other_end(row)
            if nk in seen:
                continue
            seen.add(nk)
            neighbors.append({"kind": nk_kind, "key": nk, "predicate": row["predicate"], "weight": row["weight"]})

        two_hop: list[dict[str, Any]] = []
        for n in neighbors[:8]:
            for row in await self.edges_for(n["key"], limit=8):
                if row["src_key"] == norm_key(n["key"]):
                    fk_kind, fk = row["dst_kind"], row["dst_key"]
                else:
                    fk_kind, fk = row["src_kind"], row["src_key"]
                if fk in seen:
                    continue
                seen.add(fk)
                two_hop.append({
                    "kind": fk_kind, "key": fk, "via": n["key"],
                    "predicate": row["predicate"], "weight": min(n["weight"], row["weight"]),
                })
        two_hop.sort(key=lambda x: x["weight"], reverse=True)

        return {"key": origin, "neighbors": neighbors, "two_hop": two_hop[:limit]}

    async def stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(1), COUNT(DISTINCT src_key), COUNT(DISTINCT dst_key) FROM edges") as cur:
                row = await cur.fetchone()
        return {"edges": int(row[0] or 0), "src_nodes": int(row[1] or 0), "dst_nodes": int(row[2] or 0)}


# ── Deterministic extraction (no LLM on the hot path) ────────────────────────

_WORD_CACHE: dict[str, re.Pattern] = {}


def _name_pattern(name: str) -> re.Pattern:
    pat = _WORD_CACHE.get(name)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        _WORD_CACHE[name] = pat
    return pat


def extract_turn_edges(content: str, known_people: list[str], active_project: str | None) -> list[Edge]:
    """Edges observable from one conversation turn: people co-mentioned
    together, and people mentioned while a project is the active focus."""
    text = content or ""
    if len(text) < 8:
        return []
    mentioned = [p for p in known_people if p and len(p) >= 3 and _name_pattern(p).search(text)]
    edges: list[Edge] = []
    for i, a in enumerate(mentioned):
        for b in mentioned[i + 1:]:
            edges.append(Edge("person", a, "mentioned_with", "person", b))
    if active_project and _name_pattern(active_project.replace("-", " ")).search(text.replace("-", " ")):
        for p in mentioned:
            edges.append(Edge("person", p, "involved_in", "project", active_project))
    return edges


def fact_edge(entity: str, attribute: str, value: str) -> Edge | None:
    """A structured fact that encodes a relationship becomes an edge.
    user.child=Liam -> (person Liam) child_of (person user)."""
    if (entity or "").strip().lower() != "user":
        return None
    predicate = FAMILY_ATTR_PREDICATES.get((attribute or "").strip().lower())
    if not predicate:
        return None
    # Pet facts are stored as "Name|species" sometimes — key on the name.
    name = (value or "").split("|", 1)[0].strip()
    if not name:
        return None
    return Edge("person", name, predicate, "person", "user")


def person_relation_edge(name: str, attributes: dict[str, Any]) -> Edge | None:
    """people-table rows with a 'relation' attribute connect to Marcus."""
    relation = norm_key(str(attributes.get("relation") or ""))
    if not relation or relation == "user":
        return None
    predicate = re.sub(r"[^a-z0-9]+", "_", relation).strip("_")[:40] or "related_to"
    return Edge("person", name, predicate + "_of", "person", "user")


# ── Timeline (Phase 1.1: "what happened", time-ordered) ──────────────────────

async def build_timeline(sqlite_backend, *, about: str | None = None, days: int = 14, limit: int = 40) -> list[dict[str, Any]]:
    """Time-ordered merge of what Nova knows happened: events, daily
    conversation digests, fired reminders, and notable new facts. Honest by
    construction — only rows that actually exist, newest first."""
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    needle = norm_key(about) if about else None
    out: list[dict[str, Any]] = []

    def keep(text: str) -> bool:
        return needle is None or needle in (text or "").lower()

    for row in await sqlite_backend.search_events("", limit=200):
        if str(row.get("created_at") or "") >= since and keep(f"{row.get('date','')} {row.get('note','')}"):
            out.append({"when": row.get("created_at"), "kind": "event", "text": f"{row.get('date')}: {row.get('note')}"})

    for row in await sqlite_backend.search_facts("digest", limit=200):
        ent = str(row.get("entity") or "")
        if ent.endswith(":digest") and str(row.get("created_at") or "") >= since and keep(str(row.get("value") or "")):
            out.append({"when": row.get("created_at"), "kind": "conversation", "text": str(row.get("value") or "")[:300]})

    for row in await sqlite_backend.list_reminders(status="fired", limit=100):
        title = str(row.get("title") or "")
        if title.startswith("__nova_"):
            continue
        if str(row.get("updated_at") or "") >= since and keep(title):
            out.append({"when": row.get("updated_at"), "kind": "reminder", "text": f"Reminder fired: {title}"})

    if needle:
        for row in await sqlite_backend.search_facts(about, limit=60):
            ent = str(row.get("entity") or "")
            if ent in {"session", "mood", "wellbeing", "lesson"} or ent.startswith("conversation:"):
                continue
            if str(row.get("created_at") or "") >= since:
                out.append({
                    "when": row.get("created_at"), "kind": "fact",
                    "text": f"{ent}.{row.get('attribute')} = {str(row.get('value') or '')[:160]}",
                })

    out.sort(key=lambda x: str(x.get("when") or ""), reverse=True)
    return out[:limit]
