"""Full-audit coverage for memory/backends/chroma_backend.py (previously none).

Chroma is the rebuildable semantic index over SQLite's truth. Its failure mode
is the quiet kind: a query that returns nothing looks identical to a query with
no matches, so recall silently degrades and nobody notices.

`_SemanticEmbeddingFunction` used to fall back to the hash embedder per call when
`embedding_available()` was False, and this suite forced that globally so the real
Chroma plumbing could be exercised without pulling bge-small onto the GPU. That
fallback was a corruption — two orthogonal vector spaces in one collection — and
it is gone. The embedder now fails closed, so a globally-unavailable model would
make every write here a skip, and `test_backend` would be asserting nothing.

So the backend tests run against the REAL embedder, and unavailability is scoped
to the one test that asserts the fail-closed contract. Degraded-mode backend
behaviour (skip counters, empty query results, rebuild) is covered in
tests/test_semantic_vector_space_p10.py.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

from memory import embeddings as emb_mod
from memory.backends.chroma_backend import (
    ChromaMemoryBackend, SemanticUnavailable, _HashEmbeddingFunction,
    _SemanticEmbeddingFunction,
)

check = Checks()

_real_available = emb_mod.embedding_available


async def test_hash_embedder() -> None:
    check.section("_HashEmbeddingFunction (the offline fallback)")
    f = _HashEmbeddingFunction(dim=384)

    v = f.embed_query("hello world")
    check(len(v) == 384, "embed_query returns the configured dimension")
    check(abs(sum(x * x for x in v) - 1.0) < 1e-4, "vectors are L2-normalized")
    check(f.embed_query("hello world") == v, "the same text always embeds identically")
    check(f.embed_query("HELLO WORLD") == v, "embedding is case-insensitive")
    check(f.embed_query("totally different") != v, "different text embeds differently")

    check(f.embed_query("") == [0.0] * 384, "empty text yields a zero vector, not a crash")
    check(f.embed_query(None) == [0.0] * 384, "None yields a zero vector, not a crash")
    check(len(f.embed_query(["a", "b"])) == 384, "a list input is joined rather than rejected")

    docs = f.embed_documents(["one", "two", "three"])
    check(len(docs) == 3 and all(len(d) == 384 for d in docs), "embed_documents maps over the batch")
    check(f.embed_documents([]) == [], "an empty batch returns an empty list")
    check(f(["x"]) == f.embed_documents(["x"]), "__call__ matches embed_documents")

    long_text = " ".join(str(i) for i in range(5000))
    check(len(f.embed_query(long_text)) == 384, "a 5000-token input is truncated, not fatal")

    check(f.name() == "hash-embed-v1", "name() identifies the embedder")
    check(f.get_config() == {"dim": 384}, "get_config reports the dimension")


async def test_semantic_fails_closed() -> None:
    """This test used to assert the bug.

    It read: "it degrades to the hash embedder instead of raising", and it passed
    for exactly as long as the corruption was live. Silently substituting an
    orthogonal vector space is not degradation — measured on the same text,
    cosine(BGE, HASH) = -0.0162. The contract is now the opposite one, and the
    assertion is inverted rather than deleted so the old behaviour cannot come
    back unnoticed. See tests/test_semantic_vector_space_p10.py for the full
    corruption cases.
    """
    check.section("_SemanticEmbeddingFunction with the model unavailable")
    s = _SemanticEmbeddingFunction(dim=384)
    h = _HashEmbeddingFunction(dim=384)
    emb_mod.embedding_available = lambda: False   # scoped to this test only
    try:
        s.embed_query("anything")
    except SemanticUnavailable as e:
        raised, msg = True, str(e)
    else:
        raised, msg = False, ""
    check(raised, "it RAISES SemanticUnavailable rather than substituting a hash vector")
    check("unavailable" in msg and "SQLite" in msg,
          f"and says what was skipped and what was not ({msg[:80]!r})")

    try:
        s.embed_documents(["a", "b"])
    except SemanticUnavailable:
        batch_raised = True
    else:
        batch_raised = False
    check(batch_raised, "the batch path fails closed too — no partial writes")

    # The hash embedder still exists and still works: it is used for the
    # in-memory tool-selector cache, which is a fresh non-persistent space every
    # boot. It just may never reach the persistent collection.
    check(len(h.embed_query("anything")) == 384,
          "the hash embedder itself is untouched (still used off the persistent path)")
    check(s.name() == "nova-semantic-bge-normalized-cls-v1",
          f"the semantic embedder names its vector space ({s.name()})")
    emb_mod.embedding_available = _real_available


async def test_backend(tmp: Path) -> None:
    check.section("ChromaMemoryBackend")
    be = ChromaMemoryBackend(tmp / "chroma", collection_name="audit_test")

    check(await be.count() == 0, "a fresh collection is empty")
    check(await be.query("anything") == [], "querying an empty collection returns [], not a raise")

    await be.upsert_text("f1", "Marcus likes oat milk in his coffee", {"kind": "fact"})
    await be.upsert_text("f2", "Leslie is his wife", {"kind": "fact"})
    check(await be.count() == 2, "upserts are counted")

    hits = await be.query("coffee", limit=5)
    check(len(hits) > 0, "query returns hits")
    top = hits[0]
    check(set(top) >= {"id", "text", "metadata", "distance"}, f"hit shape is complete ({sorted(top)})")
    check(isinstance(top["distance"], float), "distance is a float")
    check(top["metadata"].get("kind") == "fact", "metadata round-trips")

    # Upsert semantics: same id must REPLACE, never duplicate. A silent
    # duplicate here would double-count a fact in every future recall.
    await be.upsert_text("f1", "Marcus switched to black coffee", {"kind": "fact"})
    check(await be.count() == 2, "re-upserting an existing id does not add a row")
    texts = [h["text"] for h in await be.query("coffee", limit=5)]
    check(any("black coffee" in t for t in texts), "the updated text is what comes back")
    check(not any("oat milk" in t for t in texts), "the superseded text is gone")

    check(len(await be.query("coffee", limit=1)) <= 1, "limit is honored")

    # Regression: the signature's own default (metadata=None -> {}) raised
    # "Expected metadata to be a non-empty dict" from chromadb.
    await be.upsert_text("no_meta", "a document with no metadata at all")
    check(await be.count() == 3, "upsert with the DEFAULT metadata=None works")
    await be.upsert_text("empty_meta", "another one", {})
    check(await be.count() == 4, "upsert with an explicitly empty metadata dict works")
    got = [h for h in await be.query("no metadata at all", limit=5) if h["id"] == "no_meta"]
    check(got and got[0]["metadata"] == {}, "a metadata-less doc reads back with an empty dict")

    await be.upsert_text("", "ignored", {"k": "v"})
    await be.upsert_text("   ", "ignored", {"k": "v"})
    check(await be.count() == 4, "a blank id is refused, not stored as a phantom row")

    await be.delete_ids(["f2"])
    check(await be.count() == 3, "delete_ids removes the row")
    await be.delete_ids([])
    await be.delete_ids(["does-not-exist"])
    check(await be.count() == 3, "deleting nothing / a missing id is a no-op, not a raise")

    await be.upsert_text("f3", "third entry", {"kind": "x"})
    check(await be.count() == 4, "still writable after deletes")
    await be.reset()
    check(await be.count() == 0, "reset empties the collection")
    await be.upsert_text("after", "still works", {"kind": "x"})
    check(await be.count() == 1, "the backend is usable again after reset")

    # Concurrency: the backend serializes on a lock. Chroma is not
    # thread-safe, so a missing lock would corrupt or deadlock here.
    await asyncio.gather(*(be.upsert_text(f"c{i}", f"entry number {i}", {"i": str(i)}) for i in range(25)))
    check(await be.count() == 26, f"25 concurrent upserts all landed (count={await be.count()})")
    results = await asyncio.gather(*(be.query(f"entry number {i}", limit=3) for i in range(10)))
    check(all(len(r) > 0 for r in results), "10 concurrent queries all return results")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        await test_hash_embedder()
        await test_semantic_fails_closed()
        await test_backend(Path(td))
    check.finish()


asyncio.run(main())
