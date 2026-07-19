"""Phase 1 (Living Memory): knowledge graph, decay/reinforcement, consolidation."""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from memory.graph import Edge, extract_turn_edges, fact_edge, person_relation_edge
from memory.unifier import MemoryUnifier, _staleness_factor

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # ── 1.1 edges: upsert + reinforcement semantics ──
        await mem.graph.upsert_edge(Edge("person", "Liam", "child_of", "person", "user"))
        await mem.graph.upsert_edge(Edge("person", "Liam", "child_of", "person", "user"))
        rows = await mem.graph.edges_for("liam")
        check(len(rows) == 1, f"re-observed edge reinforces, not duplicates (rows={len(rows)})")
        check(rows[0]["weight"] == 2.0, f"weight incremented (got {rows[0]['weight']})")
        check(rows[0]["confidence"] > 0.6, f"confidence nudged up (got {rows[0]['confidence']})")

        # ── 1.1 extraction: relationship facts become edges automatically ──
        await mem.add_fact(entity="user", attribute="child", value="Mateo", confidence=0.9)
        await mem.add_fact(entity="user", attribute="spouse", value="Leslie", confidence=0.9)
        check(len(await mem.graph.edges_for("mateo")) == 1, "user.child fact auto-creates child_of edge")
        check((await mem.graph.edges_for("leslie"))[0]["predicate"] == "spouse_of", "spouse fact -> spouse_of edge")

        # junk-value guard: long sentence stored as a child name must NOT become a node name
        names = await mem.known_person_names()
        check("Mateo" in names and "Leslie" in names, f"known names include family facts (got {names})")

        # ── 1.1 extraction: co-mentions in turns ──
        edges = extract_turn_edges("Took Mateo and Liam to the park today", ["Mateo", "Liam", "Leslie"], None)
        check(any(e.predicate == "mentioned_with" for e in edges), "co-mention edge extracted from turn text")
        check(not extract_turn_edges("nothing relevant here", ["Mateo"], None), "no false-positive mentions")

        edges = extract_turn_edges("Leslie helped me test flappy bird", ["Leslie"], "flappy-bird")
        check(any(e.predicate == "involved_in" and e.dst_key == "flappy-bird" for e in edges),
              "person + active project -> involved_in edge")

        # ── 1.1 ingest_turn end-to-end (extraction wired into the unifier) ──
        from uuid import uuid4
        await mem.ingest_turn(uuid4(), "user", "Mateo and Leslie were laughing at the game all evening")
        mateo_edges = await mem.graph.edges_for("mateo")
        check(any(e["predicate"] == "mentioned_with" for e in mateo_edges), "ingest_turn observes co-mention edges")

        # ── 1.1 related(): 1-hop + 2-hop ──
        rel = await mem.related("Mateo")
        neighbor_keys = {n["key"] for n in rel["neighbors"]}
        check("user" in neighbor_keys, f"Mateo directly linked to user (got {neighbor_keys})")
        two_hop_keys = {t["key"] for t in rel["two_hop"]}
        check("liam" in two_hop_keys, f"Liam reachable from Mateo via 2-hop (got {two_hop_keys})")

        # ── person_relation_edge from people-table relation attr ──
        e = person_relation_edge("Dave", {"relation": "coworker"})
        check(e is not None and e.predicate == "coworker_of", "relation attr -> coworker_of edge")

        # ── 1.3 staleness factor ──
        now = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        check(_staleness_factor(now, None) > 0.99, "fresh memory ~1.0")
        check(abs(_staleness_factor(old, None) - 0.85) < 0.01, "very old memory floors at 0.85")
        check(_staleness_factor(old, now) > 0.99, "reinforcement timestamp overrides old created_at")
        check(_staleness_factor(None, None) == 1.0, "missing timestamps -> no decay")

        # ── 1.3 reinforcement on duplicate fact write ──
        await mem.add_fact(entity="note", attribute="hobby", value="Marcus enjoys snowboarding", confidence=0.6)
        await mem.add_fact(entity="note", attribute="hobby", value="Marcus enjoys snowboarding", confidence=0.6)
        async with aiosqlite.connect(Path(td) / "sqlite" / "nova.sqlite3") as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT confidence, last_reinforced_at FROM facts WHERE value LIKE '%snowboarding%'") as cur:
                row = await cur.fetchone()
        check(row is not None and row["last_reinforced_at"] is not None, "duplicate write sets last_reinforced_at")
        check(row["confidence"] > 0.6, f"duplicate write bumps confidence (got {row['confidence']})")

        # ── 1.3 ranking: old note sinks below fresh note, identity facts exempt ──
        await mem.add_fact(entity="user", attribute="name", value="Marcus Deleon", confidence=0.95)
        async with aiosqlite.connect(Path(td) / "sqlite" / "nova.sqlite3") as db:
            await db.execute("UPDATE facts SET created_at=? WHERE value LIKE '%snowboarding%'", (old,))
            await db.execute("UPDATE facts SET last_reinforced_at=NULL WHERE value LIKE '%snowboarding%'")
            await db.commit()
        mem._search_gen += 1
        hits = await mem.search(q="snowboarding hobby Marcus", limit=10)
        note_hit = next((h for h in hits if "snowboarding" in h.text), None)
        name_hit = next((h for h in hits if "Marcus Deleon" in h.text), None)
        check(note_hit is not None and note_hit.score < 0.95, f"old note decayed below base (got {note_hit.score if note_hit else None})")
        check(note_hit is not None and note_hit.score >= 0.80, "decayed note never sinks below CH1 high-confidence floor")
        check(name_hit is not None and name_hit.score == 0.95, "identity fact (user.name) exempt from decay")

        # ── 1.4 consolidation ──
        await mem.add_lesson("Do not start new projects when debugging existing ones", topic="reflection")
        await mem.add_lesson("Do not start new projects when debugging existing ones; fix the current issue", topic="reflection")
        await mem.add_lesson("Do not start a new project when debugging an existing one", topic="reflection")
        await mem.add_lesson("Always verify code changes work before reporting completion", topic="reflection")
        removed = await mem.consolidate_lessons()
        remaining = await mem.get_lessons(limit=20)
        check(removed >= 1, f"near-duplicate lessons removed (removed={removed})")
        check(any("verify code changes" in l for l in remaining), "distinct lesson survives consolidation")
        check(sum("not start" in l for l in remaining) == 1, f"one phrasing of the duplicate cluster survives (got {remaining})")

        # ── 1.1 timeline ──
        await mem.add_event(date="2026-07-18", note="Took the boys to the park")
        entries = await mem.timeline(days=7)
        check(any(e["kind"] == "event" and "park" in e["text"] for e in entries), "timeline includes fresh event")
        entries_about = await mem.timeline(about="park", days=7)
        check(all("park" in e["text"].lower() for e in entries_about if e["kind"] == "event"), "timeline 'about' filter works")

        # ── migration v2 applies to a v1-stamped DB ──
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
            from memory.backends.sqlite_backend import SQLiteMemoryBackend

            class V1Only(SQLiteMemoryBackend):
                _MIGRATIONS = [SQLiteMemoryBackend._MIGRATIONS[0]]

            db_path = Path(td2) / "nova.sqlite3"
            # Build a DB WITHOUT the v2 schema bits, stamped v1 (simulates pre-P1)
            v1 = V1Only(db_path)
            await v1.initialize()
            async with aiosqlite.connect(db_path) as db:
                await db.execute("DROP TABLE IF EXISTS edges")
                # remove the column fresh-create added, to simulate a real v1 DB
                cols = [r[1] for r in await (await db.execute("PRAGMA table_info(facts)")).fetchall()]
                if "last_reinforced_at" in cols:
                    await db.execute("ALTER TABLE facts DROP COLUMN last_reinforced_at")
                await db.commit()
            v2 = SQLiteMemoryBackend(db_path)
            await v2.initialize()
            check(await v2.schema_version() == 2, "v1-stamped DB migrates to v2")
            async with aiosqlite.connect(db_path) as db:
                tables = [r[0] for r in await (await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='edges'")).fetchall()]
                cols = [r[1] for r in await (await db.execute("PRAGMA table_info(facts)")).fetchall()]
            check("edges" in tables, "migration created edges table")
            check("last_reinforced_at" in cols, "migration added facts.last_reinforced_at")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
