"""The UPGRADE path: booting against a database older than the current schema.

Every other suite starts from an empty file, where `CREATE TABLE IF NOT EXISTS`
builds the complete current schema and no migration ever has to do anything. So
95/95 could be green while Nova refused to boot on Marcus's real database — and
that is exactly what happened. P5.1e added

    CREATE INDEX idx_episodes_speaker ON episodes(speaker_entity, ...)

to EPISODIC_DDL. On a P4-era database the `episodes` table already exists, so
CREATE TABLE IF NOT EXISTS is a no-op, the new columns are never added, and the
next statement indexes a column that isn't there:

    sqlite3.OperationalError: no such column: speaker_entity

`_migrate_episodes_schema()` adds those columns, but it ran AFTER the DDL loop —
too late to help. `turns` had the order right (migrate, then index); `episodes`
did not.

This file boots the real backend against databases built at each historical
shape. It fails if any create-block statement depends on a column that only the
idempotent migrations add.

Run:  venv\\Scripts\\python.exe tests\\test_schema_upgrade_path.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, run  # noqa: E402

from memory.backends.sqlite_backend import SQLiteMemoryBackend  # noqa: E402

check = Checks()

# The `episodes` table exactly as V3 P4 shipped it (16 columns, no speaker,
# actor or privacy attribution). Copied from Marcus's real nova.sqlite3, which
# is the database this bug was found on. Do not "modernise" this literal — its
# whole value is being the OLD shape.
P4_EPISODES = """
CREATE TABLE episodes (
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
    outcome TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL
);
"""

# `turns` as it shipped before P5.1d.1 — no speaker columns at all.
PRE_P51_TURNS = """
CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

SPEAKER_COLS = ("speaker_entity", "speaker_label", "input_source",
                "actor_entity", "actor_label", "privacy_scope")


def _cols(db: Path, table: str) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    finally:
        c.close()


def _indexes(db: Path, table: str) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,))}
    finally:
        c.close()


def _build_old_db(path: Path, *, episodes: bool = True, turns: bool = True,
                  rows: int = 0) -> None:
    c = sqlite3.connect(path)
    try:
        if turns:
            c.execute(PRE_P51_TURNS)
            for i in range(rows):
                c.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                          (f"t{i}", "conv", "user", f"legacy turn {i}",
                           "2026-07-01T00:00:00+00:00"))
        if episodes:
            c.execute(P4_EPISODES)
            for i in range(rows):
                c.execute(
                    "INSERT INTO episodes (id, kind, summary, created_at) "
                    "VALUES (?,?,?,?)",
                    (f"e{i}", "note", f"legacy episode {i}",
                     "2026-07-01T00:00:00+00:00"))
        c.commit()
    finally:
        c.close()


async def test_boots_on_a_p4_era_database():
    """The exact failure Marcus hit: pre-P5.1e episodes table, live boot."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.sqlite3"
        _build_old_db(db, rows=5)
        before = _cols(db, "episodes")
        check(not (before & set(SPEAKER_COLS)),
              "fixture really is pre-P5.1e (no speaker/actor/privacy columns)")

        await SQLiteMemoryBackend(db).initialize()   # used to raise here

        after = _cols(db, "episodes")
        for col in SPEAKER_COLS:
            check(col in after, f"initialize() added episodes.{col}")
        idx = _indexes(db, "episodes")
        check("idx_episodes_speaker" in idx, "speaker index created")
        check("idx_episodes_privacy" in idx, "privacy index created")


async def test_legacy_rows_survive_and_backfill_closed():
    """An upgrade must not drop history, and must not widen it either."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.sqlite3"
        _build_old_db(db, rows=7)
        await SQLiteMemoryBackend(db).initialize()

        c = sqlite3.connect(db)
        try:
            check(c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 7,
                  "all legacy turns survived the upgrade")
            check(c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 7,
                  "all legacy episodes survived the upgrade")
            # Pre-activation history is Marcus's, and must land owner-private.
            scopes = {r[0] for r in c.execute(
                "SELECT DISTINCT privacy_scope FROM episodes")}
            check(scopes == {"user"},
                  f"legacy episodes backfill closed to owner-private, got {scopes}")
            ents = {r[0] for r in c.execute(
                "SELECT DISTINCT speaker_entity FROM turns")}
            check(ents == {"user"}, f"legacy turns attributed to the owner, got {ents}")
        finally:
            c.close()


async def test_upgrade_is_idempotent():
    """Booting twice must not raise duplicate-column or duplicate-index."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.sqlite3"
        _build_old_db(db, rows=3)
        for n in (1, 2, 3):
            await SQLiteMemoryBackend(db).initialize()
        check(True, "three consecutive boots on an upgraded database succeed")


async def test_fresh_database_still_builds_everything():
    """The fix must not regress the path every other suite exercises."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "fresh.sqlite3"
        await SQLiteMemoryBackend(db).initialize()
        cols = _cols(db, "episodes")
        for col in SPEAKER_COLS:
            check(col in cols, f"fresh database has episodes.{col}")
        idx = _indexes(db, "episodes")
        check({"idx_episodes_speaker", "idx_episodes_privacy"} <= idx,
              "fresh database has both attribution indexes")


async def test_table_missing_entirely_is_created():
    """A database old enough to have no episodes table at all still upgrades."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ancient.sqlite3"
        _build_old_db(db, episodes=False, rows=2)
        await SQLiteMemoryBackend(db).initialize()
        cols = _cols(db, "episodes")
        check(cols, "episodes table created where none existed")
        for col in SPEAKER_COLS:
            check(col in cols, f"and it has episodes.{col}")


async def test_no_create_block_index_outruns_its_migration():
    """The structural guard, so the next column does not repeat this.

    Any index in EPISODIC_DDL over a column that `_migrate_episodes_schema`
    adds is an ordering hazard: it is fine on a fresh database and fatal on an
    old one. The migration call must come first in `initialize()`.
    """
    src = (REPO / "memory" / "backends" / "sqlite_backend.py").read_text(
        encoding="utf-8")
    mig = src.index("await self._migrate_episodes_schema(db)")
    ddl = src.index("for _sql in EPISODIC_DDL:")
    check(mig < ddl,
          "_migrate_episodes_schema runs BEFORE the EPISODIC_DDL loop")

    turns_mig = src.index("await self._migrate_turns_schema(db)")
    turns_idx = src.index("idx_turns_speaker_created")
    check(turns_mig < turns_idx,
          "_migrate_turns_schema still runs before the turns speaker index")

    # And the hazard is real, not hypothetical: prove the DDL does index a
    # column the migration owns.
    from memory.episodic_schema import EPISODIC_DDL
    indexed = [s for s in EPISODIC_DDL
               if "CREATE INDEX" in s and "speaker_entity" in s]
    check(bool(indexed),
          "EPISODIC_DDL does index speaker_entity — ordering is load-bearing")


async def main():
    await test_boots_on_a_p4_era_database()
    await test_legacy_rows_survive_and_backfill_closed()
    await test_upgrade_is_idempotent()
    await test_fresh_database_still_builds_everything()
    await test_table_missing_entirely_is_created()
    await test_no_create_block_index_outruns_its_migration()
    check.finish()


if __name__ == "__main__":
    run(main)
