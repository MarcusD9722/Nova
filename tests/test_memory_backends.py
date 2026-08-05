"""Full-audit coverage for memory/ modules that had NONE.

Covers memory/backends/diskcache_backend.py, memory/backends/json_backend.py,
memory/world_model.py and memory/schemas.py — every public function, plus the
edge cases that decide whether a failure is loud or silent.

These four sit under the source of truth. A cache that silently returns stale
data, an audit log that drops a record, or a world fact stored without a source
are all failures that look exactly like success from the caller's side, which
is why they get explicit tests rather than incidental ones.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

import aiosqlite
import orjson

from memory.backends.diskcache_backend import DiskCacheBackend
from memory.backends.json_backend import JsonAuditBackend
from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.world_model import WorldModel, _norm
from memory.schemas import ConversationTurn, FactRecord, PersonRecord, EventRecord, MemoryHit

check = Checks()


async def test_diskcache(tmp: Path) -> None:
    check.section("DiskCacheBackend")
    cache = DiskCacheBackend(tmp / "dc")

    await cache.set("k", {"a": 1})
    check(await cache.get("k") == {"a": 1}, "set/get round-trips a dict")
    check(await cache.get("missing") is None, "missing key returns None, not a raise")

    await cache.set("bytes", b"\x00\x01")
    check(await cache.get("bytes") == b"\x00\x01", "binary values survive")
    await cache.set("none_value", None)
    check(await cache.get("none_value") is None, "a stored None reads back as None")

    check(await cache.delete("k") is True, "delete reports True for a present key")
    check(await cache.get("k") is None, "deleted key is gone")
    check(await cache.delete("k") is False, "delete reports False for an absent key")

    # TTL is the whole point of this cache — an expiry that silently never
    # fires would serve stale memory results forever.
    await cache.set("ttl", "soon", ttl_s=1)
    check(await cache.get("ttl") == "soon", "value is live before the TTL")
    await asyncio.sleep(1.4)
    check(await cache.get("ttl") is None, "value is gone after the TTL")

    # Concurrency: the backend serializes on an asyncio lock; prove no
    # interleaving corruption and no deadlock under parallel access.
    await asyncio.gather(*(cache.set(f"c{i}", i) for i in range(40)))
    got = await asyncio.gather(*(cache.get(f"c{i}") for i in range(40)))
    check(got == list(range(40)), "40 concurrent set/get pairs are all correct")

    await cache.close()
    check(cache._cache is None, "close() releases the handle")
    await cache.set("after_close", 1)
    check(await cache.get("after_close") == 1, "the cache transparently reopens after close")
    await cache.close()


async def test_json_audit(tmp: Path) -> None:
    check.section("JsonAuditBackend")
    audit = JsonAuditBackend(tmp / "json")

    await audit.initialize()
    check((tmp / "json" / "audit.jsonl").exists(), "initialize creates audit.jsonl")
    check((tmp / "json" / "snapshots.jsonl").exists(), "initialize creates snapshots.jsonl")
    await audit.initialize()
    check(True, "initialize is idempotent (no raise on second call)")

    await audit.append_audit({"op": "add_fact", "entity": "user"})
    lines = (tmp / "json" / "audit.jsonl").read_bytes().splitlines()
    rec = orjson.loads(lines[0])
    check(rec["op"] == "add_fact", "the audit record is written")
    check("ts" in rec, "a timestamp is stamped automatically")

    await audit.append_audit({"op": "x", "ts": "2020-01-01T00:00:00+00:00"})
    rec2 = orjson.loads((tmp / "json" / "audit.jsonl").read_bytes().splitlines()[1])
    check(rec2["ts"] == "2020-01-01T00:00:00+00:00", "a caller-supplied ts is preserved")

    await audit.append_snapshot({"kind": "daily"})
    check(len((tmp / "json" / "snapshots.jsonl").read_bytes().splitlines()) == 1,
          "snapshots go to their own file")

    # Append integrity under concurrency: JSONL is only useful if every line is
    # one whole record. A torn write here corrupts the audit trail silently.
    await asyncio.gather(*(audit.append_audit({"n": i}) for i in range(60)))
    raw = (tmp / "json" / "audit.jsonl").read_bytes().splitlines()
    parsed = []
    for ln in raw:
        try:
            parsed.append(orjson.loads(ln))
        except Exception:
            parsed.append(None)
    check(all(p is not None for p in parsed), f"every line is valid JSON after 60 concurrent appends ({len(raw)} lines)")
    check(sorted(p["n"] for p in parsed if p and "n" in p) == list(range(60)),
          "all 60 concurrent records landed, none lost")

    # datetime is natively serializable to orjson; a set is not. The second
    # case must fail LOUDLY rather than silently dropping an audit record.
    await audit.append_audit({"when": datetime.now(timezone.utc)})
    check(True, "a datetime value serializes without error")
    raised = False
    try:
        await audit.append_audit({"bad": {1, 2, 3}})
    except TypeError:
        raised = True
    check(raised, "an unserializable value raises instead of silently dropping the record")


async def test_world_model(tmp: Path) -> None:
    check.section("WorldModel")
    db_path = tmp / "wm" / "nova.sqlite3"
    sqlite = SQLiteMemoryBackend(db_path)
    await sqlite.initialize()
    wm = WorldModel(db_path)

    check(_norm("  Python   Lang  ") == "python lang", "_norm collapses whitespace and lowercases")
    check(len(_norm("x" * 500)) == 200, "_norm caps length at 200")

    check(await wm.stats() == {"triples": 0, "subjects": 0}, "stats on an empty model")

    # The provenance rule is the module's stated non-negotiable.
    check(await wm.upsert("Python", "is-a", "programming language", source="") is False,
          "an UNSOURCED triple is refused")
    check(await wm.upsert("", "is-a", "thing", source="web") is False, "empty subject refused")
    check(await wm.upsert("x", "", "thing", source="web") is False, "empty predicate refused")
    check(await wm.upsert("x", "is-a", "", source="web") is False, "empty object refused")
    check(await wm.stats()["triples"] if False else (await wm.stats())["triples"] == 0,
          "no refused triple reached the table")

    ok = await wm.upsert("Python", "is-a", "programming language", confidence=0.6, source="https://python.org")
    check(ok is True, "a sourced triple is stored")
    rows = await wm.query_subject("python")
    check(len(rows) == 1 and rows[0]["object"] == "programming language", "query_subject finds it")
    check(rows[0]["subject"] == "python", "subject is normalized on write")
    check(rows[0]["source"] == "https://python.org", "the source is retained")

    # Re-observation must reinforce, never duplicate.
    await wm.upsert("Python", "is-a", "programming language", source="https://docs.python.org")
    rows = await wm.query_subject("Python")
    check(len(rows) == 1, "re-observing does not duplicate the triple")
    check(abs(rows[0]["confidence"] - 0.65) < 1e-6, f"confidence reinforced 0.60 -> {rows[0]['confidence']}")
    check(rows[0]["source"] == "https://docs.python.org", "the newest source wins")

    for _ in range(20):
        await wm.upsert("Python", "is-a", "programming language", source="s")
    rows = await wm.query_subject("Python")
    check(rows[0]["confidence"] <= 0.97, f"confidence is capped at 0.97 (got {rows[0]['confidence']})")

    await wm.upsert("Anthropic", "makes", "Claude", confidence=0.9, source="https://anthropic.com")
    check(len(await wm.search("claude")) == 1, "search matches on object")
    check(len(await wm.search("anthropic")) == 1, "search matches on subject")
    check(len(await wm.search("makes")) == 1, "search matches on predicate")
    check(await wm.search("nothing-here") == [], "search returns [] for no match")

    ordered = await wm.search("a", limit=10)
    confs = [r["confidence"] for r in ordered]
    check(confs == sorted(confs, reverse=True), "search returns most-confident first")

    check(await wm.is_fresh("Python") is True, "a just-written subject is fresh")
    check(await wm.is_fresh("Never-Heard-Of-It") is False, "an unknown subject is not fresh")
    check(await wm.is_fresh("Python", max_age_days=0) is False, "max_age_days=0 makes nothing fresh")

    check(await wm.stale_subjects(older_than_days=90) == [], "nothing is stale yet")
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE world_model SET last_confirmed_at=? WHERE subject='anthropic'", (old,))
        await db.commit()
    stale = await wm.stale_subjects(older_than_days=90)
    check(len(stale) == 1 and stale[0]["subject"] == "anthropic", "an aged triple surfaces as stale")
    check(await wm.is_fresh("Anthropic") is False, "an aged subject is no longer fresh")

    stats = await wm.stats()
    check(stats == {"triples": 2, "subjects": 2}, f"stats counts triples and subjects ({stats})")

    long_obj = "y" * 5000
    await wm.upsert("Big", "has", long_obj, source="s")
    got = (await wm.query_subject("Big"))[0]["object"]
    check(len(got) == 2000, f"an oversized object is truncated to 2000, not rejected ({len(got)})")


async def test_schemas() -> None:
    check.section("memory/schemas.py")
    conv, now = uuid4(), datetime.now(timezone.utc)

    t = ConversationTurn(id=uuid4(), conversation_id=conv, role="user", content="hi", created_at=now)
    check(t.role == "user" and t.content == "hi", "ConversationTurn builds")

    f = FactRecord(id=uuid4(), entity="user", attribute="name", value="Marcus", created_at=now)
    check(f.confidence == 0.7, "FactRecord confidence defaults to 0.7")
    check(f.verification_status == "unverified", "a new fact is 'unverified' until confirmed")
    check(f.source is None and f.last_confirmed_at is None, "provenance fields default to absent")

    p = PersonRecord(id=uuid4(), name="Leslie", attributes={"relation": "spouse"}, created_at=now)
    check(p.attributes["relation"] == "spouse", "PersonRecord carries attributes")
    p2 = PersonRecord(id=uuid4(), name="Solo", created_at=now)
    check(p2.attributes == {}, "PersonRecord attributes default to an empty dict")
    p2.attributes["x"] = "y"
    p3 = PersonRecord(id=uuid4(), name="Other", created_at=now)
    check(p3.attributes == {}, "the attributes default is per-instance, not shared state")

    e = EventRecord(id=uuid4(), date="2026-07-04", note="fireworks", created_at=now)
    check(e.date == "2026-07-04", "EventRecord builds")

    h = MemoryHit(id="1", kind="fact", text="x", score=0.5, provenance={"src": "sqlite"})
    check(h.score == 0.5, "MemoryHit builds")

    # Validation must reject bad input rather than coerce it silently.
    def rejects(fn, label):
        try:
            fn()
        except Exception:
            check(True, label)
            return
        check(False, label)

    rejects(lambda: ConversationTurn(id=uuid4(), conversation_id="not-a-uuid", role="user",
                                     content="x", created_at=now),
            "an invalid conversation_id is rejected, not coerced")
    rejects(lambda: ConversationTurn(id=uuid4(), conversation_id=conv, role="wizard",
                                     content="x", created_at=now),
            "an unknown role is rejected (Literal is enforced)")
    rejects(lambda: FactRecord(id=uuid4(), entity="e", attribute="a", value="v",
                               confidence=1.5, created_at=now),
            "confidence above 1.0 is rejected")
    rejects(lambda: FactRecord(id=uuid4(), entity="e", attribute="a", value="v",
                               confidence=-0.1, created_at=now),
            "confidence below 0.0 is rejected")


async def test_semantic_index_health(tmp: Path) -> None:
    """A degraded semantic index must stay VISIBLE, not just be survivable."""
    check.section("Unifier: semantic-index degradation is reported")
    from memory.unifier import MemoryUnifier

    mem = MemoryUnifier(tmp / "health", enable_chroma=False)
    await mem.initialize()
    health = mem.semantic_index_health()
    check(health["enabled"] is False, "chroma-disabled instances report enabled=False")

    mem2 = MemoryUnifier(tmp / "health2", enable_chroma=True)
    await mem2.initialize()

    class Broken:
        async def upsert_text(self, **_kw):
            raise RuntimeError("incompatible chroma store")

    mem2._chroma = Broken()
    h = mem2.semantic_index_health()
    check(h["enabled"] is True and h["degraded"] is False, "a healthy index reports degraded=False")

    for _ in range(5):
        await mem2._chroma_upsert_safe(doc_id="x", text="t", metadata={"k": "v"})

    h = mem2.semantic_index_health()
    # Before this fix the warning fired once and every later failure vanished:
    # semantic recall was permanently degraded with nothing reporting it.
    check(h["degraded"] is True, "five failed writes mark the index degraded")
    check(h["failures"] == 5, f"EVERY failure is counted, not just the first ({h['failures']})")
    check("incompatible chroma store" in (h["last_error"] or ""), "the real error text is retained")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_diskcache(tmp)
        await test_json_audit(tmp)
        await test_world_model(tmp)
        await test_schemas()
        await test_semantic_index_health(tmp)
    check.finish()


asyncio.run(main())
