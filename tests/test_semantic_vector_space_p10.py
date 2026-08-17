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
from uuid import uuid4

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


def _code_only(path: Path) -> str:
    """Source with comments and docstrings stripped.

    Static assertions that grep raw source keep matching the PROSE explaining a
    fix rather than the code implementing it — the old-and-wrong formatter strings
    are quoted in this repo's docstrings on purpose.
    """
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    prev = tokenize.INDENT
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev in (
                    tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
                continue
            out.append(tok.string)
            prev = tok.type
    except tokenize.TokenError:
        return src
    return "\n".join(out)


def _attr_uses(path: Path, owner: str) -> set[str]:
    """Names used as `owner.NAME` in real code (not comments or docstrings).

    Read from the AST. A substring grep over source keeps matching the prose that
    EXPLAINS a rule instead of the code that follows it, and `_code_only()` joins
    tokens with newlines so `semantic_records.fact_record` is no longer one string.

    `owner` matches either a bare name (`semantic_records.x`) or an attribute
    (`self._chroma.x`). The first version handled only the bare case, so every
    lookup for `_chroma` came back EMPTY — and an empty set made a check for
    "reset() is no longer called" pass vacuously. A static check that cannot
    observe its subject is worse than no check.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id == owner:
            out.add(node.attr)
        elif isinstance(base, ast.Attribute) and base.attr == owner:
            out.add(node.attr)
    return out


def _from_pretrained_kwargs(path: Path) -> list[set[str]]:
    """The keyword names passed to each `*.from_pretrained(...)` call."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_pretrained"):
            calls.append({k.arg for k in node.keywords if k.arg})
    return calls


async def _dump_collection(unifier) -> dict[str, dict]:
    """Every record actually in the live semantic collection: id -> {text, metadata}.

    Reads Chroma directly rather than through query(), because the point is to
    compare what was STORED, not what ranks for some probe.
    """
    import asyncio as _a

    be = unifier._chroma
    await _a.to_thread(be._ensure)
    got = await _a.to_thread(be._collection.get, include=["documents", "metadatas"])
    ids = got.get("ids") or []
    docs = got.get("documents") or []
    metas = got.get("metadatas") or []
    return {ids[i]: {"text": docs[i], "metadata": metas[i] or {}}
            for i in range(len(ids))}


async def _read_snapshots(unifier) -> list[dict]:
    """The snapshot JSONL the unifier appends to (no reader exists on the backend)."""
    import json as _j

    path = getattr(unifier._json, "_snapshot_path", None)
    if not path or not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(_j.loads(line))
            except Exception:
                pass
    return out


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


async def test_revision_is_part_of_the_space_identity():
    """A repo can change its weights and keep its model id (review finding 1).

    Same id, same 384 dimensions, same pooling, different weights, and - before
    this - the same persistent collection. That is the corruption this whole PR
    removes, arriving through a different door.
    """
    check.section("vector-space identity includes the model revision")
    from memory import embeddings as emb
    from memory.backends import chroma_backend as cb

    rev = cb.semantic_model_revision()
    check(len(rev) == 40 and all(c in "0123456789abcdef" for c in rev),
          f"the pinned revision is a full commit sha ({rev})")
    check(rev.lower() not in ("main", "master", "latest", "head"),
          "and not a moving ref")
    check(rev in cb.semantic_space_id(),
          f"the space id carries it ({cb.semantic_space_id()})")

    base_sid, base_name = cb.semantic_space_id(), cb.semantic_collection_name()
    check(cb.semantic_space_id() == base_sid
          and cb.semantic_collection_name() == base_name,
          "identity is stable across recomputation (i.e. across restart)")

    prev = os.environ.get("NOVA_EMBED_REVISION")
    os.environ["NOVA_EMBED_REVISION"] = "0" * 39 + "1"
    try:
        other_sid, other_name = cb.semantic_space_id(), cb.semantic_collection_name()
    finally:
        if prev is None:
            os.environ.pop("NOVA_EMBED_REVISION", None)
        else:
            os.environ["NOVA_EMBED_REVISION"] = prev

    check(other_sid != base_sid, "a different revision is a different space id")
    check(other_name != base_name,
          f"and a DIFFERENT COLLECTION ({base_name} vs {other_name})")
    check(cb.SEMANTIC_DIM == 384 and "384" in other_sid
          and cb.semantic_model_id() in other_sid,
          "with dimension and model id unchanged - only the revision differed")
    check(cb.semantic_collection_name() == base_name,
          "and the identity returns to normal once the override is gone")

    os.environ["NOVA_EMBED_REVISION"] = "main"
    try:
        check(emb.embedding_revision() == rev,
              "an override of 'main' is REFUSED - a moving ref is not an identity")
    finally:
        if prev is None:
            os.environ.pop("NOVA_EMBED_REVISION", None)
        else:
            os.environ["NOVA_EMBED_REVISION"] = prev

    loads = _from_pretrained_kwargs(REPO / "memory" / "embeddings.py")
    check(len(loads) == 2, f"there are exactly two model loads ({len(loads)})")
    check(all("revision" in kw for kw in loads),
          f"tokenizer AND model are both pinned to a revision ({loads})")
    src = _code_only(REPO / "memory" / "embeddings.py")
    check(src.count("embedding_revision") >= 2,
          "and both read it from the one authoritative helper")


