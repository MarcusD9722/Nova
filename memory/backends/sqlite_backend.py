from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite


class SQLiteMemoryBackend:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS turns (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    );
                    """
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_conv_created ON turns(conversation_id, created_at);")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS facts (
                        id TEXT PRIMARY KEY,
                        entity TEXT NOT NULL,
                        attribute TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_facts_entity_attr ON facts(entity, attribute);")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS people (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        attributes_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY,
                        date TEXT NOT NULL,
                        note TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);")
                await db.commit()
            self._initialized = True

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def ensure_conversation(self, conversation_id: UUID) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO conversations(id, created_at) VALUES(?, ?)",
                (str(conversation_id), self._now_iso()),
            )
            await db.commit()

    async def add_turn(self, turn_id: UUID, conversation_id: UUID, role: str, content: str, created_at_iso: str | None = None) -> None:
        await self.initialize()
        await self.ensure_conversation(conversation_id)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO turns(id, conversation_id, role, content, created_at) VALUES(?, ?, ?, ?, ?)",
                (str(turn_id), str(conversation_id), role, content, created_at_iso or self._now_iso()),
            )
            await db.commit()

    async def add_fact(self, fact_id: UUID, entity: str, attribute: str, value: str, confidence: float) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO facts(id, entity, attribute, value, confidence, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(fact_id), entity, attribute, value, float(confidence), self._now_iso()),
            )
            await db.commit()

    async def upsert_person(self, person_id: UUID, name: str, attributes_json: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO people(id, name, attributes_json, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET attributes_json=excluded.attributes_json
                """,
                (str(person_id), name, attributes_json, self._now_iso()),
            )
            await db.commit()

    async def add_event(self, event_id: UUID, date: str, note: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO events(id, date, note, created_at) VALUES(?, ?, ?, ?)",
                (str(event_id), date, note, self._now_iso()),
            )
            await db.commit()

    async def recent_turns(self, conversation_id: UUID | None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        query = "SELECT id, conversation_id, role, content, created_at FROM turns"
        params: tuple[Any, ...]
        if conversation_id is not None:
            query += " WHERE conversation_id=?"
            params = (str(conversation_id),)
        else:
            params = ()
        query += " ORDER BY created_at DESC LIMIT ?"
        params = (*params, int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_facts(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, entity, attribute, value, confidence, created_at FROM facts WHERE entity LIKE ? OR attribute LIKE ? OR value LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_people(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, attributes_json, created_at FROM people WHERE name LIKE ? OR attributes_json LIKE ? ORDER BY name LIMIT ?",
                (like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_events(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, date, note, created_at FROM events WHERE date LIKE ? OR note LIKE ? ORDER BY date DESC LIMIT ?",
                (like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


    async def count_records(self) -> dict[str, int]:
        """Return counts for durable tables used for long-term memory."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            facts = await (await db.execute("SELECT COUNT(1) FROM facts")).fetchone()
            people = await (await db.execute("SELECT COUNT(1) FROM people")).fetchone()
            events = await (await db.execute("SELECT COUNT(1) FROM events")).fetchone()
        return {
            "facts": int((facts or [0])[0] or 0),
            "people": int((people or [0])[0] or 0),
            "events": int((events or [0])[0] or 0),
        }


    async def all_facts(self, limit: int | None = None) -> list[dict[str, Any]]:
        await self.initialize()
        sql = "SELECT id, entity, attribute, value, confidence, created_at FROM facts ORDER BY created_at ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


    async def all_people(self, limit: int | None = None) -> list[dict[str, Any]]:
        await self.initialize()
        sql = "SELECT id, name, attributes_json, created_at FROM people ORDER BY name ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


    async def all_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        await self.initialize()
        sql = "SELECT id, date, note, created_at FROM events ORDER BY date ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


    async def find_fact_ids(
        self,
        *,
        entity: str,
        attribute: str,
        value_in: list[str] | None = None,
        value_ilike: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """Find fact IDs matching entity+attribute and either exact values or LIKE pattern (case-insensitive)."""
        await self.initialize()
        vals = [v.strip().lower() for v in (value_in or []) if str(v).strip()]
        pat = (str(value_ilike).strip().lower() if value_ilike is not None else "")

        if not vals and not pat:
            return []

        if pat:
            if "%" not in pat:
                pat = f"%{pat}%"
            sql = """
                SELECT id FROM facts
                WHERE entity = ?
                  AND attribute = ?
                  AND LOWER(value) LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            params: list[Any] = [entity, attribute, pat, int(limit)]
        else:
            placeholders = ",".join(["?"] * len(vals))
            sql = f"""
                SELECT id FROM facts
                WHERE entity = ?
                  AND attribute = ?
                  AND LOWER(value) IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = [entity, attribute, *vals, int(limit)]

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
        return [r[0] for r in rows if r and r[0]]

    async def delete_facts_by_ids(self, ids: list[str]) -> int:
        """Delete facts by ID. Returns number of rows deleted."""
        await self.initialize()
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        sql = f"DELETE FROM facts WHERE id IN ({placeholders})"
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, ids)
            await db.commit()
            return int(cur.rowcount or 0)
