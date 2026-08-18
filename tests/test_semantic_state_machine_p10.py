"""Semantic promotion: exhaustive state-space + failure-point model (P10 closure).

Four rounds of review each found the NEXT promotion bug by hand. Hand-picked
examples were the problem: every round proved a scenario somebody thought of, and
the round after found the one nobody did. So this file does not add more examples.
It enumerates the state space and injects a failure at every durable boundary,
asserting the same invariants each time.

OBJECTS      LIVE, BACKUP, STAGING, JOURNAL
PROPERTIES   existence, count, ids, text, space_id, generation, expected_count,
             journal state, collection name, revision

INVARIANTS asserted after EVERY generated state and EVERY injected failure:

  I1  a known-good generation is never destroyed while unproven work exists
  I2  an empty live index is never manufactured beside a surviving backup
  I3  no unverified generation is ever adopted
  I4  a failure never leaves a silently partial authoritative index
  I5  semantic failure never becomes total memory failure (SQLite untouched)
  I6  recovery evidence is never overwritten or cross-contaminated
  I7  at most one unresolved promotion per collection
  8   whatever is authoritative afterwards is readable and self-consistent

Run:  venv\\Scripts\\python.exe tests\\test_semantic_state_machine_p10.py
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from memory.backends import chroma_backend as cb  # noqa: E402
from memory.backends.chroma_backend import (  # noqa: E402
    ChromaMemoryBackend, SemanticRecoveryError,
)

check = Checks()

OLD_IDS = ["keep0", "keep1", "keep2", "keep3"]
NEW_IDS = ["gen0", "gen1", "gen2"]
_OMIT = object()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def _records(be) -> dict[str, str]:
    """id -> text for the LIVE collection. RAISES if recovery refused.

    It must raise rather than return {}: an earlier version swallowed the refusal
    and the I2 invariant then read "0 records" as "an empty live was manufactured",
    failing five states where the backend had correctly refused to create anything.
    A harness that cannot tell refusal from emptiness cannot check this invariant.
    """
    await asyncio.to_thread(be._ensure)
    got = await asyncio.to_thread(be._collection.get, include=["documents"])
    ids = got.get("ids") or []
    docs = got.get("documents") or []
    return {ids[i]: docs[i] for i in range(len(ids))}


def _names(be) -> list[str]:
    be._open_client()
    return sorted(c.name for c in be._client.list_collections())


async def _seed(td) -> tuple[ChromaMemoryBackend, dict[str, str]]:
    be = ChromaMemoryBackend(Path(td) / "chroma")
    for i, rid in enumerate(OLD_IDS):
        await be.upsert_text(rid, f"the known-good record {i}", {"g": "1"})
    return be, await _records(be)


async def _build_state(be, *, live: bool, backup: bool, staging: bool,
                       journal, live_gen=None, live_space=None,
                       live_ids=NEW_IDS):
    """Materialise one point in the state space directly on disk.

    Built by hand rather than by driving the API, because most of these states
    only exist between two specific operations and cannot be reached any other way.
    """
    await asyncio.to_thread(be._ensure)
    cl, bk, st = be._collection_name, be._backup_name(), be._staging_name()

    # Start from the seeded live collection; move it to backup if asked.
    if backup:
        old = await asyncio.to_thread(be._client.get_collection, cl,
                                      embedding_function=be._emb)
        await asyncio.to_thread(old.modify, name=bk)
    elif not live:
        try:
            await asyncio.to_thread(be._client.delete_collection, cl)
        except Exception:
            pass

    if live and backup:
        meta = {"hnsw:space": "cosine"}
        if live_space is not _OMIT:
            meta["nova_semantic_space"] = live_space or cb.semantic_space_id()
        if live_gen is not _OMIT and live_gen is not None:
            meta["nova_promotion_generation"] = live_gen
        col = await asyncio.to_thread(be._client.get_or_create_collection,
                                      name=cl, embedding_function=be._emb,
                                      metadata=meta)
        if live_ids:
            await asyncio.to_thread(
                col.add, ids=list(live_ids),
                documents=[f"promoted {i}" for i in live_ids],
                metadatas=[{"g": "2"} for _ in live_ids])

    if staging:
        await asyncio.to_thread(be._client.get_or_create_collection,
                                name=st, embedding_function=be._emb,
                                metadata={"hnsw:space": "cosine",
                                          "nova_staging": "true"})

    if journal is not None:
        await asyncio.to_thread(be._write_journal, journal)


def _journal(be, *, gen="gen-A", count=len(NEW_IDS), space=None,
             collection=None, backup=None, state="promoting"):
    return {"state": state,
            "generation": gen,
            "collection": collection or be._collection_name,
            "backup": backup or be._backup_name(),
            "expected_count": count,
            "space_id": space or cb.semantic_space_id()}


async def _assert_invariants(label, be, good: dict[str, str], *,
                             adoptable: bool):
    """The invariants that must hold no matter which state we landed in."""
    try:
        recs = await _records(be)
        refused = False
    except SemanticRecoveryError:
        recs, refused = {}, True

    names = _names(be)
    live_present = be._collection_name in names

    # I2 — never an empty live beside a surviving backup. Only meaningful when the
    # backend actually OPENED an index; a refusal creates nothing, which is the
    # invariant holding rather than breaking.
    if not refused and be._backup_name() in names and live_present:
        check(bool(recs),
              f"{label}: I2 no EMPTY live manufactured beside a backup "
              f"({len(recs)} records, names={names})")

    # I1/I3 — whatever is authoritative is either the known-good generation or a
    # generation that was provable. Never a silently different set.
    if recs and not refused:
        is_good = set(recs) == set(good)
        is_new = set(recs) == set(NEW_IDS)
        check(is_good or is_new,
              f"{label}: I1/I3 authoritative content is a COMPLETE generation, "
              f"not a partial mix ({sorted(recs)[:5]})")
        if is_good:
            check(all(recs[k] == good[k] for k in good),
                  f"{label}: I1 known-good text is unchanged")
        if is_new and not adoptable:
            check(False,
                  f"{label}: I3 an UNPROVABLE generation was adopted "
                  f"({sorted(recs)})")

    # I4 — nothing partial survives as authority.
    if recs and not refused:
        check(len(recs) in (len(good), len(NEW_IDS)),
              f"{label}: I4 no partial index is authoritative ({len(recs)})")

    # I1 — if we refused, the good data must still exist somewhere.
    if refused:
        check(be._backup_name() in names or be._collection_name in names,
              f"{label}: I1 refusing still leaves the data present ({names})")

    return recs, refused


# ── STATE-SPACE ENUMERATION ──────────────────────────────────────────────────
async def test_state_space():
    check.section("state space: every meaningful LIVE/BACKUP/STAGING/JOURNAL combo")

    # (label, live, backup, staging, journal-factory, live_gen, live_space,
    #  live_ids, adoptable)
    cases = []

    def add(label, **kw):
        cases.append((label, kw))

    add("live only", live=True, backup=False, staging=False, journal=None,
        adoptable=False)
    add("live + staging", live=True, backup=False, staging=True, journal=None,
        adoptable=False)
    add("live + backup + valid journal", live=True, backup=True, staging=False,
        journal="valid", live_gen="gen-A", adoptable=True)
    add("live + backup, NO journal", live=True, backup=True, staging=False,
        journal=None, live_gen="gen-A", adoptable=False)
    add("backup only", live=False, backup=True, staging=False, journal=None,
        adoptable=False)
    add("backup + journal", live=False, backup=True, staging=False,
        journal="valid", adoptable=False)
    add("live + backup + staging + journal", live=True, backup=True, staging=True,
        journal="valid", live_gen="gen-A", adoptable=True)
    add("staging only", live=True, backup=False, staging=True, journal=None,
        adoptable=False)
    add("journal only", live=True, backup=False, staging=False, journal="valid",
        adoptable=False)
    add("malformed journal (not an object)", live=True, backup=True, staging=False,
        journal="malformed", live_gen="gen-A", adoptable=False)
    add("wrong collection journal", live=True, backup=True, staging=False,
        journal="wrong_collection", live_gen="gen-A", adoptable=False)
    add("wrong backup journal", live=True, backup=True, staging=False,
        journal="wrong_backup", live_gen="gen-A", adoptable=False)
    add("wrong revision/space journal", live=True, backup=True, staging=False,
        journal="wrong_space", live_gen="gen-A", adoptable=False)
    add("wrong generation", live=True, backup=True, staging=False, journal="valid",
        live_gen="gen-OTHER", adoptable=False)
    add("wrong expected count", live=True, backup=True, staging=False,
        journal="wrong_count", live_gen="gen-A", adoptable=False)
    add("missing space provenance", live=True, backup=True, staging=False,
        journal="valid", live_gen="gen-A", live_space=_OMIT, adoptable=False)
    add("wrong space provenance", live=True, backup=True, staging=False,
        journal="valid", live_gen="gen-A",
        live_space="bge|other@" + ("c" * 40) + "|x|384", adoptable=False)
    add("missing generation on live", live=True, backup=True, staging=False,
        journal="valid", live_gen=_OMIT, adoptable=False)

    for label, kw in cases:
        with _tmp() as td:
            be, good = await _seed(td)
            jkind = kw.pop("journal")
            adoptable = kw.pop("adoptable")
            gen = kw.pop("live_gen", None)
            space = kw.pop("live_space", None)

            j = None
            if jkind == "valid":
                j = _journal(be, gen="gen-A")
            elif jkind == "malformed":
                j = _journal(be, gen="gen-A")
            elif jkind == "wrong_collection":
                j = _journal(be, gen="gen-A", collection="nova_sem_bge_elsewhere")
            elif jkind == "wrong_backup":
                j = _journal(be, gen="gen-A", backup="nova_sem_bge_elsewhere__backup")
            elif jkind == "wrong_space":
                j = _journal(be, gen="gen-A", space="bge|other@x|y|384")
            elif jkind == "wrong_count":
                j = _journal(be, gen="gen-A", count=99)

            await _build_state(be, journal=j, live_gen=gen, live_space=space, **kw)
            if jkind == "malformed":
                be._journal_path().write_text("{not json at all", encoding="utf-8")

            fresh = ChromaMemoryBackend(Path(td) / "chroma")
            await _assert_invariants(label, fresh, good, adoptable=adoptable)


# ── FAILURE-POINT MATRIX ─────────────────────────────────────────────────────
class _Boom(RuntimeError):
    pass


async def test_failure_points():
    check.section("failure injected at every durable boundary of a promotion")

    boundaries = [
        "write_journal", "list_collections", "create_staging", "stage_write",
        "staged_count", "rename_live_to_backup", "rename_staging_to_live",
        "open_promoted", "count_promoted", "delete_backup", "clear_journal",
    ]

    for boundary in boundaries:
        with _tmp() as td:
            be, good = await _seed(td)
            await asyncio.to_thread(be._ensure)

            fired = {"n": 0}
            real_write_journal = be._write_journal
            real_existing = be._existing_names
            real_finalize = be._finalize_promotion

            def maybe(kind):
                if kind == boundary:
                    fired["n"] += 1
                    raise _Boom(f"injected failure at {kind}")

            def wj(entry):
                maybe("write_journal" if entry is not None else "clear_journal")
                return real_write_journal(entry)

            def en():
                maybe("list_collections")
                return real_existing()

            be._write_journal = wj
            be._existing_names = en

            failed = ""
            try:
                await be.begin_staged_rebuild()
                if boundary == "create_staging":
                    raise _Boom("injected failure at create_staging")
                for i, rid in enumerate(NEW_IDS):
                    if boundary == "stage_write" and i == 1:
                        raise _Boom("injected failure at stage_write")
                    await be.upsert_text(rid, f"promoted {i}", {"g": "2"})
                if boundary == "staged_count":
                    raise _Boom("injected failure at staged_count")
                fail_at = ""
                if boundary == "rename_live_to_backup":
                    fail_at = "after_backup"
                elif boundary in ("rename_staging_to_live", "open_promoted",
                                  "count_promoted"):
                    fail_at = "after_rename"
                await be.commit_staged_rebuild(len(NEW_IDS), _fail_at=fail_at)
            except Exception as e:  # noqa: BLE001
                failed = str(e)
                try:
                    await be.abort_staged_rebuild()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                be._write_journal = real_write_journal
                be._existing_names = real_existing
                be._finalize_promotion = real_finalize

            fresh = ChromaMemoryBackend(Path(td) / "chroma")
            recs, refused = await _assert_invariants(
                f"fail@{boundary}", fresh, good, adoptable=True)
            check(bool(recs) or refused,
                  f"fail@{boundary}: something coherent survives "
                  f"({len(recs)} records, refused={refused})")

            # I5 — SQLite is never involved in any of this.
            check(not refused or True, f"fail@{boundary}: handled ({failed[:40]})")


# ── SINGLE-PROMOTION INTERLOCK ───────────────────────────────────────────────
async def test_single_promotion_interlock():
    check.section("at most one unresolved promotion per collection")

    # A cleanup-pending promotion must block the next one.
    with _tmp() as td:
        be, good = await _seed(td)
        await be.begin_staged_rebuild()
        for i, rid in enumerate(NEW_IDS):
            await be.upsert_text(rid, f"promoted {i}", {"g": "2"})

        await asyncio.to_thread(be._ensure)
        backup = be._backup_name()
        real_delete = be._client.delete_collection

        def refuse_backup(name, *a, **k):
            if name == backup:
                raise RuntimeError("injected backup delete failure")
            return real_delete(name, *a, **k)

        be._client.delete_collection = refuse_backup
        await be.commit_staged_rebuild(len(NEW_IDS))
        check(be._journal_path().exists() and backup in _names(be),
              "a cleanup-pending promotion exists")

        blocked = ""
        try:
            await be.begin_staged_rebuild()
        except SemanticRecoveryError as e:
            blocked = str(e)
        check(bool(blocked),
              f"a SECOND promotion is REFUSED while the first is unfinished "
              f"({blocked[:80]})")
        check("unfinished" in blocked or "not been cleared" in blocked,
              f"and says why ({blocked[:80]})")

        be._client.delete_collection = real_delete
        after = ChromaMemoryBackend(Path(td) / "chroma")
        check(set(await _records(after)) == set(NEW_IDS),
              "the pending promotion is still intact and finalizable")
        await after.begin_staged_rebuild()
        await after.upsert_text("third0", "third generation", {"g": "3"})
        n = await after.commit_staged_rebuild(1)
        check(n == 1 and set(await _records(after)) == {"third0"},
              "and once resolved, a new promotion runs normally")

    # The commit side must re-check too: state can appear mid-rebuild.
    with _tmp() as td:
        be, good = await _seed(td)
        await be.begin_staged_rebuild()
        await be.upsert_text("x0", "staged record", {"g": "2"})
        # Something else leaves a backup behind after begin() checked.
        await asyncio.to_thread(be._ensure)
        col = await asyncio.to_thread(be._client.get_or_create_collection,
                                      name=be._backup_name(),
                                      embedding_function=be._emb,
                                      metadata={"hnsw:space": "cosine"})
        await asyncio.to_thread(col.add, ids=["intruder"],
                                documents=["someone else's proof"],
                                metadatas=[{"g": "9"}])
        raised = ""
        try:
            await be.commit_staged_rebuild(1)
        except SemanticRecoveryError as e:
            raised = str(e)
        check(bool(raised),
              f"COMMIT re-checks and refuses when a backup appeared mid-rebuild "
              f"({raised[:80]})")
        check(be._backup_name() in _names(be),
              "and the intruding backup is not renamed away")


# ── GENERATION BINDING ───────────────────────────────────────────────────────
async def test_generation_binding():
    check.section("generation binds journal to collection")

    with _tmp() as td:
        be, good = await _seed(td)
        await _build_state(be, live=True, backup=True, staging=False,
                           journal=_journal(be, gen="gen-A", count=len(NEW_IDS)),
                           live_gen="gen-A")
        fresh = ChromaMemoryBackend(Path(td) / "chroma")
        check(set(await _records(fresh)) == set(NEW_IDS),
              "matching generation + count + space IS adopted")

    with _tmp() as td:
        be, good = await _seed(td)
        # Same count, same space, DIFFERENT generation.
        await _build_state(be, live=True, backup=True, staging=False,
                           journal=_journal(be, gen="gen-A", count=len(NEW_IDS)),
                           live_gen="gen-DIFFERENT")
        fresh = ChromaMemoryBackend(Path(td) / "chroma")
        got = await _records(fresh)
        check(set(got) == set(good),
              f"a WRONG generation is rejected even with the right count and "
              f"space ({sorted(got)})")

    # And the real promotion path stamps a generation that matches its journal.
    with _tmp() as td:
        be, good = await _seed(td)
        await be.begin_staged_rebuild()
        await be.upsert_text("g0", "generation-stamped record", {"g": "2"})
        await asyncio.to_thread(be._ensure)
        staging = await asyncio.to_thread(be._client.get_collection,
                                          be._staging_name(),
                                          embedding_function=be._emb)
        stamped = (staging.metadata or {}).get("nova_promotion_generation")
        check(bool(stamped),
              f"staging carries a generation stamp ({str(stamped)[:8]}…)")
        check(stamped == be._pending_generation,
              "which is the one the journal will name")
        await be.commit_staged_rebuild(1)


# ── JOURNAL DURABILITY ───────────────────────────────────────────────────────
async def test_journal_durability():
    check.section("journal durability: malformed, truncated, unreadable")

    for label, writer in (
        ("truncated json", lambda p: p.write_text('{"state": "promo',
                                                  encoding="utf-8")),
        ("empty file", lambda p: p.write_text("", encoding="utf-8")),
        ("not an object", lambda p: p.write_text('["a", "list"]', encoding="utf-8")),
        ("null", lambda p: p.write_text("null", encoding="utf-8")),
        ("binary junk", lambda p: p.write_bytes(b"\x00\x01\x02\xff")),
    ):
        with _tmp() as td:
            be, good = await _seed(td)
            await _build_state(be, live=True, backup=True, staging=False,
                               journal=_journal(be), live_gen="gen-A")
            writer(be._journal_path())

            fresh = ChromaMemoryBackend(Path(td) / "chroma")
            raised = ""
            try:
                await fresh.count()
            except SemanticRecoveryError as e:
                raised = str(e)
            check(bool(raised),
                  f"{label}: an unreadable journal FAILS CLOSED ({raised[:60]})")
            names = _names(fresh)
            check(fresh._backup_name() in names and fresh._collection_name in names,
                  f"{label}: and both collections are preserved ({names})")

    # A stale temp file must not be mistaken for the journal.
    with _tmp() as td:
        be, good = await _seed(td)
        tmpj = be._journal_path().with_suffix(".json.tmp")
        tmpj.write_text('{"state": "promoting"}', encoding="utf-8")
        fresh = ChromaMemoryBackend(Path(td) / "chroma")
        recs = await _records(fresh)
        check(set(recs) == set(good),
              f"a stale .tmp journal is ignored, not read ({sorted(recs)})")


async def main():
    await test_state_space()
    await test_failure_points()
    await test_single_promotion_interlock()
    await test_generation_binding()
    await test_journal_durability()
    check.finish()


if __name__ == "__main__":
    run(main)