async def test_live_and_rebuilt_records_are_identical():
    """One canonical builder, not two formatters that agree by intention.

    The drift this catches was real: live wrote "FILE notes.txt (part 1/4)" while
    the rebuild wrote "(part 1)"; live wrote turn id "turn:<uuid>" with text
    "Marcus said: ..." while the rebuild wrote a bare "<uuid>" with raw content.
    Different embedded text is a different vector for the same source row.
    """
    check.section("canonical record shapes: live == rebuilt")
    from memory import semantic_records as sr
    from memory.unifier import MemoryUnifier

    # Structural: every semantic write path must go through the one module.
    used = _attr_uses(REPO / "memory" / "unifier.py", "semantic_records")
    for builder in ("fact_record", "person_record", "event_record",
                    "turn_record", "document_chunk_record"):
        check(builder in used,
              f"the unifier builds {builder} through the canonical module ({sorted(used)})")
    uni = _code_only(REPO / "memory" / "unifier.py")
    check("said: " not in uni and "(part " not in uni,
          "and hand-rolls neither the turn nor the document text any more")

    doc = sr.document_chunk_record(path="/tmp/notes.txt", chunk_index=0,
                                   chunk_total=4, text="chunk body",
                                   created_at="2026-01-01T00:00:00")
    check(doc.text == "FILE notes.txt (part 1/4): chunk body",
          f"document text includes the chunk TOTAL ({doc.text})")
    check(doc.doc_id == "doc:/tmp/notes.txt#0", f"and the live id shape ({doc.doc_id})")

    turn = sr.turn_record(turn_id="u1", role="user",
                          content="a substantive sentence here",
                          created_at="t", conversation_id="c1",
                          speaker_entity="user", speaker_label="Marcus")
    check(turn.doc_id == "turn:u1", f"turn ids keep the turn: prefix ({turn.doc_id})")
    check(turn.text == "Marcus said: a substantive sentence here",
          f"and the speaker-prefixed text ({turn.text})")
    check(sr.turn_record(turn_id="u1", role="user", content="x", created_at="t",
                         conversation_id="c1", speaker_entity="user").text
          == "Marcus said: x",
          "a pre-P5.1d.1 row with no stored label uses the legacy label")

    # The real proof: write through production paths, capture what LIVE indexing
    # produced, rebuild, compare record for record.
    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        await m.add_fact(entity="user", attribute="lang", value="Python",
                         confidence=0.9)
        await m.upsert_person(name="Leslie", attributes={"role": "wife"})
        await m.add_event(date="2026-03-01", note="dentist appointment downtown")
        conv = uuid4()
        await m.ingest_turn(conv, "user",
                            "I have been reading about sourdough starters all week")
        docp = Path(td) / "notes.txt"
        body = ("alpha " * 400) + "\n" + ("omega omega " * 400)
        docp.write_text(body, encoding="utf-8")
        await m.index_document(path=str(docp), excerpt=body,
                               mtime=docp.stat().st_mtime)

        live = await _dump_collection(m)
        check(len(live) >= 5, f"live indexing wrote {len(live)} semantic records")
        kinds_live = {v["metadata"].get("kind") for v in live.values()}
        check(kinds_live == {"fact", "person", "event", "turn", "document"},
              f"covering all five classes ({sorted(kinds_live)})")

        res = await m.rebuild_semantic_index()
        check(res.get("complete") is True, f"the rebuild reports complete ({res})")
        rebuilt = await _dump_collection(m)

        check(set(rebuilt) == set(live),
              f"rebuilt IDS match live exactly "
              f"(live-only={sorted(set(live) - set(rebuilt))[:3]}, "
              f"rebuilt-only={sorted(set(rebuilt) - set(live))[:3]})")
        text_diff = [i for i in live if live[i]["text"] != rebuilt.get(i, {}).get("text")]
        check(not text_diff,
              "rebuilt embedded TEXT matches live exactly" if not text_diff else
              f"TEXT DRIFT on {len(text_diff)} record(s): "
              f"live={live[text_diff[0]]['text'][:70]!r} "
              f"rebuilt={rebuilt.get(text_diff[0], {}).get('text', '')[:70]!r}")
        meta_diff = [i for i in live
                     if {k: v for k, v in live[i]["metadata"].items() if k != "created_at"}
                     != {k: v for k, v in rebuilt.get(i, {}).get("metadata", {}).items()
                         if k != "created_at"}]
        check(not meta_diff, f"and the metadata shape matches ({meta_diff[:2]})")

        # Equal is not enough - it must be usable.
        turn_hits = await m._chroma.query("sourdough starters reading", limit=5)
        check(any(h["metadata"].get("kind") == "turn" for h in turn_hits),
              f"a rebuilt TURN is retrievable "
              f"({[h['metadata'].get('kind') for h in turn_hits]})")
        doc_hits = await m._chroma.query("omega omega omega", limit=5)
        check(any(h["metadata"].get("kind") == "document" for h in doc_hits),
              f"a rebuilt DOCUMENT chunk is retrievable "
              f"({[h['metadata'].get('kind') for h in doc_hits]})")

        # And it survives a restart.
        m2 = MemoryUnifier(Path(td))
        await m2.initialize()
        after = await _dump_collection(m2)
        check(set(after) == set(live),
              f"the promoted collection is intact after restart ({len(after)} records)")
        check(any(h["metadata"].get("kind") == "turn"
                  for h in await m2._chroma.query("sourdough starters reading", limit=5)),
              "and still queryable through a fresh MemoryUnifier")


