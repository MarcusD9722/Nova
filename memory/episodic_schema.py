from __future__ import annotations

"""SQL for P4's warm tier. Defined once, used by both the create block and the
migration, so the two can never drift.

The existing backend repeats each table's DDL in `_create_all` and again inside
its migration entry. That is the documented convention and it works, but it has
already produced one near-miss in this codebase's history, and P4 adds four
tables at once. Declaring the statements here and referencing them from both
places keeps the convention's guarantee (a fresh DB builds the current schema;
an old DB replays migrations) without the copy.

Storage decision, made after reading the backend rather than before:
SQLite stays authoritative. Episodes, artifacts and decisions are small,
strongly relational, and want the same transactional guarantees as facts. Only
the heavy *evidence* goes to the filesystem (memory/cold_store.py), because
multi-megabyte blobs in SQLite bloat every backup and every VACUUM for data that
is read rarely and never queried by content.
"""

#: Every P4 table, in dependency order. Each statement is idempotent.
EPISODIC_DDL: list[str] = [
    # ── WARM: episodes ───────────────────────────────────────────────────────
    # What HAPPENED, as opposed to what is true (facts) or what is on screen
    # (hot artifacts). Deliberately small: enough to decide relevance without
    # touching cold evidence.
    """
    CREATE TABLE IF NOT EXISTS episodes (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        entities TEXT NOT NULL DEFAULT '[]',
        conversation_id TEXT,
        project TEXT,
        source_tool TEXT,
        trust TEXT NOT NULL DEFAULT 'TOOL_RESULT',
        freshness TEXT NOT NULL DEFAULT 'SESSION',
        provenance TEXT NOT NULL DEFAULT '{}',
        outcome TEXT,
        importance REAL NOT NULL DEFAULT 0.5,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed_at TEXT,
        superseded_by TEXT,
        created_at TEXT NOT NULL,
        -- V3 P5.1e / P5.1e.1: WHO did it, and WHO may read it. These are two
        -- questions, and conflating them is a real hazard in both directions:
        -- a background build failure on Marcus's private project has actor
        -- `system` but must stay owner-private, while labelling it `user` to
        -- keep it private would assert he ran the build.
        --
        --   actor_*        provenance: who caused this. Never authorises.
        --   privacy_scope  authorisation: whose episode this is. Never rendered.
        --
        -- `user` is the correct backfill for every pre-activation row: the
        -- frontend sent no speaker identity before P5.1e, so all existing
        -- episodic history IS Marcus's. Never store an embedding, similarity,
        -- threshold or raw audio here.
        speaker_entity TEXT NOT NULL DEFAULT 'user',
        speaker_label TEXT NOT NULL DEFAULT '',
        input_source TEXT NOT NULL DEFAULT 'typed',
        actor_entity TEXT NOT NULL DEFAULT 'user',
        actor_label TEXT NOT NULL DEFAULT '',
        privacy_scope TEXT NOT NULL DEFAULT 'user'
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_speaker ON episodes(speaker_entity, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_privacy ON episodes(privacy_scope, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_conv ON episodes(conversation_id, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_kind ON episodes(kind, importance DESC);",

    # ── WARM: artifacts ──────────────────────────────────────────────────────
    # The durable half of memory/artifacts.py. `item_index` and `parent_id` are
    # what make "the second one" answerable after a restart, so they are
    # first-class columns rather than JSON.
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        turn_id TEXT,
        episode_id TEXT,
        parent_id TEXT,
        item_index INTEGER,
        artifact_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        source_tool TEXT,
        trust TEXT NOT NULL DEFAULT 'TOOL_RESULT',
        freshness TEXT NOT NULL DEFAULT 'SESSION',
        provenance TEXT NOT NULL DEFAULT '{}',
        importance REAL NOT NULL DEFAULT 0.5,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed_at TEXT,
        cold_ref TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts(conversation_id, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_id, item_index);",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_episode ON artifacts(episode_id);",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type, created_at DESC);",

    # ── WARM: decisions ──────────────────────────────────────────────────────
    # Architectural decisions with their reasoning, so a future agent asking
    # "why is it built this way" gets the rationale rather than re-deriving it
    # from the implementation (or undoing it).
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        decision TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '[]',
        alternatives TEXT NOT NULL DEFAULT '[]',
        subsystem TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        supersedes TEXT,
        superseded_by TEXT,
        source_refs TEXT NOT NULL DEFAULT '[]',
        constraints TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status, decided_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_decisions_subsystem ON decisions(subsystem, decided_at DESC);",

    # ── COLD: evidence index ─────────────────────────────────────────────────
    # Metadata only. Bytes live on disk, content-addressed, so identical
    # evidence stored twice costs one copy and a missing file degrades to
    # "evidence unavailable" rather than corrupting the warm record.
    """
    CREATE TABLE IF NOT EXISTS cold_evidence (
        digest TEXT PRIMARY KEY,
        rel_path TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        content_type TEXT NOT NULL DEFAULT 'text/plain',
        ref_count INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_accessed_at TEXT
    );
    """,
]

#: Migration entry for memory/backends/sqlite_backend.py::_MIGRATIONS.
EPISODIC_MIGRATION = (
    7,
    "V3 P4: episodic memory (episodes, artifacts, decisions, cold_evidence)",
    EPISODIC_DDL,
)

# NOTE: there is deliberately no numbered migration for the speaker/actor/
# privacy columns (V3 P5.1e.2 cleanup).
#
# P5.1e briefly defined one. It could never fire: `_apply_migrations` stamps
# `latest` without replaying history for a fresh database — whose create block
# already builds these columns — so a versioned ALTER would only ever run
# against a table that already had them, which is a duplicate-column error.
# It was replaced by `SQLiteMemoryBackend._migrate_episodes_schema()`, an
# idempotent PRAGMA-guarded in-place upgrade, but the dead constant survived and
# then drifted: P5.1e.1 appended an index over `privacy_scope`, a column that
# constant never added.
#
# Removed rather than repaired. A second migration path that nothing calls is
# worse than none: it reads as authoritative, and the next person to touch the
# schema would reasonably update it instead of the code that actually runs.
# `_migrate_episodes_schema()` is the single canonical upgrade path.
