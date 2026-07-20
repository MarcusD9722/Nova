from __future__ import annotations

"""Persistent internal thoughts (Goal #6, Phase 4).

Nova's ongoing PRIVATE notes-to-self that survive across sessions: ideas,
unresolved questions, potential improvements, interesting discoveries, failed
experiments, future plans. They persist and can inform planning, but are
surfaced only when Marcus explicitly asks — never injected into ordinary
replies, never a way to nag.

Honesty: thoughts are Nova's own reflections, not facts about the world or
Marcus. They live in their own `thoughts` table (schema v4) and are clearly
labeled as internal. This class only reads/writes that table (created by
memory/backends/sqlite_backend.py), mirroring GraphStore / WorldModel.
"""

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from core.logging_setup import get_logger

logger = get_logger(__name__)

# The kinds of thought the roadmap calls for. `note` is a generic catch-all.
KINDS = frozenset({
    "idea", "question", "unresolved", "improvement", "discovery",
    "failed_experiment", "future_plan", "project", "research", "note",
})


def normalize_kind(kind: str | None) -> str:
    k = re.sub(r"[^a-z_]+", "_", (kind or "").strip().lower()).strip("_")
    return k if k in KINDS else "note"


class ThoughtStore:
    def __init__(self, db_path) -> None:
        self._db_path = db_path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def add(self, kind: str, topic: str, content: str, *, status: str = "open") -> str:
        """Record a thought. A near-identical OPEN thought of the same kind is
        refreshed (updated_at touched) rather than duplicated, so a recurring
        reflection doesn't pile up copies. Returns the thought id ('' if empty)."""
        text = (content or "").strip()
        if not text:
            return ""
        k = normalize_kind(kind)
        topic = (topic or "general").strip()[:120]
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id FROM thoughts WHERE kind = ? AND status = 'open' AND content = ? LIMIT 1",
                (k, text[:2000]),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute("UPDATE thoughts SET updated_at = ? WHERE id = ?", (self._now(), existing["id"]))
                await db.commit()
                return str(existing["id"])
            tid = uuid4().hex
            now = self._now()
            await db.execute(
                "INSERT INTO thoughts(id, kind, topic, content, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (tid, k, topic, text[:2000], (status or "open"), now, now),
            )
            await db.commit()
            return tid

    async def list(self, *, kind: str | None = None, status: str | None = "open", limit: int = 20) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(normalize_kind(kind))
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT id, kind, topic, content, status, created_at, updated_at FROM thoughts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def recall(self, *, topic: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Open thoughts, optionally filtered to a topic/content keyword."""
        if not topic:
            return await self.list(status="open", limit=limit)
        like = f"%{topic.strip().lower()}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, kind, topic, content, status, created_at, updated_at FROM thoughts "
                "WHERE status = 'open' AND (LOWER(topic) LIKE ? OR LOWER(content) LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (like, like, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def resolve(self, thought_id: str, *, status: str = "resolved") -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE thoughts SET status = ?, updated_at = ? WHERE id = ?",
                (status, self._now(), thought_id),
            )
            await db.commit()

    async def stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(1) FROM thoughts WHERE status='open'") as cur:
                open_n = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(1) FROM thoughts") as cur:
                total = (await cur.fetchone())[0]
        return {"open": int(open_n or 0), "total": int(total or 0)}