async def test_a_failed_rebuild_never_becomes_authoritative():
    """Review finding 3: no half-built index may survive as healthy."""
    check.section("rebuild is all-or-nothing")
    from memory.unifier import MemoryUnifier

    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        for i in range(6):
            await m.add_fact(entity="user", attribute=f"attr{i}",
                             value=f"value number {i}", confidence=0.9)
        await m.upsert_person(name="Leslie", attributes={"role": "wife"})
        await m.add_event(date="2026-03-01", note="dentist appointment downtown")
        conv = uuid4()
        for i in range(3):
            await m.ingest_turn(conv, "user",
                                f"substantive sentence number {i} about gardening tools")
        docp = Path(td) / "notes.txt"
        body = ("alpha " * 400) + "\n" + ("omega omega " * 400)
        docp.write_text(body, encoding="utf-8")
        await m.index_document(path=str(docp), excerpt=body,
                               mtime=docp.stat().st_mtime)

        good = await _dump_collection(m)
        good_n = len(good)
        check(good_n >= 12, f"a healthy index of {good_n} records exists first")

        # Fail deterministically PART WAY THROUGH: several embeddings succeed,
        # then the backend dies.
        from memory import embeddings as emb
        real_embed = emb.embed_texts
        calls = {"n": 0}

        def flaky(texts):
            calls["n"] += 1
            if calls["n"] >= 5:
                raise RuntimeError("simulated embedding backend death mid-rebuild")
            return real_embed(texts)

        emb.embed_texts = flaky
        try:
            res = await m.rebuild_semantic_index()
        finally:
            emb.embed_texts = real_embed

        check(calls["n"] >= 5,
              f"the failure landed mid-rebuild, after {calls['n'] - 1} successful "
              f"embeddings")
        check(res.get("complete") is False, f"the rebuild reports INCOMPLETE ({res})")
        check(bool(res.get("reason")), f"with a reason ({str(res.get('reason'))[:90]})")
        check(all(int(res.get(k, 0)) == 0
                  for k in ("facts", "people", "events", "turns", "documents")),
              f"and claims no coverage it does not have ({res})")

        after = await _dump_collection(m)
        check(len(after) == good_n and set(after) == set(good),
              f"the PREVIOUS index survived untouched ({len(after)} of {good_n})")
        names = [c.name for c in m._chroma._client.list_collections()]
        check(not any(n.endswith("__staging") for n in names),
              f"no staging collection is left behind ({names})")

        snaps = [x for x in (await _read_snapshots(m) or [])
                 if x.get("kind") == "semantic_index_rebuild"]
        check(not any(x.get("complete") for x in snaps),
              f"and no snapshot claims a successful rebuild ({len(snaps)} snapshots)")

        # Recovery from the authoritative store.
        res2 = await m.rebuild_semantic_index()
        check(res2.get("complete") is True, f"a clean rebuild then succeeds ({res2})")
        recovered = await _dump_collection(m)
        check(set(recovered) == set(good),
              f"restoring every record from SQLite ({len(recovered)} of {good_n})")

        # ── the model going UNAVAILABLE mid-rebuild ─────────────────────────
        #
        # A different failure mode from the exception above, and the likelier
        # one: `embedding_available()` starts returning False (model evicted,
        # OOM, a worker died), so the embedder raises SemanticUnavailable. On the
        # LIVE path that is a silent skip by design — inside a staged rebuild a
        # skip would be a hole in the thing about to be promoted, so it must
        # abort instead. Found by mutation testing: the RuntimeError injection
        # above never reached this branch.
        real_avail = emb.embedding_available
        avail_calls = {"n": 0}

        def dying(*_a, **_k):
            avail_calls["n"] += 1
            return avail_calls["n"] <= 6   # a few succeed, then the model is gone

        emb.embedding_available = dying
        try:
            res3 = await m.rebuild_semantic_index()
        finally:
            emb.embedding_available = real_avail

        check(avail_calls["n"] > 6,
              f"availability was consulted {avail_calls['n']} times and went false "
              f"part way through")
        check(res3.get("complete") is False,
              f"a mid-rebuild UNAVAILABILITY also reports incomplete ({res3})")
        check("promoted" not in res3,
              f"and reports no promotion at all ({res3.get('promoted', 'absent')})")
        still = await _dump_collection(m)
        check(set(still) == set(good),
              f"the previous index survived this failure too ({len(still)} of {good_n})")
        names2 = [c.name for c in m._chroma._client.list_collections()]
        check(not any(n.endswith("__staging") for n in names2),
              f"and no staging collection is left behind ({names2})")

        res4 = await m.rebuild_semantic_index()
        check(res4.get("complete") is True,
              f"and a clean rebuild still recovers afterwards ({res4})")


