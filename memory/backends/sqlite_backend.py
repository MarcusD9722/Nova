from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from memory.episodic_schema import EPISODIC_DDL, EPISODIC_MIGRATION


class SQLiteMemoryBackend:
    #: How many candidate rows a claimer will try before giving up for this tick.
    #: A claim can legitimately lose the race to another worker; it should then
    #: look at the next runnable task rather than return "queue empty".
    _CLAIM_ATTEMPTS = 8

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
                        -- V3 P5.1d.1: who this turn actually belongs to. The
                        -- speaker label lived only in Chroma metadata, so the
                        -- durable record — the one date-range recall reads —
                        -- could not tell Marcus's sentences from a guest's.
                        -- Defaults are the legacy answer: every row written
                        -- before speaker identity existed WAS Marcus.
                        -- Never store embeddings, similarity or audio here.
                        speaker_entity TEXT NOT NULL DEFAULT 'user',
                        speaker_label TEXT NOT NULL DEFAULT '',
                        input_source TEXT NOT NULL DEFAULT 'typed',
                        speaker_status TEXT NOT NULL DEFAULT '',
                        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    );
                    """
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_conv_created ON turns(conversation_id, created_at);")
                await self._migrate_turns_schema(db)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_speaker_created ON turns(speaker_entity, created_at);")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS facts (
                        id TEXT PRIMARY KEY,
                        entity TEXT NOT NULL,
                        attribute TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        last_reinforced_at TEXT,
                        source TEXT,
                        evidence TEXT,
                        verification_status TEXT,
                        last_confirmed_at TEXT,
                        access_count INTEGER NOT NULL DEFAULT 0,
                        last_accessed_at TEXT,
                        salience REAL NOT NULL DEFAULT 0.0
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

                # --- Reminders / scheduling (user-facing, distinct from internal worker pacing) ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS reminders (
                        reminder_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        details TEXT NOT NULL,
                        due_at TEXT NOT NULL,
                        recurrence TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    '''
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);")

                # --- Local file / photo recall (indexed documents) ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS documents (
                        path TEXT PRIMARY KEY,
                        excerpt TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        indexed_at TEXT NOT NULL
                    );
                    '''
                )

                # --- Deeper document synthesis (DS1): chunked text per file, so
                # cross-document queries can pull multiple relevant passages
                # instead of one excerpt per file. `documents` above stays the
                # per-file indexing ledger (mtime tracking, LIKE fallback). ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS document_chunks (
                        path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        PRIMARY KEY (path, chunk_index)
                    );
                    '''
                )

                # --- Knowledge graph (Phase 1.1): typed edges between the things
                # Nova already stores (people, projects, facts, events, documents).
                # Re-observing the same edge reinforces it (weight/confidence up)
                # instead of duplicating. ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS edges (
                        id TEXT PRIMARY KEY,
                        src_kind TEXT NOT NULL,
                        src_key TEXT NOT NULL,
                        predicate TEXT NOT NULL,
                        dst_kind TEXT NOT NULL,
                        dst_key TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        confidence REAL NOT NULL DEFAULT 0.6,
                        created_at TEXT NOT NULL,
                        last_reinforced_at TEXT NOT NULL
                    );
                    '''
                )
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_edges_triple ON edges(src_kind, src_key, predicate, dst_kind, dst_key);"
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_kind, src_key);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_kind, dst_key);")

                # --- Habit & pattern learning (HP1): lightweight tool-call history ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS tool_usage_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name TEXT NOT NULL,
                        called_at TEXT NOT NULL
                    );
                    '''
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tool_usage_name_time ON tool_usage_log(tool_name, called_at);")

                # --- Semantic world model (Phase 4 / #11): general knowledge
                # (subject-predicate-object) with confidence + SOURCE attribution,
                # kept separate from the personal knowledge graph so world facts
                # never pollute "who is connected to Marcus". ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS world_model (
                        id TEXT PRIMARY KEY,
                        subject TEXT NOT NULL,
                        predicate TEXT NOT NULL,
                        object TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.6,
                        source TEXT,
                        created_at TEXT NOT NULL,
                        last_confirmed_at TEXT NOT NULL
                    );
                    '''
                )
                await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_world_triple ON world_model(subject, predicate, object);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_world_subject ON world_model(subject);")

                # --- Persistent internal thoughts (Phase 4 / #6): Nova's ongoing
                # private notes (ideas/questions/improvements/discoveries) that
                # survive across sessions. Surfaced only on request. ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS thoughts (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    '''
                )
                await db.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_kind_status ON thoughts(kind, status, updated_at);")

                # --- Agentic goal/task system (Nova Hybrid Autonomy) ---
                # These tables may be introduced after an existing DB already has a `tasks` table
                # from earlier prototypes. We use best-effort migrations before creating indexes.
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS goals (
                        goal_id TEXT PRIMARY KEY,
                        project_name TEXT NOT NULL,
                        title TEXT NOT NULL,
                        objective TEXT NOT NULL,
                        success_criteria TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        -- Bumped by every cancel and resume. A decision that
                        -- was already in flight belongs to the generation it
                        -- was claimed under, and may only be applied while the
                        -- goal is still on that generation. See cancel_goal.
                        generation INTEGER NOT NULL DEFAULT 0
                    );
                    '''
                )
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        goal_id TEXT,
                        project_name TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        args_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        run_after TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        -- The goal generation this task was created under.
                        generation INTEGER NOT NULL DEFAULT 0
                    );
                    '''
                )

                # --- Autonomy task queue (ChatGPT-like background tasks) ---
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS autonomy_tasks (
                        task_id TEXT PRIMARY KEY,
                        conversation_id TEXT,
                        project_name TEXT NOT NULL,
                        title TEXT NOT NULL,
                        details TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        run_after TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        initiated_by_user INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    '''
                )
                
                # Best-effort migrations for existing DBs (older schema).
                await self._migrate_tasks_schema(db)
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS proposals (
                        proposal_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        suggestion TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        decided_at TEXT
                    );
                    '''
                )
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS progress_events (
                        event_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        acknowledged INTEGER NOT NULL
                    );
                    '''
                )

                # Best-effort migrations for existing DBs where tasks/proposals tables pre-exist but lack new columns.
                async def _table_columns(table: str) -> set[str]:
                    cols: set[str] = set()
                    async with db.execute(f"PRAGMA table_info({table});") as cur:
                        async for row in cur:
                            # row: (cid, name, type, notnull, dflt_value, pk)
                            cols.add(str(row[1]))
                    return cols

                existing_tables: set[str] = set()
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table';") as cur:
                    async for row in cur:
                        existing_tables.add(str(row[0]))

                if "tasks" in existing_tables:
                    cols = await _table_columns("tasks")
                    # Add missing columns with safe defaults (SQLite allows ADD COLUMN).
                    if "goal_id" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN goal_id TEXT;")
                    if "project_name" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN project_name TEXT NOT NULL DEFAULT 'temp';")
                    if "tool_name" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN tool_name TEXT NOT NULL DEFAULT '';")
                    if "args_json" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN args_json TEXT NOT NULL DEFAULT '{}';")
                    if "status" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'queued';")
                    if "attempts" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;")
                    if "run_after" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN run_after TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")
                    if "last_error" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN last_error TEXT NOT NULL DEFAULT '';")
                    if "result_json" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}';")
                    if "created_at" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")
                    if "updated_at" not in cols:
                        await db.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")

                if "goals" in existing_tables:
                    cols = await _table_columns("goals")
                    if "project_name" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN project_name TEXT NOT NULL DEFAULT 'temp';")
                    if "title" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN title TEXT NOT NULL DEFAULT '';")
                    if "objective" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN objective TEXT NOT NULL DEFAULT '';")
                    if "success_criteria" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN success_criteria TEXT NOT NULL DEFAULT '';")
                    if "status" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN status TEXT NOT NULL DEFAULT 'active';")
                    if "priority" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN priority INTEGER NOT NULL DEFAULT 50;")
                    if "created_at" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")
                    if "updated_at" not in cols:
                        await db.execute("ALTER TABLE goals ADD COLUMN updated_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")

                if "proposals" in existing_tables:
                    cols = await _table_columns("proposals")
                    if "project_name" not in cols:
                        await db.execute("ALTER TABLE proposals ADD COLUMN project_name TEXT NOT NULL DEFAULT 'temp';")
                    if "rationale" not in cols:
                        await db.execute("ALTER TABLE proposals ADD COLUMN rationale TEXT NOT NULL DEFAULT '';")
                    if "status" not in cols:
                        await db.execute("ALTER TABLE proposals ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';")
                    if "created_at" not in cols:
                        await db.execute("ALTER TABLE proposals ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")
                    if "decided_at" not in cols:
                        await db.execute("ALTER TABLE proposals ADD COLUMN decided_at TEXT;")

                # Indexes (safe after migrations)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_goals_project_status ON goals(project_name, status, updated_at);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks(goal_id, status, updated_at);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_runnable ON tasks(status, run_after, updated_at);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_autonomy_tasks_runnable ON autonomy_tasks(status, run_after, priority, updated_at);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_proposals_pending ON proposals(status, created_at);")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_progress_ack ON progress_events(project_name, acknowledged, created_at);")

                # --- V3 P4: episodic memory (episodes, artifacts, decisions,
                # cold_evidence). The DDL lives in memory/episodic_schema.py and
                # is referenced by BOTH this create block and migration 7, so the
                # fresh-database schema and the upgrade path cannot drift apart.
                #
                # The migration runs FIRST, and that ordering is load-bearing.
                # EPISODIC_DDL both creates `episodes` AND indexes it on
                # speaker_entity / privacy_scope. On a database predating P5.1e
                # the table already exists, so CREATE TABLE IF NOT EXISTS is a
                # no-op, those columns never appear, and the very next statement
                # indexes them — boot dies with `no such column: speaker_entity`
                # and Nova will not start. Migrating first adds the columns to
                # the old table; on a fresh database the guard inside returns
                # early (no table yet) and the DDL builds the full schema.
                # Same order as `turns` above: migrate, THEN index.
                await self._migrate_episodes_schema(db)
                for _sql in EPISODIC_DDL:
                    await db.execute(_sql)

                await self._apply_migrations(db)

                await db.commit()
            self._initialized = True

    # ---------------- Schema versioning (Phase 0.5 of docs/ROADMAP.md) ----------------
    #
    # The CREATE TABLE IF NOT EXISTS block above always builds the CURRENT full
    # schema, so a fresh database needs no migrations and is stamped at the
    # latest version. An existing database runs every migration newer than its
    # stamp, in order, each inside the surrounding transaction. Rules from here
    # on: any schema change = a new (version, description, [sql]) entry BELOW
    # plus the matching change in the create block above. Never edit or remove
    # a shipped migration.

    _MIGRATIONS: list[tuple[int, str, list[str]]] = [
        (1, "baseline: full schema as of Phase 0 (2026-07-18)", []),
        (
            2,
            "Phase 1.1/1.3: knowledge-graph edges table + facts.last_reinforced_at",
            [
                """CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    src_kind TEXT NOT NULL,
                    src_key TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    dst_kind TEXT NOT NULL,
                    dst_key TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    confidence REAL NOT NULL DEFAULT 0.6,
                    created_at TEXT NOT NULL,
                    last_reinforced_at TEXT NOT NULL
                );""",
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_edges_triple ON edges(src_kind, src_key, predicate, dst_kind, dst_key);",
                "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_kind, src_key);",
                "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_kind, dst_key);",
                "ALTER TABLE facts ADD COLUMN last_reinforced_at TEXT;",
            ],
        ),
        (
            3,
            "Phase 3.5/#19: memory provenance columns on facts (source, evidence, verification_status, last_confirmed_at)",
            [
                "ALTER TABLE facts ADD COLUMN source TEXT;",
                "ALTER TABLE facts ADD COLUMN evidence TEXT;",
                "ALTER TABLE facts ADD COLUMN verification_status TEXT;",
                "ALTER TABLE facts ADD COLUMN last_confirmed_at TEXT;",
            ],
        ),
        (
            4,
            "Phase 4/#11+#6: semantic world_model + persistent thoughts tables",
            [
                """CREATE TABLE IF NOT EXISTS world_model (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.6,
                    source TEXT,
                    created_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL
                );""",
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_world_triple ON world_model(subject, predicate, object);",
                "CREATE INDEX IF NOT EXISTS idx_world_subject ON world_model(subject);",
                """CREATE TABLE IF NOT EXISTS thoughts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );""",
                "CREATE INDEX IF NOT EXISTS idx_thoughts_kind_status ON thoughts(kind, status, updated_at);",
            ],
        ),
        (
            5,
            "Human-like memory: retrieval reinforcement (access_count/last_accessed_at) + encoding salience",
            [
                # Retrieval strengthens memory — the testing effect. Until now
                # only a duplicate WRITE reinforced a fact; recalling one did
                # nothing, so a fact Marcus refers to daily was no more durable
                # than one he never mentions again.
                "ALTER TABLE facts ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE facts ADD COLUMN last_accessed_at TEXT;",
                # How much this mattered when it was formed (emotion, surprise,
                # how directly it was stated). Salient memories decay slower —
                # the reason you remember where you were for the big moments.
                "ALTER TABLE facts ADD COLUMN salience REAL NOT NULL DEFAULT 0.0;",
                "CREATE INDEX IF NOT EXISTS idx_facts_accessed ON facts(last_accessed_at);",
            ],
        ),
        (
            6,
            "Backfill salience for facts written before v5 (mirrors unifier._default_salience)",
            [
                # Without this, every memory formed before today sits at
                # salience 0.0 and decays faster than an identical one learned
                # tomorrow — an arbitrary cliff at the upgrade date. This
                # applies the same deterministic default the unifier uses for
                # new writes, so old memories are ranked on what they ARE
                # rather than on when the feature happened to land.
                #
                # Only touches rows still at the 0.0 default, so it is safe to
                # run against a database that already has real salience values.
                """UPDATE facts SET salience = CASE
                       WHEN entity = 'user' AND attribute IN
                            ('name','location','spouse','child','children_type',
                             'mother','father','sibling','birthday','anniversary') THEN 1.0
                       WHEN entity = 'lesson'        THEN 0.9
                       WHEN entity = 'people'        THEN 0.7
                       WHEN entity LIKE 'person:%'   THEN 0.7
                       WHEN entity LIKE 'project:%'  THEN 0.5
                       WHEN entity = 'note'          THEN 0.2
                       ELSE MAX(0.0, MIN(1.0, COALESCE(confidence, 0.0))) * 0.5
                   END
                   WHERE salience IS NULL OR salience = 0.0;""",
            ],
        ),
        EPISODIC_MIGRATION,
        (
            8,
            "Goal lifecycle generation: fence decisions that were in flight across a cancel",
            [
                # WHY: a __decide__ claimed before a cancel could still be
                # applied after an immediate resume, because the goal was
                # active again by the time the model answered. The decision
                # now carries the generation it was claimed under and is
                # refused if the goal has moved on.
                "ALTER TABLE goals ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE tasks ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;",
            ],
        ),
    ]

    async def _apply_migrations(self, db: "aiosqlite.Connection") -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL);"
        )
        async with db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version") as cur:
            row = await cur.fetchone()
        current = int(row[0]) if row else 0

        latest = max(v for v, _, _ in self._MIGRATIONS)
        if current == 0:
            # Fresh DB (or a pre-versioning DB, which by definition matches
            # today's create block): stamp latest without replaying history.
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, description, applied_at) VALUES(?, ?, ?)",
                (latest, "stamped current (create block builds latest schema)", self._now_iso()),
            )
            return

        for version, description, statements in sorted(self._MIGRATIONS, key=lambda m: m[0]):
            if version <= current:
                continue
            for sql in statements:
                try:
                    await db.execute(sql)
                except Exception as e:  # noqa: BLE001
                    # `ADD COLUMN` on a column that is already there.
                    #
                    # This is inherent to the design, not a one-off: the create
                    # block above always builds the LATEST schema, so a table
                    # an older database never had is created complete — and the
                    # migration that introduces its new column then finds the
                    # column present. SQLite has no ADD COLUMN IF NOT EXISTS.
                    #
                    # Only that exact case is tolerated; every other migration
                    # error still raises, because a migration that silently
                    # half-applies is worse than one that stops.
                    if "duplicate column name" in str(e).lower():
                        continue
                    raise
            await db.execute(
                "INSERT INTO schema_version(version, description, applied_at) VALUES(?, ?, ?)",
                (version, description, self._now_iso()),
            )

    async def schema_version(self) -> int:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------------- Autonomy task queue (new contract) ----------------

    async def enqueue_autonomy_task(
        self,
        *,
        task_id: UUID,
        project_name: str,
        title: str,
        details: str,
        priority: int,
        initiated_by_user: bool,
        conversation_id: str | None = None,
        status: str = "queued",
        attempts: int = 0,
        run_after_iso: str | None = None,
    ) -> None:
        await self.initialize()
        now = self._now_iso()
        run_after = run_after_iso or now
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO autonomy_tasks(task_id, conversation_id, project_name, title, details, priority, status, attempts, run_after, last_error, result_json, initiated_by_user, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task_id),
                    (conversation_id or None),
                    project_name,
                    title,
                    details,
                    int(priority),
                    status,
                    int(attempts),
                    run_after,
                    "",
                    "{}",
                    1 if initiated_by_user else 0,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def claim_next_autonomy_task(self) -> dict[str, Any] | None:
        """Claim one background task. See claim_next_task: same defect, same fix.

        Measured before the fix: 17 claims over 10 queued tasks. A duplicated
        claim here runs a whole planner tool plan twice.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            for _ in range(self._CLAIM_ATTEMPTS):
                cur = await db.execute(
                    "SELECT task_id, conversation_id, project_name, title, details, priority, attempts, initiated_by_user FROM autonomy_tasks WHERE status='queued' AND run_after <= ? ORDER BY priority ASC, updated_at ASC LIMIT 1",
                    (now,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                claimed = await db.execute(
                    "UPDATE autonomy_tasks SET status='running', updated_at=? WHERE task_id=? AND status='queued'",
                    (now, str(row[0])),
                )
                await db.commit()
                if claimed.rowcount != 1:
                    continue
                return {
                    "task_id": row[0],
                    "conversation_id": row[1],
                    "project_name": row[2],
                    "title": row[3],
                    "details": row[4],
                    "priority": row[5],
                    "attempts": row[6],
                    "initiated_by_user": bool(int(row[7] or 0)),
                }
        return None

    async def complete_autonomy_task(
        self,
        *,
        task_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> bool:
        """Write the terminal outcome. The FIRST one wins; later ones no-op.

        Returns whether this call is the one that landed.

        Unguarded, this was `UPDATE ... WHERE task_id=?`, so any later caller
        could overwrite a finished task. That is not hypothetical here: the
        interruption handler added in this same commit runs from
        `except CancelledError`, which can fire immediately AFTER a successful
        `mark_task_done` has already committed — and would have replaced a
        genuine `done` with `failed: interrupted`. A terminal state is a fact
        about something that already happened, so the first one to be written
        is the one that is true.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE autonomy_tasks SET status=?, result_json=?, last_error=?, "
                "updated_at=? WHERE task_id=? AND status='running'",
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    (error or ""),
                    now,
                    task_id,
                ),
            )
            await db.commit()
            return int(cur.rowcount or 0) == 1

    async def block_autonomy_task(self, *, task_id: str, question: str,
                                  result: dict[str, Any] | None = None) -> bool:
        """Park a task that is waiting on a person. Returns whether it landed.

        `blocked` is a durable state, not a terminal one. It exists because the
        four statuses this table had could not express "waiting for the user":

            queued    it will run on its own. It will not; it needs an answer.
            running   a worker holds it. None does.
            done      finished. It is not, and pretending so is what made
                      "what is pending?" answer nothing.
            failed    it went wrong. Nothing went wrong.

        The autonomy worker asked its question and then called mark_task_done,
        so a task waiting on a human was filed with the completed work and the
        question was invisible to every caller that reads a task.

        A blocked row is deliberately NOT claimable (`claim` reads 'queued'
        only) and deliberately survives a restart: `cancel_pending_background_work`
        touches queued and running, and an unanswered question is still an
        unanswered question after a reboot.
        """
        await self.initialize()
        now = self._now_iso()
        payload = dict(result or {})
        payload["question"] = question
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE autonomy_tasks SET status='blocked', result_json=?, "
                "last_error=?, updated_at=? WHERE task_id=? AND status='running'",
                (json.dumps(payload, ensure_ascii=False), question, now, task_id))
            await db.commit()
            return int(cur.rowcount or 0) == 1

    async def answer_autonomy_task(self, *, task_id: str, answer: str) -> bool:
        """Give a blocked task its answer and let it run again.

        Without this, `blocked` would be a nicer-looking dead end than `done`:
        a state with no exit is not an improvement on a state that lies. The
        answer is appended to `details` so the next plan actually sees it, and
        the row goes back to 'queued' where the claim can pick it up.

        Guarded on `status='blocked'` so an answer cannot restart something
        that is running, finished, or cancelled.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE autonomy_tasks SET status='queued', run_after=?, "
                "last_error='', "
                "details = details || ? , updated_at=? "
                "WHERE task_id=? AND status='blocked'",
                (now, f"\n\nYou asked, and the answer is: {answer}", now,
                 task_id))
            await db.commit()
            return int(cur.rowcount or 0) == 1

    async def bump_autonomy_task_attempt(self, *, task_id: str, attempts: int, run_after_iso: str, error: str) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE autonomy_tasks SET status='queued', attempts=?, run_after=?, last_error=?, updated_at=? WHERE task_id=?",
                (int(attempts), run_after_iso, error, now, task_id),
            )
            await db.commit()

    async def list_autonomy_tasks(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        # `result_json` is selected because without it nothing that reads a
        # task can see the outcome the worker recorded. That is what made
        # S13B-1 invisible: the honest result was written, and no reader could
        # reach it.
        query = (
            "SELECT task_id, conversation_id, project_name, title, details, priority, status, attempts, "
            "last_error, initiated_by_user, result_json, created_at, updated_at FROM autonomy_tasks"
        )
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(query, params)
            rows = await cur.fetchall()

        return [
            {
                "task_id": r[0],
                "conversation_id": r[1],
                "project_name": r[2],
                "title": r[3],
                "details": r[4],
                "priority": r[5],
                "status": r[6],
                "attempts": r[7],
                "last_error": r[8],
                "initiated_by_user": bool(int(r[9] or 0)),
                "result_json": r[10],
                "created_at": r[11],
                "updated_at": r[12],
            }
            for r in rows
        ]

    #: A task that never left the queue provably did nothing, so saying it was
    #: cancelled is simply true.
    _STARTUP_CANCEL = (
        "UPDATE {table} SET status='cancelled', last_error=?, updated_at=? "
        "WHERE status='queued'")

    #: A task that was RUNNING when the process died is a different fact. It had
    #: been claimed, it may have invoked a tool, and nothing survived to say
    #: which. Recording it as `cancelled` asserts that it never happened, and
    #: that assertion is not one this process is in a position to make.
    _STARTUP_INTERRUPTED = (
        "UPDATE {table} SET status='failed', last_error=?, updated_at=? "
        "WHERE status='running'")

    async def cancel_pending_background_work(self) -> dict[str, int]:
        """Clear background work left over from an older session.

        This prevents stale autonomous jobs from auto-resuming on process restart
        and repeatedly invoking the model before a user asks for new work.

        Queued and running rows are NOT the same thing and are no longer written
        the same way. Both used to become `cancelled` / "cancelled_on_startup",
        so a task that had been interrupted halfway through a tool call was
        recorded exactly like one that never started — a definite claim that the
        work never happened, made about something the process could not know.
        """
        await self.initialize()
        now = self._now_iso()
        interrupted_note = (
            "interrupted by a restart while it was running. Whether its tool "
            "completed is unknown: it was claimed and may have acted before "
            "the process stopped.")
        async with aiosqlite.connect(self._db_path) as db:
            totals: dict[str, int] = {}
            interrupted = 0
            for key, table in (("autonomy_tasks", "autonomy_tasks"),
                               ("goal_tasks", "tasks")):
                cur_q = await db.execute(
                    self._STARTUP_CANCEL.format(table=table),
                    ("cancelled_on_startup", now))
                cur_r = await db.execute(
                    self._STARTUP_INTERRUPTED.format(table=table),
                    (interrupted_note, now))
                n_q, n_r = int(cur_q.rowcount or 0), int(cur_r.rowcount or 0)
                totals[key] = n_q + n_r
                interrupted += n_r
            await db.commit()
        # `interrupted` is additive: existing callers read the two totals and
        # keep working, and anything that wants to say "N were mid-flight" now
        # can.
        totals["interrupted"] = interrupted
        return totals

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _migrate_episodes_schema(self, db: aiosqlite.Connection) -> None:
        """Add speaker provenance to an existing `episodes` table, in place.

        Deliberately NOT a numbered migration. The runner stamps `latest`
        without replaying history for a fresh database — whose create block
        already builds these columns — so a versioned ALTER could only ever fire
        against a table that already had them. This is the same idempotent
        pattern `_migrate_turns_schema` uses, and for the same reason.

        `user` / `typed` is the correct backfill rather than a guess: every
        episode written before P5.1e predates live speaker activation, so all of
        it is Marcus's history.
        """
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='episodes';")
        if not await cur.fetchone():
            return
        cur = await db.execute("PRAGMA table_info(episodes);")
        have = {r[1] for r in await cur.fetchall()}
        for col, default in (("speaker_entity", "'user'"), ("speaker_label", "''"),
                             ("input_source", "'typed'"), ("actor_entity", "'user'"),
                             ("actor_label", "''"), ("privacy_scope", "'user'")):
            if col not in have:
                await db.execute(
                    f"ALTER TABLE episodes ADD COLUMN {col} TEXT NOT NULL DEFAULT {default};")
        # V3 P5.1e.1 backfill. Rows written by PR #41 carry speaker_entity and
        # nothing else, so derive BOTH new fields from it — the actor and the
        # privacy owner genuinely were the same thing for those human turns.
        # `user`/`speaker:<id>` map across unchanged; anything unrecognised
        # (including '' from a pre-P5.1e row) falls closed to owner-private,
        # because widening legacy data to shared is the one irreversible
        # mistake available here.
        if "privacy_scope" not in have:
            await db.execute(
                "UPDATE episodes SET privacy_scope = CASE "
                "  WHEN speaker_entity IN ('user','unverified') THEN speaker_entity "
                "  WHEN speaker_entity LIKE 'speaker:%' THEN speaker_entity "
                "  ELSE 'user' END")
        if "actor_entity" not in have:
            await db.execute(
                "UPDATE episodes SET actor_entity = CASE "
                "  WHEN speaker_entity IN ('user','unverified','system') THEN speaker_entity "
                "  WHEN speaker_entity LIKE 'speaker:%' THEN speaker_entity "
                "  ELSE 'user' END, "
                "actor_label = speaker_label")
        for idx in ("CREATE INDEX IF NOT EXISTS idx_episodes_speaker "
                    "ON episodes(speaker_entity, created_at DESC);",
                    "CREATE INDEX IF NOT EXISTS idx_episodes_privacy "
                    "ON episodes(privacy_scope, created_at DESC);"):
            await db.execute(idx)

    async def _migrate_turns_schema(self, db: aiosqlite.Connection) -> None:
        """Add speaker attribution to an existing `turns` table, in place.

        Every row that predates this column existed before Nova could tell who
        was speaking, and every one of them was Marcus — the frontend has never
        sent a speaker identity. So the backfill is not a guess: `user` / typed
        is what those turns actually were, and it keeps his history readable
        exactly as before. No DB deletion, no rebuild.
        """
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='turns';")
        if not await cur.fetchone():
            return
        cur = await db.execute("PRAGMA table_info(turns);")
        have = {r[1] for r in await cur.fetchall()}
        for col, default in (("speaker_entity", "'user'"), ("speaker_label", "''"),
                             ("input_source", "'typed'"), ("speaker_status", "''")):
            if col not in have:
                await db.execute(
                    f"ALTER TABLE turns ADD COLUMN {col} TEXT NOT NULL DEFAULT {default};")

    async def _migrate_tasks_schema(self, db: aiosqlite.Connection) -> None:
        """Best-effort, in-place schema migration for the tasks table.

        Older Nova DBs may have a 'tasks' table without newer columns (task_id, run_after, goal_id, etc.).
        We add missing columns and backfill values where required so the supervisor can run without manual DB deletion.
        """
        # Does tasks table exist?
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks';")
        row = await cur.fetchone()
        if not row:
            return

        cur = await db.execute("PRAGMA table_info(tasks);")
        cols = [r[1] for r in await cur.fetchall()]  # (cid, name, type, notnull, dflt_value, pk)

        async def _add_column(sql: str) -> None:
            try:
                await db.execute(sql)
            except Exception:
                # If column already exists or SQLite rejects (rare), ignore.
                return

        # Columns required by claim_next_task() and the supervisor loop.
        if "task_id" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN task_id TEXT;")
            # Backfill deterministic-ish ids for existing rows.
            await db.execute("UPDATE tasks SET task_id = lower(hex(randomblob(16))) WHERE task_id IS NULL OR task_id = ''")

        if "run_after" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN run_after TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")

        if "goal_id" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN goal_id TEXT;")

        if "project_name" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN project_name TEXT NOT NULL DEFAULT 'temp';")

        if "tool_name" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN tool_name TEXT NOT NULL DEFAULT '__decide__';")

        if "args_json" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN args_json TEXT NOT NULL DEFAULT '{}';")

        if "status" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'queued';")

        if "attempts" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;")

        if "updated_at" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")

        if "created_at" not in cols:
            await _add_column("ALTER TABLE tasks ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';")

        # Ensure task_id is unique-ish for new logic (won't change existing PK).
        try:
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_task_id_unique ON tasks(task_id);")
        except Exception:
            pass

    async def ensure_conversation(self, conversation_id: UUID) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO conversations(id, created_at) VALUES(?, ?)",
                (str(conversation_id), self._now_iso()),
            )
            await db.commit()

    async def add_turn(
        self,
        turn_id: UUID,
        conversation_id: UUID,
        role: str,
        content: str,
        created_at_iso: str | None = None,
        *,
        speaker_entity: str = "user",
        speaker_label: str = "",
        input_source: str = "typed",
        speaker_status: str = "",
    ) -> None:
        await self.initialize()
        await self.ensure_conversation(conversation_id)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO turns(id, conversation_id, role, content, created_at, "
                "speaker_entity, speaker_label, input_source, speaker_status) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(turn_id), str(conversation_id), role, content,
                 created_at_iso or self._now_iso(),
                 speaker_entity or "user", speaker_label or "",
                 input_source or "typed", speaker_status or ""),
            )
            await db.commit()

    async def add_fact(
        self,
        fact_id: UUID,
        entity: str,
        attribute: str,
        value: str,
        confidence: float,
        *,
        source: str | None = None,
        evidence: str | None = None,
        verification_status: str | None = None,
        last_confirmed_at: str | None = None,
        salience: float = 0.0,
    ) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO facts(id, entity, attribute, value, confidence, created_at, "
                "source, evidence, verification_status, last_confirmed_at, salience) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(fact_id),
                    entity,
                    attribute,
                    value,
                    float(confidence),
                    self._now_iso(),
                    source,
                    evidence,
                    verification_status,
                    last_confirmed_at,
                    max(0.0, min(1.0, float(salience))),
                ),
            )
            await db.commit()

    async def touch_facts_accessed(self, fact_ids: list[str]) -> None:
        """Record that these facts were RETRIEVED — the testing effect.

        Recalling a memory is what makes it durable in humans, far more than
        re-reading it. Until this existed, only a duplicate write reinforced a
        fact, so memory was shaped by how often something was RESTATED rather
        than by how often it actually mattered.

        Deliberately does NOT touch confidence: being reminded of something is
        not evidence that it is more true, only that it is more used. Decay
        ranking reads access_count/last_accessed_at (see unifier).
        """
        ids = [str(i) for i in (fact_ids or []) if str(i).strip()]
        if not ids:
            return
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "UPDATE facts SET access_count = COALESCE(access_count, 0) + 1, "
                "last_accessed_at = ? WHERE id = ?",
                [(now, i) for i in ids],
            )
            await db.commit()

    async def confirm_fact(self, fact_id: str, *, evidence: str | None = None) -> None:
        """Mark a fact re-verified against a fresh observation (#19): stamp
        last_confirmed_at, set status to 'confirmed', and attach evidence if
        given without clobbering any existing evidence."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE facts SET verification_status='confirmed', last_confirmed_at=?, "
                "evidence=COALESCE(?, evidence) WHERE id=?",
                (self._now_iso(), evidence, fact_id),
            )
            await db.commit()

    async def facts_needing_reverification(self, *, before_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        """Directly-observed facts whose last confirmation predates `before_iso`
        (assumptions are excluded — they are already flagged, not 'stale'). A
        future re-verification worker consumes this; kept read-only for now."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, entity, attribute, value, verification_status, last_confirmed_at, created_at "
                "FROM facts WHERE verification_status IN ('stated','observed','external','confirmed') "
                "AND COALESCE(last_confirmed_at, created_at) < ? ORDER BY COALESCE(last_confirmed_at, created_at) ASC LIMIT ?",
                (before_iso, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

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
        query = "SELECT id, conversation_id, role, content, created_at, speaker_entity, speaker_label, input_source, speaker_status FROM turns"
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

    async def search_turns(
        self,
        term: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
        conversation_id: UUID | None = None,
        limit: int = 30,
        speaker_entity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve turns by keyword and/or created_at range (ISO strings sort
        lexicographically). Enables 'what did we talk about last Tuesday' and
        keyword recall over the full history, which recent_turns can't do.

        `speaker_entity` restricts the result to one person's side of the
        history plus Nova's replies to them (V3 P5.1d.1). Assistant turns are
        stamped with whoever they were answering, so an exchange stays whole.
        """
        await self.initialize()
        where: list[str] = []
        params: list[Any] = []
        if conversation_id is not None:
            where.append("conversation_id=?")
            params.append(str(conversation_id))
        if speaker_entity:
            where.append("speaker_entity=?")
            params.append(str(speaker_entity))
        if term:
            where.append("content LIKE ?")
            params.append(f"%{term}%")
        if since_iso:
            where.append("created_at >= ?")
            params.append(since_iso)
        if until_iso:
            where.append("created_at <= ?")
            params.append(until_iso)
        sql = "SELECT id, conversation_id, role, content, created_at, speaker_entity, speaker_label, input_source, speaker_status FROM turns"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_facts(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, entity, attribute, value, confidence, created_at, last_reinforced_at, "
                "source, evidence, verification_status, last_confirmed_at, "
                "access_count, last_accessed_at, salience FROM facts "
                "WHERE entity LIKE ? OR attribute LIKE ? OR value LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def reinforce_fact(self, fact_id: str) -> None:
        """Re-mention of an existing fact: touch last_reinforced_at and nudge
        confidence toward (but never past) 0.95 — Phase 1.3. A re-mention is
        also a fresh observation of the fact, so stamp last_confirmed_at too
        (#19) — the verification_status label is left unchanged (only an
        explicit confirm_fact promotes it)."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE facts SET last_reinforced_at=?, last_confirmed_at=?, "
                "confidence=MIN(0.95, confidence + 0.05) WHERE id=?",
                (self._now_iso(), self._now_iso(), fact_id),
            )
            await db.commit()

    async def get_person_by_name(self, name: str) -> dict[str, Any] | None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, attributes_json, created_at FROM people WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

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
        sql = (
            "SELECT id, entity, attribute, value, confidence, created_at, "
            "source, evidence, verification_status, last_confirmed_at FROM facts ORDER BY created_at ASC"
        )
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

    # --- Reminders / scheduling ------------------------------------------------

    async def create_reminder(
        self,
        *,
        reminder_id: UUID,
        title: str,
        details: str,
        due_at_iso: str,
        recurrence: str = "none",
        status: str = "pending",
    ) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO reminders(reminder_id, title, details, due_at, recurrence, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (str(reminder_id), title, details, due_at_iso, recurrence, status, now, now),
            )
            await db.commit()

    async def list_reminders(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        sql = "SELECT reminder_id, title, details, due_at, recurrence, status, created_at, updated_at FROM reminders"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY due_at ASC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def due_reminders(self, *, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT reminder_id, title, details, due_at, recurrence, status FROM reminders "
                "WHERE status='pending' AND due_at <= ? ORDER BY due_at ASC LIMIT ?",
                (now_iso, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def reschedule_reminder(self, *, reminder_id: str, next_due_at_iso: str) -> None:
        """Fire-and-reschedule for a recurring reminder — stays 'pending'."""
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE reminders SET due_at=?, updated_at=? WHERE reminder_id=?",
                (next_due_at_iso, now, reminder_id),
            )
            await db.commit()

    async def set_reminder_status(self, *, reminder_id: str, status: str) -> bool:
        """True if a reminder actually changed status; False if there is none."""
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE reminders SET status=?, updated_at=? WHERE reminder_id=?",
                (status, now, reminder_id),
            )
            await db.commit()
            return cur.rowcount == 1

    # --- Local file / photo recall (indexed documents) -------------------------

    async def get_document_mtime(self, path: str) -> float | None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT mtime FROM documents WHERE path=?", (path,)) as cur:
                row = await cur.fetchone()
        return float(row[0]) if row else None

    async def upsert_document(self, *, path: str, excerpt: str, mtime: float) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO documents(path, excerpt, mtime, indexed_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET excerpt=excluded.excerpt, mtime=excluded.mtime, indexed_at=excluded.indexed_at",
                (path, excerpt, float(mtime), now),
            )
            await db.commit()

    async def search_documents(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        """Keyword fallback so indexed files stay recallable even if the
        semantic index (Chroma) is degraded — mirrors search_facts/etc."""
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT path, excerpt, mtime, indexed_at FROM documents WHERE path LIKE ? OR excerpt LIKE ? "
                "ORDER BY indexed_at DESC LIMIT ?",
                (like, like, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_documents(self, *, limit: int = 200) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT path, excerpt, mtime, indexed_at FROM documents ORDER BY indexed_at DESC LIMIT ?", (int(limit),)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --- Deeper document synthesis (DS1) ----------------------------------------

    async def document_chunk_count(self, path: str) -> int:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(1) FROM document_chunks WHERE path=?", (path,)) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def replace_document_chunks(self, path: str, chunks: list[str]) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM document_chunks WHERE path=?", (path,))
            await db.executemany(
                "INSERT INTO document_chunks(path, chunk_index, text) VALUES(?, ?, ?)",
                [(path, i, c) for i, c in enumerate(chunks)],
            )
            await db.commit()

    async def all_document_chunks(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every indexed chunk, for rebuilding the semantic index (P10 pre-flight).

        Document chunks are written to Chroma live but were absent from
        `rebuild_semantic_index`, so a rebuild produced a quietly incomplete
        index. They are fully present here, which is what makes the rebuild
        honest rather than partial.
        """
        await self.initialize()
        sql = "SELECT path, chunk_index, text FROM document_chunks ORDER BY path, chunk_index"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def all_turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every stored turn, for rebuilding the semantic index.

        Same reason as `all_document_chunks`: substantive turns are indexed
        live and were missing from the rebuild.
        """
        await self.initialize()
        sql = ("SELECT id, conversation_id, role, content, created_at, "
               "speaker_entity, speaker_label, input_source FROM turns "
               "ORDER BY created_at")
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def search_document_chunks(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """Keyword fallback across chunk text — same resilience role as
        search_documents, but at chunk granularity for synthesis queries."""
        await self.initialize()
        like = f"%{q}%"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT path, chunk_index, text FROM document_chunks WHERE text LIKE ? LIMIT ?", (like, int(limit))
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --- Habit & pattern learning (HP1) -----------------------------------------

    async def log_tool_usage(self, tool_name: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO tool_usage_log(tool_name, called_at) VALUES(?, ?)", (tool_name, self._now_iso())
            )
            await db.commit()

    async def distinct_logged_tools(self, since_iso: str) -> list[str]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT DISTINCT tool_name FROM tool_usage_log WHERE called_at >= ?", (since_iso,)
            ) as cur:
                rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def tool_usage_since(self, tool_name: str, since_iso: str) -> list[str]:
        """Raw called_at timestamps for one tool since a given time — the
        caller does the hour/day clustering (keeps the SQL simple)."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT called_at FROM tool_usage_log WHERE tool_name=? AND called_at >= ? ORDER BY called_at ASC",
                (tool_name, since_iso),
            ) as cur:
                rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

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


    # ---------------- Agentic goal/task/proposal APIs ----------------

    async def create_goal(
        self,
        *,
        goal_id: UUID,
        project_name: str,
        title: str,
        objective: str,
        success_criteria: str,
        status: str = "active",
        priority: int = 50,
    ) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO goals(goal_id, project_name, title, objective, success_criteria, status, priority, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(goal_id), project_name, title, objective, success_criteria, status, int(priority), now, now),
            )
            await db.commit()

    async def update_goal_status(self, *, goal_id: UUID, status: str,
                                 expected_generation: int | None = None) -> bool:
        """True if a goal actually changed status; False if there is no such goal.

        The caller needs to know. `POST /goals/<unknown-uuid>/cancel` used to
        answer `{"status": "cancelled"}` for a goal that does not exist, and the
        resume route went on to enqueue a `__decide__` task against it.

        `expected_generation` fences the write to one lifecycle run: a decision
        made before a cancel must not pause or complete the goal the user
        resumed afterwards. Checked IN the UPDATE, so there is no gap between
        verifying and mutating.

        A fenced write ALSO requires the goal to be `active`. Generation
        equality says "the same run"; it does not say "a run still accepting
        decisions". A goal paused on generation 4 is on run 4 and must still
        refuse a decision that would complete it — the pause is exactly the
        state where Nova is waiting on the user, and honouring a queued decision
        there resumes work nobody asked to resume.

        Unfenced callers (`expected_generation=None`) are untouched: cancel,
        resume and the operator routes legitimately move a goal out of states
        other than active, and they are not applying a decision.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            if expected_generation is None:
                cur = await db.execute(
                    "UPDATE goals SET status=?, updated_at=? WHERE goal_id=?",
                    (status, now, str(goal_id)))
            else:
                cur = await db.execute(
                    self._DECISION_GATE,
                    (status, now, str(goal_id), int(expected_generation)))
            await db.commit()
            return cur.rowcount == 1

    # ── Atomic decision application (V3 P10 C5) ─────────────────────────────
    #
    # A supervisor decision is ONE act with several consequences: the goal
    # changes state, the decision task finishes, history records what happened,
    # and a question additionally leaves a proposal. Those were four separate
    # transactions, so a cancel landing between them left a coherent-looking
    # database describing something that never happened — a pending proposal
    # from a run the user had already cancelled, or a `complete` event arriving
    # after the goal had moved to a new generation.
    #
    # Two things make each method below atomic:
    #
    #   * ONE connection, ONE commit. aiosqlite opens an implicit transaction at
    #     the first write and holds it until commit, so every statement here
    #     lands together or not at all.
    #   * The FIRST statement is always the fenced UPDATE on `goals`. It is both
    #     the authority check and the lock: it takes the write lock for the whole
    #     transaction, so a concurrent cancel must wait for this to commit. The
    #     user's cancel is not lost — it is ordered after a decision that had
    #     already earned the right to apply.
    #
    # The gate requires `status='active'` as well as the generation. Generation
    # equality alone says "the same run", not "a run still accepting decisions":
    # a PAUSED goal on generation 4 must not be completed by a decision that
    # also carries generation 4.

    #: The one condition under which a decision may be applied.
    _DECISION_GATE = (
        "UPDATE goals SET status=?, updated_at=? "
        "WHERE goal_id=? AND generation=? AND status='active'"
    )

    async def _complete_decision_task(self, db, *, task_id: str,
                                      result: dict[str, Any], now: str) -> None:
        await db.execute(
            "UPDATE tasks SET status='done', result_json=?, last_error='', updated_at=? "
            "WHERE task_id=?",
            (json.dumps(result or {}, ensure_ascii=False), now, task_id))

    async def _add_event(self, db, *, event_id: UUID, goal_id: UUID,
                         project_name: str, kind: str, message: str,
                         now: str) -> None:
        await db.execute(
            "INSERT INTO progress_events(event_id, goal_id, project_name, kind, "
            "message, created_at, acknowledged) VALUES(?, ?, ?, ?, ?, ?, 0)",
            (str(event_id), str(goal_id), project_name, kind, message, now))

    async def apply_question_decision(
        self, *, goal_id: UUID, project_name: str, expected_generation: int,
        task_id: str, proposal_id: UUID, event_id: UUID, message: str,
    ) -> bool:
        """Pause, record the question, finish the task, log it — or none of it.

        The measured split: the fenced pause committed, the user cancelled, and
        the proposal, task completion and `question` event then landed against a
        cancelled goal on a newer generation. Nova was waiting for an answer to
        a question belonging to a run that no longer existed.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                self._DECISION_GATE,
                ("paused", now, str(goal_id), int(expected_generation)))
            if int(cur.rowcount or 0) != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO proposals(proposal_id, goal_id, project_name, "
                "suggestion, rationale, status, created_at, decided_at) "
                "VALUES(?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (str(proposal_id), str(goal_id), project_name, message,
                 "Needed to proceed with the active goal.", now))
            await self._complete_decision_task(
                db, task_id=task_id, result={"question": message}, now=now)
            await self._add_event(db, event_id=event_id, goal_id=goal_id,
                                  project_name=project_name, kind="question",
                                  message=message, now=now)
            await db.commit()
            return True

    async def apply_final_decision(
        self, *, goal_id: UUID, project_name: str, expected_generation: int,
        task_id: str, event_id: UUID, message: str,
    ) -> bool:
        """Complete the goal, finish the task, log it — or none of it.

        A `complete` event is the authoritative record that the goal finished.
        It must not be able to arrive after the goal has been cancelled and
        moved to a new run.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                self._DECISION_GATE,
                ("completed", now, str(goal_id), int(expected_generation)))
            if int(cur.rowcount or 0) != 1:
                await db.rollback()
                return False
            await self._complete_decision_task(
                db, task_id=task_id, result={"final": message}, now=now)
            await self._add_event(db, event_id=event_id, goal_id=goal_id,
                                  project_name=project_name, kind="complete",
                                  message=message, now=now)
            await db.commit()
            return True

    async def apply_decision_error(
        self, *, goal_id: UUID, project_name: str, expected_generation: int,
        task_id: str, error: str, message: str,
    ) -> bool:
        """Record a planner failure — atomically, against the run that had it.

        The supervisor used to consult `_generation_is_current()` and then, if
        it said yes, perform two unfenced writes. That is a check-then-write,
        and `_generation_is_current` says so about itself: the goal can be
        cancelled between the SELECT and the write. Measured on e9183c2 by
        blocking between them: a run-0 planner failure published "Could not
        decide next step" into the run-1 lifecycle the user had just resumed.

        A failure is a report about what happened, but the report is ADDRESSED
        to a lifecycle run — so it is gated exactly like a decision. The goal's
        status is unchanged; the gate is an active->active touch that both
        proves authority and holds the row for the rest of the transaction.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                self._DECISION_GATE,
                ("active", now, str(goal_id), int(expected_generation)))
            if int(cur.rowcount or 0) != 1:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE tasks SET status='failed', result_json='{}', "
                "last_error=?, updated_at=? WHERE task_id=?",
                (error, now, task_id))
            await self._add_event(db, event_id=uuid4(), goal_id=goal_id,
                                  project_name=project_name, kind="error",
                                  message=message, now=now)
            await db.commit()
            return True

    async def count_finished_steps(self, *, goal_id: UUID, generation: int) -> int:
        """Finished steps in ONE lifecycle run — the step budget's window.

        The budget counted every done/failed task the goal had ever had, across
        all runs. Combined with a resume that did not open a new run, a goal
        that hit the budget paused, resumed, counted the same history, and
        paused again without ever reaching the model — so the "resume or
        cancel it" Nova offers was not actually resumable.
        """
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM tasks WHERE goal_id=? AND generation=? "
                "AND status IN ('done', 'failed')",
                (str(goal_id), int(generation)))
            row = await cur.fetchone()
        return int((row or [0])[0] or 0)

    async def apply_step_budget_pause(
        self, *, goal_id: UUID, project_name: str, expected_generation: int,
        task_id: str, message: str,
    ) -> bool:
        """Pause a goal that has spent its step budget — atomically, or not.

        This transition sits BEFORE the model is consulted, so it never went
        through the decision path and kept its own unfenced
        `update_goal_status(status="paused")` plus two more commits. Same
        defect class as the original cancelled -> paused bug: the budget is hit,
        the user cancels, and the unfenced write then overwrites the
        cancellation with `paused` on a run that had already ended.

        Gated exactly like a decision, because it is one: it ends the run the
        claimed `__decide__` belongs to.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                self._DECISION_GATE,
                ("paused", now, str(goal_id), int(expected_generation)))
            if int(cur.rowcount or 0) != 1:
                await db.rollback()
                return False
            await self._complete_decision_task(
                db, task_id=task_id, result={"paused": message}, now=now)
            await self._add_event(db, event_id=uuid4(), goal_id=goal_id,
                                  project_name=project_name, kind="paused",
                                  message=message, now=now)
            await db.commit()
            return True

    async def apply_tool_decision(
        self, *, goal_id: UUID, project_name: str, expected_generation: int,
        task_id: str, tool_task_id: UUID, decide_task_id: UUID, event_id: UUID,
        tool_name: str, args: dict[str, Any],
    ) -> bool:
        """Schedule the tool AND its continuation, finish the task, log it.

        The scheduled tool and the `__decide__` that follows it are two halves
        of one plan. Committed separately, a cancel in between left a runnable
        tool with no continuation, or a continuation for work that was never
        scheduled — and the supervisor announced "Next: <tool>" either way.

        The goal keeps its status here; this decision does not end the run. The
        gate is still a write, because it is what takes the lock and proves the
        goal is on this generation AND still active.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            # 'active' -> 'active': a touch, not a transition. It is the gate
            # and the lock, in the one statement.
            cur = await db.execute(
                self._DECISION_GATE,
                ("active", now, str(goal_id), int(expected_generation)))
            if int(cur.rowcount or 0) != 1:
                await db.rollback()
                return False
            insert = (
                "INSERT INTO tasks(task_id, goal_id, project_name, tool_name, "
                "args_json, status, attempts, run_after, last_error, result_json, "
                "created_at, updated_at, generation) "
                "VALUES(?, ?, ?, ?, ?, 'queued', 0, ?, '', '{}', ?, ?, ?)")
            await db.execute(insert, (
                str(tool_task_id), str(goal_id), project_name, tool_name,
                json.dumps(args or {}, ensure_ascii=False), now, now, now,
                int(expected_generation)))
            await db.execute(insert, (
                str(decide_task_id), str(goal_id), project_name, "__decide__",
                "{}", now, now, now, int(expected_generation)))
            await self._complete_decision_task(
                db, task_id=task_id,
                result={"decision": {"type": "tool", "name": tool_name,
                                     "args": args or {}}, "scheduled": True},
                now=now)
            await self._add_event(db, event_id=event_id, goal_id=goal_id,
                                  project_name=project_name, kind="plan",
                                  message=f"Next: {tool_name}", now=now)
            await db.commit()
            return True

    async def get_goal(self, *, goal_id: UUID) -> dict[str, Any] | None:
        """One goal, or None. The stored row is the authority on its project."""
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT goal_id, project_name, title, objective, success_criteria, status, priority, created_at, updated_at, generation FROM goals WHERE goal_id=?",
                (str(goal_id),),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "goal_id": row[0], "project_name": row[1], "title": row[2],
            "objective": row[3], "success_criteria": row[4], "status": row[5],
            "priority": row[6], "created_at": row[7], "updated_at": row[8],
            "generation": int(row[9] or 0),
        }

    async def cancel_goal(self, *, goal_id: UUID) -> dict[str, Any] | None:
        """Cancel a goal AND stop the work it had queued. None if no such goal.

        Cancelling used to set `goals.status` and nothing else, so every task
        already queued for that goal stayed runnable. Stopping the queued rows
        here and refusing to claim a non-active goal's tasks (see
        claim_next_task) are two halves of the same invariant: after this
        returns, no NEW work for the goal can start.

        What it does NOT claim: a task already claimed and mid-flight cannot be
        un-executed. That count is reported rather than hidden.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT project_name, status FROM goals WHERE goal_id=?",
                                   (str(goal_id),))
            row = await cur.fetchone()
            if not row:
                return None
            project_name, previous = str(row[0]), str(row[1])
            # The generation bump is what makes an in-flight decision stale.
            # Without it, "cancel then immediately resume" left the goal active
            # again by the time a pre-cancel model call returned, and that old
            # decision could enqueue a tool, pause, or complete the RESUMED
            # goal. Cancelling has to invalidate work already in the air, not
            # just work already queued.
            await db.execute(
                "UPDATE goals SET status='cancelled', generation=generation+1, updated_at=? WHERE goal_id=?",
                (now, str(goal_id)),
            )
            stopped = await db.execute(
                "UPDATE tasks SET status='cancelled', last_error='goal cancelled', updated_at=? "
                "WHERE goal_id=? AND status='queued'",
                (now, str(goal_id)),
            )
            cancelled_tasks = int(stopped.rowcount or 0)
            cur = await db.execute(
                "SELECT COUNT(*) FROM tasks WHERE goal_id=? AND status='running'",
                (str(goal_id),),
            )
            running = int((await cur.fetchone())[0] or 0)
            await db.commit()
        return {
            "goal_id": str(goal_id), "project_name": project_name,
            "previous_status": previous, "status": "cancelled",
            "cancelled_tasks": cancelled_tasks, "already_running": running,
        }

    async def resume_goal(self, *, goal_id: UUID) -> dict[str, Any] | None:
        """Make a goal active with AT MOST ONE runnable continuation. None if absent.

        Two defects in one place. Resume used to enqueue a `__decide__`
        unconditionally, so three resumes produced three independent
        continuations (and eight concurrent ones produced eight — different
        rows, so the duplicate-claim fix does not touch it). And it took the
        project from a `?project=` query parameter defaulting to "general", so
        a goal created under "alpha" resumed into "general".

        Both are fixed by the same single statement: the INSERT selects the
        project FROM the goals row and only fires when no runnable continuation
        exists. One statement is one implicit transaction in SQLite, so
        concurrent callers serialise on the write lock and the second sees the
        first's row.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            # WHICH run a resume lands in depends on where it is resuming FROM,
            # and the difference is decided inside the one UPDATE so concurrent
            # resumes cannot each reach their own answer:
            #
            #   cancelled -> cancel already opened the new run. Bumping again
            #                would give eight concurrent resumes eight runs and
            #                eight continuations.
            #   paused    -> nothing has opened a run yet, and the step budget
            #                is counted per run. Without a bump here, a goal
            #                paused ON the budget resumes into the same run,
            #                counts the same finished steps, and pauses again
            #                without ever reaching the model — measured: two
            #                `paused` events, zero planner calls. Nova offers
            #                "resume or cancel it", so resume has to mean
            #                something: it opens exactly ONE new bounded run.
            #   active    -> already running; no churn.
            #
            # The CASE reads the status in the same statement that changes it,
            # so the first caller bumps and every later one sees 'active' and
            # does not.
            activated = await db.execute(
                "UPDATE goals SET status='active', "
                "generation = generation + (CASE WHEN status='paused' THEN 1 ELSE 0 END), "
                "updated_at=? WHERE goal_id=?",
                (now, str(goal_id)),
            )
            if int(activated.rowcount or 0) != 1:
                await db.rollback()
                return None
            created = await db.execute(
                "INSERT INTO tasks(task_id, goal_id, project_name, tool_name, args_json, "
                "status, attempts, run_after, last_error, result_json, created_at, updated_at, generation) "
                "SELECT ?, g.goal_id, g.project_name, '__decide__', '{}', 'queued', 0, ?, '', '{}', ?, ?, g.generation "
                "FROM goals g WHERE g.goal_id = ? AND NOT EXISTS ("
                "  SELECT 1 FROM tasks t WHERE t.goal_id = g.goal_id "
                "  AND t.tool_name = '__decide__' AND t.status IN ('queued', 'running') "
                # Only a continuation from THIS run counts. A decide left
                # running by the run the user cancelled must not stop the
                # resumed run from getting one of its own.
                "  AND t.generation = g.generation)",
                (str(uuid4()), now, now, now, str(goal_id)),
            )
            enqueued = int(created.rowcount or 0) == 1
            existing = None
            if not enqueued:
                # Say WHICH continuation already exists. `queued` means a
                # supervisor will pick it up; `running` means one already has
                # it — and if that claimer died, the goal has no runnable step
                # until boot recovery releases the row
                # (cancel_pending_background_work). Reporting only "not
                # enqueued" would make those two states look identical to the
                # caller.
                cur = await db.execute(
                    "SELECT status FROM tasks WHERE goal_id=? AND tool_name='__decide__' "
                    "AND status IN ('queued', 'running') ORDER BY updated_at ASC LIMIT 1",
                    (str(goal_id),),
                )
                found = await cur.fetchone()
                existing = str(found[0]) if found else None
            cur = await db.execute("SELECT project_name FROM goals WHERE goal_id=?",
                                   (str(goal_id),))
            project_name = str((await cur.fetchone())[0])
            await db.commit()
        return {
            "goal_id": str(goal_id), "project_name": project_name,
            "status": "active", "continuation_enqueued": enqueued,
            "existing_continuation": existing,
        }

    async def list_goals(self, *, project_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        # `generation` is selected because a goal list that cannot say which
        # lifecycle run a goal is on cannot answer "which revision is that?".
        # Appended last so the positional mapping below does not shift.
        q = "SELECT goal_id, project_name, title, objective, success_criteria, status, priority, created_at, updated_at, generation FROM goals"
        params: list[Any] = []
        if project_name:
            q += " WHERE project_name=?"
            params.append(project_name)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(q, params)
            rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append(
                dict(
                    goal_id=r[0],
                    project_name=r[1],
                    title=r[2],
                    objective=r[3],
                    success_criteria=r[4],
                    status=r[5],
                    priority=r[6],
                    created_at=r[7],
                    updated_at=r[8],
                    generation=int(r[9] or 0),
                )
            )
        return out

    async def enqueue_task(
        self,
        *,
        task_id: UUID,
        goal_id: UUID,
        project_name: str,
        tool_name: str,
        args: dict[str, Any],
        status: str = "queued",
        attempts: int = 0,
        run_after_iso: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Queue one goal task. False if the goal is no longer accepting work.

        Found by the seeded fuzz (seeds 13, 99, 31337) after the cancel fix: a
        goal could be CANCELLED and then still be given new work. Nothing ran —
        the claim refuses a non-active goal — but the row sat there runnable,
        and resuming the goal later would have executed a step planned before
        the user cancelled it. In production the window is real: the supervisor
        claims a step, the user cancels mid-flight, and the supervisor then
        enqueues the next one.

        The INSERT carries the condition so there is no check-then-act gap.

        The goal must EXIST. This used to insert happily against a goal_id no
        row matched, so a bug anywhere upstream produced runnable work owned by
        nothing. Both production callers create the goal immediately before
        calling this, so requiring it costs nothing and closes the door.

        A PAUSED goal still accepts work: pause is a hold, not an ending, and
        resume is meant to continue the plan that was already laid. Only
        `cancelled` and `completed` are closed.
        """
        await self.initialize()
        now = self._now_iso()
        run_after = run_after_iso or now
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "INSERT INTO tasks(task_id, goal_id, project_name, tool_name, args_json, status, attempts, run_after, last_error, result_json, created_at, updated_at, generation) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "       COALESCE((SELECT g.generation FROM goals g WHERE g.goal_id = ?), 0) "
                "WHERE EXISTS (SELECT 1 FROM goals g WHERE g.goal_id = ? "
                "              AND g.status NOT IN ('cancelled', 'completed')) "
                # The fence: when the caller says which generation it is acting
                # for, the row only appears if the goal is still on it. One
                # statement, so there is no window between checking and writing.
                "  AND (? IS NULL OR EXISTS (SELECT 1 FROM goals g WHERE g.goal_id = ? "
                "                            AND g.generation = ?))",
                (
                    str(task_id),
                    str(goal_id),
                    project_name,
                    tool_name,
                    json.dumps(args or {}, ensure_ascii=False),
                    status,
                    int(attempts),
                    run_after,
                    "",
                    "{}",
                    now,
                    now,
                    str(goal_id),
                    str(goal_id),
                    expected_generation,
                    str(goal_id),
                    expected_generation,
                ),
            )
            await db.commit()
            return int(cur.rowcount or 0) == 1

    #: The statement that actually CLAIMS a row.
    #:
    #: It repeats the runnable condition rather than trusting the SELECT that
    #: chose the candidate: those are two statements, and a cancel landing
    #: between them would otherwise hand out a superseded row. Named so the
    #: guard can be exercised directly instead of only through the race.
    _CLAIM_UPDATE = (
        "UPDATE tasks SET status='running', updated_at=? "
        "WHERE task_id=? AND status='queued' "
        "AND EXISTS (SELECT 1 FROM goals g WHERE g.goal_id = tasks.goal_id "
        "            AND g.status='active' "
        "            AND g.generation = tasks.generation)"
    )

    #: The candidate SELECT. Named alongside `_CLAIM_UPDATE` so each half of the
    #: claim can be exercised on its own: with both in place either one alone
    #: keeps an unrunnable row out, which is the point of having both — and also
    #: means a defect in one is invisible through `claim_next_task`.
    #:
    #: An INNER join: no authoritative goal, no execution. This was a LEFT JOIN
    #: with `g.goal_id IS NULL` treated as runnable, kept for compatibility —
    #: but this is the GOAL task queue, and the supervisor hands a claimed real
    #: tool straight to ToolRouter.execute without ever looking the parent up.
    #: So a row naming a nonexistent goal and a side-effecting tool ran on
    #: nothing's authority (measured: an orphan `demo.spy` executed). Rows left
    #: by older builds that created work against goals which never existed are
    #: exactly what must not run.
    _CLAIM_SELECT = (
        "SELECT t.task_id, t.goal_id, t.project_name, t.tool_name, t.args_json, "
        "       t.attempts, t.generation "
        "FROM tasks t JOIN goals g ON g.goal_id = t.goal_id "
        "WHERE t.status='queued' AND t.run_after <= ? "
        "AND g.status='active' AND t.generation = g.generation "
        "ORDER BY t.updated_at ASC LIMIT 1"
    )

    async def claim_next_task(self) -> dict[str, Any] | None:
        """Claim one runnable goal task. Never hands the same row to two callers.

        This used to SELECT the next row and then UPDATE it in two separate
        statements — "atomically-ish (SQLite single writer)", which is not a
        thing. Every claimer opens its OWN connection, so each statement is its
        own transaction and two claimers simply selected the same row: measured
        19 claims over 10 queued tasks. Both would then run the same tool plan,
        which is precisely the delete-twice / write-twice / spend-twice hazard
        the router's retry guard exists to prevent, one layer further down.

        The UPDATE now carries the state it expects, so the row is claimed by
        exactly one caller and everyone else moves on to the next candidate.

        GENERATION IS A PROPERTY OF THE TASK ROW, not of the goal at claim time.
        This returned `COALESCE(g.generation, 0)` — the goal's CURRENT
        generation — so a task created in run N that survived a cancel was
        handed to the supervisor labelled as run N+1. Combined with a runnable
        predicate that never compared the two, a stale task was laundered into
        the resumed lifecycle and executed as though it belonged there
        (measured: one tool ran 3 times across a cancel/resume). The row owns
        the run it was created under; nothing may relabel it.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            for _ in range(self._CLAIM_ATTEMPTS):
                # The parent goal's state is part of what makes a task runnable.
                # Cancelling a goal used to change only the goals row, so its
                # queued tasks stayed claimable and the supervisor went on
                # executing a goal the user had cancelled — measured: 3 tool
                # calls after cancellation, and the goal's own status then
                # overwritten to 'paused'. See `_CLAIM_SELECT`.
                cur = await db.execute(self._CLAIM_SELECT, (now,))
                row = await cur.fetchone()
                if not row:
                    return None
                claimed = await db.execute(self._CLAIM_UPDATE, (now, row[0]))
                await db.commit()
                if claimed.rowcount == 1:
                    return dict(task_id=row[0], goal_id=row[1], project_name=row[2],
                                tool_name=row[3], args_json=row[4], attempts=row[5],
                                # t.generation — the run this task was CREATED
                                # under, never whatever the goal is on now.
                                generation=int(row[6] or 0))
                # Someone else took it between the SELECT and the UPDATE, or the
                # goal moved on. The next iteration reads what is genuinely
                # still runnable.
        return None

    #: Does this caller still own the run it is reporting on? The task must be
    #: the one it claimed, on the same run, and that run must still be the
    #: goal's current one. `paused` counts as owned: a pause does not bump the
    #: generation and does not invalidate work already in flight, so a tool that
    #: finished during a pause finished honestly and resume must not redo it.
    _OWNS_RUN = (
        "generation=? AND EXISTS (SELECT 1 FROM goals g "
        "                         WHERE g.goal_id = tasks.goal_id "
        "                         AND g.generation=? "
        "                         AND g.status IN ('active','paused'))"
    )

    async def complete_task(self, *, task_id: str, status: str,
                            result: dict[str, Any] | None = None,
                            error: str = "",
                            expected_generation: int) -> str:
        """Write a terminal outcome, if this caller still owns the run.

        Returns "applied", "superseded" or "ignored".

        This was `UPDATE tasks SET status=? ... WHERE task_id=?` with no guard
        at all, while the claim and the retry beside it were both fenced. So a
        worker holding a task from run N could write a terminal status at any
        later moment. Measured on ed81c94:

            claim (run 0) -> cancel (goal cancelled, run 1) -> worker returns ok
              -> tasks.status = 'done'.  A cancelled goal had completed work.
            claim -> cancel -> resume (run 1) -> the run-0 worker returns ok
              -> 'done' on the CURRENT run's behalf.
            complete failed('disk full') -> a duplicate callback says done
              -> 'done', last_error ''. A recorded failure became a success.
            cancel marks a queued task 'cancelled' -> a late completion arrives
              -> 'done'. The cancellation was erased.

        `expected_generation` is required rather than optional. An opt-in fence
        is the same defect with more steps: whoever forgets it is unfenced
        again, silently. There is no way to finish a goal task without saying
        which run is being finished.

        Three outcomes, decided in one statement each:

        applied     the caller owns the run; the status it reports is written.
        superseded  the row is still running but the run ended underneath it.
                    The work may genuinely have succeeded, so the truth is
                    kept in `result_json` and `last_error` - but the STATUS is
                    NOT the one reported, because "what completed?" must not
                    count work belonging to a run the user stopped.

                    It resolves to `failed`, which is what this codebase
                    already means by "ran during a run that ended": it is the
                    status `_discard_stale_decision` chose for exactly this
                    case, and the generation step counter already reads
                    `status IN ('done','failed')`. Neither label is precise -
                    the work did not fail, and `cancelled` means "never ran"
                    for every queued row a cancel touches. That missing state
                    is recorded in the Stage 13B state-model note rather than
                    invented here: a sixth task status is a schema and UI
                    change, and it should be decided once, together with the
                    `ask_user` missing-state work.
        ignored     the row is not running: already done, failed or cancelled.
                    The first terminal write wins and repeats change nothing.

        The outer `WHERE ... AND status='running'` is what makes it idempotent;
        the CASE is what keeps a superseded row from being stranded in
        `running` forever.
        """
        await self.initialize()
        now = self._now_iso()
        gen = int(expected_generation)
        payload = json.dumps(result or {}, ensure_ascii=False)
        stale_note = (error or "").strip() or (
            f"this finished after lifecycle run {gen} ended (the goal was "
            f"cancelled, or resumed onto a later run), so its outcome "
            f"({status}) was recorded but not counted as work done")
        stale_payload = json.dumps(
            {"superseded": True, "generation": gen, "reported_status": status,
             "reported_result": result or {}, "reported_error": error or ""},
            ensure_ascii=False)

        async with aiosqlite.connect(self._db_path, isolation_level=None) as db:
            # IMMEDIATE takes the write lock up front, so the ownership read and
            # the write it decides cannot straddle another writer's commit.
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT status, (" + self._OWNS_RUN + ") FROM tasks "
                    "WHERE task_id=?", (gen, gen, task_id))
                row = await cur.fetchone()
                if row is None or str(row[0]) != "running":
                    await db.execute("COMMIT")
                    return "ignored"
                owns = bool(row[1])
                await db.execute(
                    "UPDATE tasks SET status=?, result_json=?, last_error=?, "
                    "updated_at=? WHERE task_id=? AND status='running'",
                    (status if owns else "failed",
                     payload if owns else stale_payload,
                     (error or "") if owns else stale_note,
                     now, task_id))
                await db.execute("COMMIT")
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return "applied" if owns else "superseded"

    async def bump_task_attempt(self, *, task_id: str, attempts: int,
                                run_after_iso: str, error: str,
                                expected_generation: int | None = None) -> bool:
        """Requeue a running task for one more attempt. False if it may not be.

        A retry creates FUTURE runnable work, so it is a lifecycle decision, not
        bookkeeping about what already happened. This was an unconditional
        `UPDATE tasks SET status='queued' ... WHERE task_id=?`, which meant:

            tool from run N is claimed and starts
            user cancels           -> goal cancelled, generation N+1
            the running tool fails transiently
            the retry path requeues it ANYWAY
            user resumes           -> goal active, generation N+1
            the run-N task is runnable again and executes

        Measured on 31150da: the tool ran 3 times. An in-flight side effect
        cannot be undone and nobody claims otherwise — but scheduling it to run
        AGAIN after the user cancelled is a different thing entirely.

        With `expected_generation` the requeue lands only if, in one statement:
        the task is still the running one the caller claimed, on that same run,
        and its goal still exists, is active, and is still on that run.

        `expected_generation=None` keeps the old unconditional behaviour for any
        caller that is not a goal task.
        """
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            if expected_generation is not None:
                cur = await db.execute(
                    "UPDATE tasks SET status='queued', attempts=?, run_after=?, "
                    "last_error=?, updated_at=? "
                    "WHERE task_id=? AND status='running' AND generation=? "
                    "AND EXISTS (SELECT 1 FROM goals g WHERE g.goal_id = tasks.goal_id "
                    "            AND g.status='active' AND g.generation=?)",
                    (int(attempts), run_after_iso, error, now, task_id,
                     int(expected_generation), int(expected_generation)),
                )
                await db.commit()
                return int(cur.rowcount or 0) == 1
            await db.execute(
                "UPDATE tasks SET status='queued', attempts=?, run_after=?, last_error=?, updated_at=? WHERE task_id=?",
                (int(attempts), run_after_iso, error, now, task_id),
            )
            await db.commit()
            return True

    async def list_tasks(self, *, goal_id: str | None = None, project_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        # `generation` is selected because a task list that cannot say which
        # run a row belongs to cannot answer "which revision was that?".
        q = "SELECT task_id, goal_id, project_name, tool_name, status, attempts, run_after, last_error, result_json, generation, created_at, updated_at FROM tasks"
        params: list[Any] = []
        where = []
        if goal_id:
            where.append("goal_id=?")
            params.append(goal_id)
        if project_name:
            where.append("project_name=?")
            params.append(project_name)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(q, params)
            rows = await cur.fetchall()
        out=[]
        for r in rows:
            out.append(dict(
                task_id=r[0], goal_id=r[1], project_name=r[2], tool_name=r[3], status=r[4],
                attempts=r[5], run_after=r[6], last_error=r[7], result_json=r[8],
                generation=r[9], created_at=r[10], updated_at=r[11]
            ))
        return out

    async def create_proposal(
        self,
        *,
        proposal_id: UUID,
        goal_id: UUID,
        project_name: str,
        suggestion: str,
        rationale: str,
        status: str = "pending",
    ) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO proposals(proposal_id, goal_id, project_name, suggestion, rationale, status, created_at, decided_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (str(proposal_id), str(goal_id), project_name, suggestion, rationale, status, now, None),
            )
            await db.commit()

    async def latest_pending_proposal(self, *, project_name: str) -> dict[str, Any] | None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT proposal_id, goal_id, suggestion, rationale, created_at FROM proposals WHERE project_name=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (project_name,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return dict(proposal_id=row[0], goal_id=row[1], suggestion=row[2], rationale=row[3], created_at=row[4])

    async def set_proposal_status(self, *, proposal_id: str, status: str) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE proposals SET status=?, decided_at=? WHERE proposal_id=?", (status, now, proposal_id))
            await db.commit()

    async def add_progress_event(self, *, event_id: UUID, goal_id: UUID, project_name: str, kind: str, message: str) -> None:
        await self.initialize()
        now = self._now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO progress_events(event_id, goal_id, project_name, kind, message, created_at, acknowledged) VALUES(?, ?, ?, ?, ?, ?, 0)",
                (str(event_id), str(goal_id), project_name, kind, message, now),
            )
            await db.commit()

    async def list_progress_events(self, *, goal_id: str | None = None,
                                   project_name: str | None = None,
                                   limit: int = 50) -> list[dict[str, Any]]:
        """Read progress without consuming it.

        `fetch_unacked_progress` ACKNOWLEDGES what it returns, which makes it a
        destructive read: the first caller takes the events and every later one
        sees nothing. That is correct for a once-only delivery, and wrong for
        anything that polls -- so it could never have served a panel, and it
        never did. Nothing in production called it at all.

        Meanwhile `add_progress_event` is written from seven places in
        `AgentSupervisor`: retries, errors, blocked work, a tool completing, and
        the note saying a completion arrived after its run had ended. All of it
        was recorded and unreachable — the channel that answers "what should
        Nova say next" was write-only.

        This read leaves `acknowledged` alone, so it can be polled, and returns
        it, so a caller can still tell what has been delivered once.
        """
        await self.initialize()
        q = ("SELECT event_id, goal_id, project_name, kind, message, "
             "created_at, acknowledged FROM progress_events")
        where, params = [], []
        if goal_id:
            where.append("goal_id=?")
            params.append(str(goal_id))
        if project_name:
            where.append("project_name=?")
            params.append(project_name)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at ASC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(q, params)
            rows = await cur.fetchall()
        return [dict(event_id=r[0], goal_id=r[1], project_name=r[2], kind=r[3],
                     message=r[4], created_at=r[5],
                     acknowledged=bool(int(r[6] or 0)))
                for r in rows]

    async def fetch_unacked_progress(self, *, project_name: str, limit: int = 10) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT event_id, goal_id, kind, message, created_at FROM progress_events WHERE project_name=? AND acknowledged=0 ORDER BY created_at ASC LIMIT ?",
                (project_name, int(limit)),
            )
            rows = await cur.fetchall()
            ids=[r[0] for r in rows]
            if ids:
                placeholders=",".join(["?"]*len(ids))
                await db.execute(f"UPDATE progress_events SET acknowledged=1 WHERE event_id IN ({placeholders})", ids)
                await db.commit()
        return [dict(event_id=r[0], goal_id=r[1], kind=r[2], message=r[3], created_at=r[4]) for r in rows]
