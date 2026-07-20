from __future__ import annotations

"""Semantic world model (Goal #11, Phase 4).

A store of GENERAL knowledge — subject → predicate → object triples with a
confidence and a SOURCE — kept deliberately separate from the personal knowledge
graph (memory/graph.py). The graph answers "how is X connected to *Marcus's*
world"; this answers "what is *true about the world*" ("Python is-a programming
language", "Anthropic makes Claude"). Web findings update it with the URL as
source, so Nova can consult it before re-searching the web.

Honesty rules (do not weaken):
- Every triple carries a source and confidence. Nothing is stored without a
  provenance string — an unsourced "fact" is an assumption, not world knowledge.
- Re-observing a triple reinforces it (confidence up, last_confirmed_at touched)
  rather than duplicating.
- Staleness is tracked (last_confirmed_at) so old world facts can be flagged for
  re-verification instead of being trusted forever.

The table itself is created by memory/backends/sqlite_backend.py (schema v4);
this class only reads/writes it, exactly like GraphStore does for `edges`.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from core.logging_setup import get_logger

logger = get_logger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())[:200]


class WorldModel:
    def __init__(self, db_path) -> None:
        self._db_path = db_path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def upsert(self, subject: str, predicate: str, obj: str, *, confidence: float = 0.6, source: str = "") -> bool:
        """Insert or reinforce a world triple. Requires a non-empty source —
        world knowledge without provenance is refused, by design. Returns False
        if the triple was degenerate or unsourced."""
        subj, pred = _norm(subject), _norm(predicate)
        o = (obj or "").strip()
        src = (source or "").strip()
        if not subj or not pred or not o or not src:
            return False
        now = self._now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO world_model(id, subject, predicate, object, confidence, source, created_at, last_confirmed_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject, predicate, object) DO UPDATE SET
                    confidence = MIN(0.97, confidence + 0.05),
                    source = excluded.source,
                    last_confirmed_at = excluded.last_confirmed_at
                """,
                (uuid4().hex, subj, pred, o[:2000], float(confidence), src[:400], now, now),
            )
            await db.commit()
        return True

    async def query_subject(self, subject: str, *, limit: int = 40) -> list[dict[str, Any]]:
        """Everything known about a subject, most-confident first."""
        subj = _norm(subject)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT subject, predicate, object, confidence, source, created_at, last_confirmed_at "
                "FROM world_model WHERE subject = ? ORDER BY confidence DESC, last_confirmed_at DESC LIMIT ?",
                (subj, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def search(self, term: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Keyword search across subject/predicate/object."""
        like = f"%{_norm(term)}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT subject, predicate, object, confidence, source, last_confirmed_at FROM world_model "
                "WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ? "
                "ORDER BY confidence DESC LIMIT ?",
                (like, like, like, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def is_fresh(self, subject: str, *, max_age_days: float = 30.0) -> bool:
        """True if we hold at least one recently-confirmed fact about `subject`
        — used to decide whether Nova can answer from the world model instead of
        re-searching the web."""
        rows = await self.query_subject(subject, limit=1)
        if not rows:
            return False
        anchor = rows[0].get("last_confirmed_at") or rows[0].get("created_at")
        try:
            dt = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=max_age_days)

    async def stale_subjects(self, *, older_than_days: float = 90.0, limit: int = 50) -> list[dict[str, Any]]:
        """World facts whose last confirmation is old — a future re-verification
        pass would refresh these against the web."""
        before = (datetime.now(timezone.utc) - timedelta(days=float(older_than_days))).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT subject, predicate, object, source, last_confirmed_at FROM world_model "
                "WHERE last_confirmed_at < ? ORDER BY last_confirmed_at ASC LIMIT ?",
                (before, int(limit)),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(1), COUNT(DISTINCT subject) FROM world_model") as cur:
                row = await cur.fetchone()
        return {"triples": int(row[0] or 0), "subjects": int(row[1] or 0)}