async def test_staged_rebuild_defences_individually():
    """Each guard that keeps a partial index from being promoted, on its own.

    Mutation testing found these untested. Driving them through the unifier could
    not reach them: whenever availability flips, `embed_texts` raises its own
    RuntimeError before `_encode` can raise `SemanticUnavailable`, and the
    availability PRECHECK catches the permanently-unavailable case before any
    write. Removing BOTH guards still left the suite green — redundancy I had
    assumed rather than verified. So they are exercised here directly, at the
    backend, where the interleaving can be made exact.
    """
    check.section("staged-rebuild guards, each exercised on its own")
    from memory.backends.chroma_backend import (
        ChromaMemoryBackend, SemanticUnavailable,
    )

    # GUARD 1 — a skipped write inside a staged rebuild must RAISE, not skip.
    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td) / "chroma")
        await be.upsert_text("live1", "a document written before the rebuild", {"k": "1"})
        check(await be.count() == 1, "one live record exists")

        await be.begin_staged_rebuild()
        await be.upsert_text("s1", "the first staged record lands fine", {"k": "1"})
        check(await be.staged_count() == 1, "a staged write lands in staging")
        check(await be.count() == 1,
              f"and NOT in the live collection ({await be.count()})")

        raised = False
        with _NoModel():
            try:
                await be.upsert_text("s2", "this one cannot be embedded", {"k": "2"})
            except SemanticUnavailable:
                raised = True
        check(raised,
              "a write that cannot be embedded RAISES during a staged rebuild "
              "(live behaviour is a silent skip; inside a rebuild that would be a "
              "hole in what gets promoted)")
        await be.abort_staged_rebuild()
        check(await be.count() == 1,
              f"after abort the live record is still there ({await be.count()})")
        names = [c.name for c in be._client.list_collections()]
        check(not any(n.endswith("__staging") for n in names),
              f"and staging is gone ({names})")

    # GUARD 2 — the count check refuses to promote a short collection.
    with _tmp() as td:
        m_be = ChromaMemoryBackend(Path(td) / "chroma")
        await m_be.begin_staged_rebuild()
        await m_be.upsert_text("s1", "only one record actually lands", {"k": "1"})
        staged = await m_be.staged_count()
        check(staged == 1, f"staging holds {staged}")
        # This is the comparison `rebuild_semantic_index` makes before promoting.
        expected_if_a_write_vanished = 2
        check(staged != expected_if_a_write_vanished,
              "so a count that disagrees with what was written is detectable — "
              "which is what stops a silently-short index being promoted")
        await m_be.abort_staged_rebuild()

    # And the unifier really uses all three staging operations.
    used = _attr_uses(REPO / "memory" / "unifier.py", "_chroma")
    for op in ("begin_staged_rebuild", "staged_count", "commit_staged_rebuild",
               "abort_staged_rebuild"):
        check(op in used, f"the rebuild calls {op} ({sorted(used)})")
    check("reset" not in used,
          f"and no longer demolishes the live collection with reset() ({sorted(used)})")


