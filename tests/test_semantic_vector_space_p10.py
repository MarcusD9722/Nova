"""Semantic memory must live in exactly ONE vector space (P10 pre-flight).

The defect this file exists to prevent, measured on main 1c7034c:

`_SemanticEmbeddingFunction._encode()` checked `embedding_available()` on every
call and fell back to a hash embedder per call, on the stated grounds that
"Both produce 384-dim vectors, so they share a collection safely enough for
graceful degradation."

For the SAME text, `cosine(BGE(t), HASH(t)) = -0.0162`. The spaces are
orthogonal. With BGE-written documents in the collection, a hash-embedded query
for "how do I bake a loaf of bread" returned a document about SQLite first, with
no error raised; a hash-embedded WRITE was accepted into the same collection;
and once BGE returned, that document could never be retrieved semantically
again.

Equal dimensionality means the vectors fit in the same table. It says nothing
about what they mean.

These tests use the REAL Chroma backend. Only `embedding_available()` is
toggled — exactly what an import failure, a missing model or an OOM would do.

Run:  venv\\Scripts\\python.exe tests\\test_semantic_vector_space_p10.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

check = Checks()

DOCS = {
    "d1": "Sourdough bread baking needs a starter, flour, water and salt.",
    "d2": "The carburetor mixes air and fuel in an internal combustion engine.",
    "d3": "Photosynthesis converts sunlight into chemical energy in plants.",
    "d4": "SQLite is an embedded relational database engine written in C.",
    "d5": "The migratory patterns of arctic terns span pole to pole.",
}
BREAD_QUERY = "how do I bake a loaf of bread"


class _NoModel:
    """Make the embedding model unavailable, the way an import failure does."""

    def __enter__(self):
        from memory import embeddings as emb
        self._emb = emb
        self._real = emb.embedding_available
        emb.embedding_available = lambda: False
        return self

    def __exit__(self, *exc):
        self._emb.embedding_available = self._real
        return False


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def test_case_a_normal_bge():
    check.section("A: BGE write, BGE query, correct retrieval")
    from memory.backends.chroma_backend import ChromaMemoryBackend
    from memory import embeddings as emb

    check(emb.embedding_available(), "bge-small is loadable on this machine")
    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td))
        for k, v in DOCS.items():
            await be.upsert_text(k, v, {"k": k})
        check(await be.count() == 5, "all five documents stored")
        top = [h["id"] for h in await be.query(BREAD_QUERY, limit=3)]
        check(top[:1] == ["d1"], f"the bread document ranks first ({top})")
        st = be.semantic_status()
        check(st["available"] is True and st["degraded"] is False, "not degraded")
        check(st["writes_skipped"] == 0 and st["queries_skipped"] == 0,
              "nothing was skipped")


async def test_case_b_unavailable_before_write():
    check.section("B: model unavailable BEFORE a write — skip, never substitute")
    from memory.backends.chroma_backend import ChromaMemoryBackend

    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td))
        with _NoModel():
            await be.upsert_text("x1", "Fresh baguettes every morning.", {"k": "x1"})
            st = be.semantic_status()
            check(st["writes_skipped"] == 1,
                  f"the write was SKIPPED, not substituted ({st['writes_skipped']})")
            check(st["degraded"] is True, "and the state says degraded")
            check(st["last_skip_reason"], "with a reason recorded")
        check(await be.count() == 0,
              f"ZERO vectors entered the collection ({await be.count()})")


async def test_case_c_unavailable_before_query():
    check.section("C: model unavailable BEFORE a query — no bogus ranking")
    from memory.backends.chroma_backend import ChromaMemoryBackend

    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td))
        for k, v in DOCS.items():
            await be.upsert_text(k, v, {"k": k})
        good = [h["id"] for h in await be.query(BREAD_QUERY, limit=3)]
        check(good[:1] == ["d1"], "baseline ranking is correct")

        with _NoModel():
            hits = await be.query(BREAD_QUERY, limit=3)
            check(hits == [],
                  f"the query returns NO semantic hits, not a wrong ranking ({hits})")
            check(be.semantic_status()["queries_skipped"] == 1,
                  "and the skip is counted")
        # The collection is untouched and correct once the model returns.
        again = [h["id"] for h in await be.query(BREAD_QUERY, limit=3)]
        check(again == good, f"ranking is intact afterwards ({again})")


async def test_case_d_availability_changes_across_restart():
    check.section("D: availability flips, then the backend is reconstructed")
    from memory.backends.chroma_backend import ChromaMemoryBackend

    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td))
        for k, v in DOCS.items():
            await be.upsert_text(k, v, {"k": k})
        before = await be.count()

        with _NoModel():
            await be.upsert_text("d9", "Croissants are laminated dough.", {"k": "d9"})
            hits = await be.query("laminated dough pastry", limit=3)
            check(hits == [], "degraded query yields nothing")
        check(await be.count() == before,
              f"no hash vector was added while degraded ({await be.count()})")

        # RESTART: a fresh backend over the same directory.
        be2 = ChromaMemoryBackend(Path(td))
        check(await be2.count() == before, "the store reloads with the same count")
        top = [h["id"] for h in await be2.query(BREAD_QUERY, limit=3)]
        check(top[:1] == ["d1"],
              f"and the original BGE data is still correctly retrievable ({top})")


async def test_case_e_legacy_collection_is_not_the_index():
    check.section("E: the legacy nova_memory_v2 collection is never the index")
    import chromadb
    from chromadb.config import Settings

    from memory.backends.chroma_backend import (ChromaMemoryBackend,
                                                _HashEmbeddingFunction,
                                                semantic_collection_name)

    with _tmp() as td:
        # Seed the legacy collection with arbitrary hash vectors, exactly as the
        # old per-call fallback could have.
        client = chromadb.PersistentClient(
            path=td, settings=Settings(persist_directory=td,
                                       anonymized_telemetry=False))
        legacy = client.get_or_create_collection(
            name=ChromaMemoryBackend.LEGACY_COLLECTION,
            embedding_function=_HashEmbeddingFunction(384),
            metadata={"hnsw:space": "cosine"})
        legacy.upsert(ids=["legacy1"], documents=["LEGACY MIXED VECTOR ROW"],
                      metadatas=[{"kind": "fact"}])
        check(legacy.count() == 1, "legacy collection seeded")
        del client, legacy

        be = ChromaMemoryBackend(Path(td))
        check(be._collection_name != ChromaMemoryBackend.LEGACY_COLLECTION,
              f"the backend uses a different collection ({be._collection_name})")
        check(be._collection_name == semantic_collection_name(),
              "namely the vector-space-keyed one")
        check(await be.count() == 0,
              f"which starts EMPTY — legacy rows are not adopted ({await be.count()})")

        for k, v in DOCS.items():
            await be.upsert_text(k, v, {"k": k})
        ids = [h["id"] for h in await be.query("LEGACY MIXED VECTOR ROW", limit=5)]
        check("legacy1" not in ids,
              f"and the legacy row is never returned ({ids})")

        # The legacy data still EXISTS — it is history, not litter.
        client2 = chromadb.PersistentClient(
            path=td, settings=Settings(persist_directory=td,
                                       anonymized_telemetry=False))
        names = [c.name for c in client2.list_collections()]
        check(ChromaMemoryBackend.LEGACY_COLLECTION in names,
              f"the legacy collection was NOT deleted ({names})")
        check(be.semantic_status()["legacy_collection_read"] is False,
              "and status states plainly that it is not read")


async def test_case_f_equal_dimensions_are_not_compatible():
    check.section("F: 384 == 384 must NEVER mean 'same vector space'")
    import numpy as np

    from memory.backends.chroma_backend import (SEMANTIC_DIM,
                                                _HashEmbeddingFunction,
                                                _SemanticEmbeddingFunction,
                                                SemanticUnavailable,
                                                semantic_space_id)
    from memory import embeddings as emb

    h = _HashEmbeddingFunction(SEMANTIC_DIM)
    text = DOCS["d1"]
    hv = np.array(h.embed_query(text), dtype=float)
    bv = np.array(emb.embed_texts([text])[0], dtype=float)
    check(len(hv) == len(bv) == SEMANTIC_DIM,
          f"both embedders produce {SEMANTIC_DIM} dimensions")
    cos = float(np.dot(hv, bv) / (np.linalg.norm(hv) * np.linalg.norm(bv) + 1e-12))
    check(abs(cos) < 0.2,
          f"yet the SAME text embeds to near-orthogonal vectors (cos={cos:.4f}) — "
          f"this is why dimension is not identity")

    # The production embedder must have no fallback left in it.
    sem = _SemanticEmbeddingFunction()
    check(not hasattr(sem, "_fallback"),
          "the semantic embedder holds no fallback embedder")
    with _NoModel():
        raised = False
        try:
            sem.embed_documents(["anything"])
        except SemanticUnavailable:
            raised = True
        check(raised, "and it RAISES rather than substituting another space")

    # The space identity must name the backend and model, not just the size.
    sid = semantic_space_id()
    for part in ("bge", "bge-small", "384"):
        check(part in sid, f"space id mentions {part} ({sid})")
    # Structural, not textual: the class body must not reference the hash
    # embedder at all. (The old false claim is quoted in the docstrings on
    # purpose, so a substring search would only find the explanation.)
    src = (REPO / "memory" / "backends" / "chroma_backend.py").read_text(encoding="utf-8")
    cls = src[src.index("class _SemanticEmbeddingFunction"):
              src.index("class ChromaMemoryBackend")]
    code = "\n".join(l for l in cls.splitlines()
                     if not l.strip().startswith("#"))
    check("_HashEmbeddingFunction(" not in code,
          "the semantic embedder never CONSTRUCTS the hash embedder")
    check("_fallback" not in code, "and holds no fallback attribute")
    check("raise SemanticUnavailable" in code, "it raises instead")


async def test_rebuild_covers_every_semantic_record_class():
    check.section("rebuild: facts, people, events, turns AND documents")
    from memory.unifier import MemoryUnifier

    src = (REPO / "memory" / "unifier.py").read_text(encoding="utf-8")
    body = src[src.index("async def rebuild_semantic_index"):
               src.index("async def ingest_turn")]
    for kind in ("fact", "person", "event", "turn", "document"):
        check(f'"kind": "{kind}"' in body,
              f"the rebuild indexes `{kind}` records")
    check("all_turns" in body and "all_document_chunks" in body,
          "reading both from SQLite, the authoritative store")
    check("embedding_available" in body,
          "and it SKIPS entirely when the model is unavailable, rather than "
          "building a half index")

    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        await m.add_fact(entity="user", attribute="lang", value="Python",
                         confidence=0.9)
        counts = await m.rebuild_semantic_index()
        for key in ("facts", "people", "events", "turns", "documents"):
            check(key in counts, f"the result reports `{key}` ({counts})")
        health = m.semantic_index_health()
        check("semantic" in health, "health exposes the semantic block")
        sem = health["semantic"]
        for key in ("backend", "model", "collection", "space_id", "available",
                    "degraded", "writes_skipped", "queries_skipped"):
            check(key in sem, f"health reports `{key}`")
        check(sem["collection"].startswith("nova_sem_bge_"),
              f"and names the space-keyed collection ({sem['collection']})")


async def test_degraded_memory_still_works_through_the_unifier():
    check.section("a BGE outage must not become total memory failure")
    from memory.unifier import MemoryUnifier

    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        with _NoModel():
            # SQLite writes must still succeed.
            await m.add_fact(entity="user", attribute="editor", value="neovim",
                             confidence=0.9)
            got = await m.get_latest_fact(entity="user", attribute="editor")
            check(got is not None and got.value == "neovim",
                  "a fact written during the outage is durably stored")
            health = m.semantic_index_health()
            check(health["degraded"] is True,
                  "health reports degraded while the model is down")
            check(health["semantic"]["available"] is False,
                  "and says the backend is unavailable")
        # And it is recoverable: a rebuild restores semantic coverage.
        counts = await m.rebuild_semantic_index()
        check(counts.get("facts", 0) >= 1,
              f"a rebuild re-indexes what was written while degraded ({counts})")


async def main():
    await test_case_a_normal_bge()
    await test_case_b_unavailable_before_write()
    await test_case_c_unavailable_before_query()
    await test_case_d_availability_changes_across_restart()
    await test_case_e_legacy_collection_is_not_the_index()
    await test_case_f_equal_dimensions_are_not_compatible()
    await test_rebuild_covers_every_semantic_record_class()
    await test_degraded_memory_still_works_through_the_unifier()
    check.finish()


if __name__ == "__main__":
    run(main)
