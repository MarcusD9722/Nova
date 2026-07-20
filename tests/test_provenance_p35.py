"""Phase 3.5 / #19: memory provenance.

Every stored fact can carry where it came from and how trustworthy it is, so an
assumption is never presented as a settled fact. Covers the vocabulary, the
storage round-trip, assumption-aware recall hedging, confirmation/re-verification,
the feature flag, and the real v2 -> v3 migration on a pre-existing database.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_MEMORY_PROVENANCE", "1")

import aiosqlite

from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.provenance import (
    CONFIRMED,
    INFERRED,
    OBSERVED,
    STATED,
    UNVERIFIED,
    classify_default,
    is_assumption,
    normalize_status,
    observed_at_write,
    reverification_due,
)
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── Vocabulary (pure) ──
    check(normalize_status("STATED") == "stated", "normalize lowercases a known status")
    check(normalize_status("garbage") == UNVERIFIED, "unknown status normalizes to unverified")
    check(normalize_status(None) == UNVERIFIED, "None normalizes to unverified")
    check(is_assumption(INFERRED) and is_assumption(UNVERIFIED), "inferred/unverified are assumptions")
    check(not is_assumption(STATED) and not is_assumption(OBSERVED), "stated/observed are NOT assumptions")
    check(observed_at_write(STATED) and observed_at_write(CONFIRMED), "stated/confirmed count as observed-at-write")
    check(not observed_at_write(INFERRED), "inferred is NOT observed-at-write")
    check(classify_default("lesson", "x")[1] == INFERRED, "lessons default to inferred (assumption)")
    check(classify_default("mood", "x")[1] == INFERRED, "mood readings default to inferred")
    check(classify_default("user", "name")[1] == STATED, "user identity defaults to stated")
    check(classify_default("session", "last_active")[1] == OBSERVED, "session bookkeeping is observed")

    now = "2026-07-20T00:00:00+00:00"
    old = "2020-01-01T00:00:00+00:00"
    check(not reverification_due(INFERRED, None, old, now_iso=now), "an assumption is never 'due for re-verification'")
    check(reverification_due(OBSERVED, None, old, now_iso=now, max_age_days=180), "an old observed fact is due for re-verify")
    check(not reverification_due(OBSERVED, now, None, now_iso=now, max_age_days=180), "a freshly-confirmed fact is not due")

    # ── Storage round-trip + defaults ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # Explicit provenance persists and round-trips.
        await mem.add_fact(
            entity="note", attribute="coffee", value="likes oat milk",
            confidence=0.9, source="user", verification_status="stated",
        )
        rec = (await mem.get_facts(entity="note", attribute="coffee"))[0]
        check(rec.verification_status == "stated", f"explicit status persisted (got {rec.verification_status})")
        check(rec.source == "user", "explicit source persisted")
        check(rec.last_confirmed_at is not None, "a stated fact is confirmed-at-write (last_confirmed_at set)")

        # Default classification: a mood reading is an inference, never confirmed.
        await mem.add_fact(entity="mood", attribute="2026-07-20", value="tired")
        mood = (await mem.get_facts(entity="mood"))[0]
        check(mood.verification_status == "inferred", f"mood defaults to inferred (got {mood.verification_status})")
        check(mood.last_confirmed_at is None, "an inferred fact has NO last_confirmed_at (never confirmed)")

        # Recall surfaces the trust label.
        hits = await mem.search("oat milk")
        fact_hit = next((h for h in hits if h.kind == "fact"), None)
        check(
            fact_hit is not None and fact_hit.provenance.get("verification") == "stated",
            "recall carries the stated verification label",
        )

        # confirm_fact promotes an assumption to a checked fact, with evidence.
        await mem.confirm_fact(str(mood.id), evidence="re-observed on 2026-07-21")
        mood2 = (await mem.get_facts(entity="mood"))[0]
        check(mood2.verification_status == "confirmed", "confirm_fact promotes status to confirmed")
        check(mood2.last_confirmed_at is not None, "confirm_fact stamps last_confirmed_at")
        check(mood2.evidence == "re-observed on 2026-07-21", "confirm_fact attaches evidence")

        # Re-mention of a list-valued fact reinforces (and re-confirms) rather than duplicating.
        await mem.add_fact(entity="note", attribute="coffee", value="likes oat milk", confidence=0.9)
        dupes = await mem.get_facts(entity="note", attribute="coffee")
        check(len(dupes) == 1, "re-mention does not duplicate a list-valued fact")
        check(dupes[0].confidence >= 0.9, "re-mention reinforces confidence")

        # Feature flag OFF -> no provenance recorded (columns stay NULL, honestly).
        os.environ["NOVA_MEMORY_PROVENANCE"] = "0"
        await mem.add_fact(entity="note", attribute="flagoff", value="no provenance here")
        off = (await mem.get_facts(entity="note", attribute="flagoff"))[0]
        check(off.verification_status == "unverified" and off.source is None, "flag off -> no provenance stored")
        os.environ["NOVA_MEMORY_PROVENANCE"] = "1"

    # ── Migration v2 -> v3 on a PRE-EXISTING database (Marcus's real upgrade path) ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "nova.sqlite3"
        async with aiosqlite.connect(db_path) as db:
            # A facts table WITHOUT the provenance columns, stamped at v2.
            await db.execute(
                "CREATE TABLE facts (id TEXT PRIMARY KEY, entity TEXT NOT NULL, attribute TEXT NOT NULL, "
                "value TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL, last_reinforced_at TEXT);"
            )
            await db.execute(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL);"
            )
            await db.execute("INSERT INTO schema_version VALUES(2, 'pre-provenance baseline', '2026-01-01T00:00:00+00:00');")
            await db.execute(
                "INSERT INTO facts(id, entity, attribute, value, confidence, created_at) "
                "VALUES('legacy-1','user','name','Marcus',0.9,'2026-01-01T00:00:00+00:00');"
            )
            await db.commit()

        be = SQLiteMemoryBackend(db_path)
        await be.initialize()
        latest = max(v for v, _, _ in SQLiteMemoryBackend._MIGRATIONS)
        check(await be.schema_version() == latest, f"existing v2 DB upgrades to latest v{latest} (got {await be.schema_version()})")

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("PRAGMA table_info(facts);") as cur:
                cols = {r[1] for r in await cur.fetchall()}
        needed = {"source", "evidence", "verification_status", "last_confirmed_at"}
        check(needed <= cols, f"v3 migration added the provenance columns (got {sorted(cols)})")

        rows = await be.search_facts("Marcus")
        check(bool(rows), "legacy fact survives the migration")
        check(rows[0].get("verification_status") is None, "legacy fact stays unlabeled (honest — not fabricated as verified)")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