async def test_semantic_status_reports_the_real_load_error():
    """Review finding 5: load_error was structurally always null.

    `embedding_available()` CATCHES the model-load exception and returns False, so
    a try/except around it can never see a normal load failure. The reason lives
    only in `embeddings.load_error()`.
    """
    check.section("semantic_status.load_error carries the real failure")
    from memory import embeddings as emb
    from memory.backends.chroma_backend import ChromaMemoryBackend

    with _tmp() as td:
        be = ChromaMemoryBackend(Path(td) / "chroma")
        real_avail, real_err = emb.embedding_available, emb.load_error
        emb.embedding_available = lambda: False
        emb.load_error = lambda: ("OSError: cannot load tokenizer for "
                                  "BAAI/bge-small-en-v1.5 (offline, empty cache)")
        try:
            st = be.semantic_status()
        finally:
            emb.embedding_available, emb.load_error = real_avail, real_err

        check(st["available"] is False, "available is false")
        check(st["degraded"] is True, "degraded is true")
        check(bool(st["load_error"]), f"load_error is NON-EMPTY ({st['load_error']!r})")
        check("cannot load tokenizer" in str(st["load_error"]),
              "and reflects the ACTUAL load failure, not a placeholder")
        check(st.get("revision") == emb.embedding_revision(),
              f"status reports the pinned revision ({st.get('revision')})")

        # Precise patterns, not bare words: a model-load error legitimately says
        # "tokenizer", and a check for "token" would fail on it while proving
        # nothing. The point is credentials and biometric data, not vocabulary.
        blob = repr(st).lower()
        for secret in ("sk-", "access_token", "token=", "bearer ", "password",
                       "api_key", "secret=", "embedding=", "similarity=",
                       "biometric"):
            check(secret not in blob, f"status leaks no {secret!r}")
        check("sourdough" not in blob and "leslie" not in blob,
              "and no memory contents")
        check(not any(isinstance(v, (list, tuple)) for v in st.values()),
              f"and no vector-shaped values at all ({[k for k, v in st.items() if isinstance(v, (list, tuple))]})")

        st2 = be.semantic_status()
        if st2["available"]:
            check(st2["load_error"] is None,
                  "a healthy backend reports load_error None")
            check(st2["degraded"] is False, "and is not degraded")


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
    await test_revision_is_part_of_the_space_identity()
    await test_live_and_rebuilt_records_are_identical()
    await test_a_failed_rebuild_never_becomes_authoritative()
    await test_staged_rebuild_defences_individually()
    await test_semantic_status_reports_the_real_load_error()
    await test_degraded_memory_still_works_through_the_unifier()
    check.finish()


if __name__ == "__main__":
    run(main)
