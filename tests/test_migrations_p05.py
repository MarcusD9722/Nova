"""Phase 0.5: schema versioning / migrations in SQLiteMemoryBackend."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from memory.backends.sqlite_backend import SQLiteMemoryBackend

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "nova.sqlite3"

        # Fresh DB: initialize builds full schema and stamps latest version.
        be = SQLiteMemoryBackend(db_path)
        await be.initialize()
        latest = max(v for v, _, _ in SQLiteMemoryBackend._MIGRATIONS)
        v = await be.schema_version()
        check(v == latest, f"fresh DB stamped at latest version (got {v}, latest {latest})")

        # Idempotent: a second backend on the same file doesn't re-stamp or fail.
        be2 = SQLiteMemoryBackend(db_path)
        await be2.initialize()
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(1) FROM schema_version") as cur:
                count = (await cur.fetchone())[0]
        check(count == 1, f"re-init doesn't duplicate stamps (rows={count})")

        # A future migration applies exactly once, in order, and re-stamps.
        class Patched(SQLiteMemoryBackend):
            _MIGRATIONS = SQLiteMemoryBackend._MIGRATIONS + [
                (latest + 1, "test: add p05_demo table",
                 ["CREATE TABLE IF NOT EXISTS p05_demo (id TEXT PRIMARY KEY);"]),
            ]

        be3 = Patched(db_path)
        await be3.initialize()
        check(await be3.schema_version() == latest + 1, "pending migration applied and stamped")
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='p05_demo'") as cur:
                found = await cur.fetchone()
        check(found is not None, "migration SQL actually executed (table exists)")

        be4 = Patched(db_path)
        await be4.initialize()
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(1) FROM schema_version") as cur:
                count = (await cur.fetchone())[0]
        check(count == 2, f"already-applied migration not replayed (stamps={count})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
