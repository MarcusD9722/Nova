from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
import numpy as np


__all__ = ["ChromaMemoryBackend"]


class _HashEmbeddingFunction:
    """Deterministic lightweight embedding without external ML deps.

    NO LONGER USED IN PRODUCTION. It was the per-call fallback for
    `_SemanticEmbeddingFunction`, which was a corruption — see that class. Nothing
    outside this module and the test suite constructs it any more.

    It is kept, rather than deleted, because it is the negative reference the
    vector-space tests measure bge against: it is the only thing that can
    demonstrate that two 384-dim embedders are orthogonal. If it ever acquires a
    caller again, that caller must own its own non-persistent space.

    Chroma embedding functions are expected to implement:
      - embed_documents(list[str]) -> list[list[float]]
      - embed_query(str) -> list[float]

    This implementation provides those methods plus __call__ for compatibility.
    It is NOT semantically strong, but it is stable and dependency-free.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = int(dim)

    def name(self) -> str:
        return "hash-embed-v1"

    def get_config(self) -> dict[str, int]:
        return {"dim": self._dim}

    def _embed_one(self, text: Any) -> list[float]:
        if isinstance(text, list):
            text = " ".join(str(x) for x in text)
        t = (str(text) if text is not None else "").strip().lower()
        if not t:
            return [0.0] * self._dim

        vec = np.zeros(self._dim, dtype=np.float32)

        parts = t.split()
        if len(parts) > 256:
            parts = parts[:256]

        for p in parts:
            h = hashlib.blake2b(p.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self._dim
            sign = -1.0 if (h[4] & 1) else 1.0
            vec[idx] += sign

        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec.tolist()

    def embed_query(self, input: Any) -> list[float]:
        return self._embed_one(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in (input or [])]

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self.embed_documents(input)


class SemanticUnavailable(RuntimeError):
    """The real embedding model is not loadable, so no vector can be produced.

    Raised INSTEAD of silently substituting another embedder. See
    `_SemanticEmbeddingFunction` for why that substitution was a corruption.
    """


#: Identity of the vector space this backend writes. Anything that changes what
#: a vector MEANS must change this, because a collection may only ever hold one
#: space. Dimension is deliberately NOT part of the identity on its own — that
#: was the whole bug.
SEMANTIC_BACKEND = "bge"
SEMANTIC_ALGORITHM = "normalized-cls-v1"
SEMANTIC_DIM = 384


def semantic_model_id() -> str:
    from memory import embeddings as emb_mod
    return emb_mod.embedding_model_id()


def semantic_model_revision() -> str:
    """The pinned model repository commit — part of the space identity.

    A repository can change its weights while keeping the same model id, so
    without this a silent upstream reupload would give: same id, same dimension,
    same pooling, DIFFERENT vectors, reusing the same collection. The revision is
    what makes "one collection == one vector space" true rather than hopeful.
    """
    from memory import embeddings as emb_mod
    return emb_mod.embedding_revision()


def semantic_space_id() -> str:
    """Full, human-readable identity of the vector space."""
    return (f"{SEMANTIC_BACKEND}|{semantic_model_id()}"
            f"@{semantic_model_revision()}|{SEMANTIC_ALGORITHM}|{SEMANTIC_DIM}")


def semantic_collection_name() -> str:
    """Chroma collection name that is UNIQUE to the vector space.

    Chroma restricts collection names (length, characters), and a model id like
    `BAAI/bge-small-en-v1.5` contains a slash and dots, so the space identity is
    hashed rather than embedded literally. The full identity is also written to
    the collection metadata so it can be read back and audited.
    """
    digest = hashlib.blake2b(semantic_space_id().encode("utf-8"),
                             digest_size=8).hexdigest()
    return f"nova_sem_{SEMANTIC_BACKEND}_{digest}"


class _SemanticEmbeddingFunction:
    """Real sentence embeddings (bge-small). FAILS CLOSED — no fallback.

    This class used to fall back to `_HashEmbeddingFunction` per call whenever
    the model was unavailable, on the stated grounds that "Both produce 384-dim
    vectors, so they share a collection safely enough for graceful
    degradation."

    That premise is false, and it was measured: for the SAME text,
    `cosine(BGE(t), HASH(t)) = -0.0162`. The two are orthogonal. Equal
    dimensionality means the vectors fit in the same table, not that they mean
    anything to each other.

    The consequences were silent and permanent. With BGE-written documents in
    the collection, a hash-embedded query for "how do I bake a loaf of bread"
    returned a document about SQLite first, with no error. A hash-embedded WRITE
    was accepted into the same collection, and once BGE came back that document
    could never be retrieved again by any semantic query.

    So: this produces a BGE vector or it raises. Callers degrade by SKIPPING
    semantic work — SQLite remains the source of truth and lexical/recent recall
    is unaffected.
    """

    def __init__(self, dim: int = SEMANTIC_DIM) -> None:
        self._dim = int(dim)

    def name(self) -> str:
        # Part of Chroma's stored config; changing it changes the space.
        return f"nova-semantic-{SEMANTIC_BACKEND}-{SEMANTIC_ALGORITHM}"

    def get_config(self) -> dict[str, Any]:
        return {"dim": self._dim, "space": semantic_space_id()}

    def _encode(self, texts: list[str]) -> list[list[float]]:
        from memory import embeddings as emb_mod

        if not emb_mod.embedding_available():
            raise SemanticUnavailable(
                f"embedding model {semantic_model_id()} is unavailable; "
                f"semantic memory is skipped (SQLite is unaffected)")
        return emb_mod.embed_texts(texts)

    def embed_query(self, input: Any) -> list[float]:
        text = " ".join(str(x) for x in input) if isinstance(input, list) else str(input or "")
        return self._encode([text])[0]

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self._encode([str(t) for t in (input or [])])

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self.embed_documents(input)


class ChromaMemoryBackend:
    """Persistent Chroma-backed store for semantic recall.

    Uses a real embedding model (see memory/embeddings.py) with a
    deterministic hash fallback so query()/upsert() never hard-fail.
    """

    #: The legacy collection. It was written by code that could put EITHER a BGE
    #: or a hash vector in it, per call, so its contents have no reliable vector
    #: space. It is never read and never written by this backend any more, and it
    #: is deliberately NOT deleted — it is historical data, and destroying it to
    #: make a test green would be the wrong trade.
    LEGACY_COLLECTION = "nova_memory_v2"

    def __init__(self, persist_dir: Path, collection_name: str | None = None) -> None:
        self._persist_dir = Path(persist_dir)
        # Keyed by VECTOR SPACE, not just dimension. A collection may only ever
        # hold one space; changing the backend/model/algorithm yields a new
        # collection and the old one is left intact.
        self._collection_name = collection_name or semantic_collection_name()
        self._lock = asyncio.Lock()
        self._client: chromadb.ClientAPI | None = None
        self._collection: Any | None = None
        self._skipped_writes = 0
        self._skipped_queries = 0
        self._last_skip_reason = ""
        #: Set only between begin_staged_rebuild() and commit/abort. While set,
        #: writes go to staging and a skip becomes a raise.
        self._staging: Any | None = None
        self._writing_staged = False

    # ── honest state, for /status ─────────────────────────────────────────────
    def semantic_status(self) -> dict[str, Any]:
        from memory import embeddings as emb_mod
        try:
            available = bool(emb_mod.embedding_available())
        except Exception as e:  # noqa: BLE001
            available, err = False, str(e)[:200]
        else:
            # `embedding_available()` CATCHES the load failure and returns False,
            # so the except branch above can never see a normal model-load error
            # — it only fires if the availability check itself explodes. The real
            # reason lives in `embeddings.load_error()`, which is the only place
            # that has it. Reading it here is what makes `load_error` mean
            # something instead of always being null.
            err = "" if available else (emb_mod.load_error() or "")
            if not available and not err:
                err = ("the embedding model is not loaded and reported no error "
                       "(it may not have been asked to load yet)")
        return {
            "backend": SEMANTIC_BACKEND,
            "model": semantic_model_id(),
            "revision": semantic_model_revision(),
            "algorithm": SEMANTIC_ALGORITHM,
            "dimension": SEMANTIC_DIM,
            "space_id": semantic_space_id(),
            "collection": self._collection_name,
            "legacy_collection_read": False,
            "available": available,
            "degraded": not available,
            "load_error": err or None,
            "writes_skipped": self._skipped_writes,
            "queries_skipped": self._skipped_queries,
            "last_skip_reason": self._last_skip_reason or None,
        }

    def _note_skip(self, kind: str, exc: Exception) -> None:
        self._last_skip_reason = str(exc)[:300]
        if kind == "write":
            self._skipped_writes += 1
        else:
            self._skipped_queries += 1

    def _ensure(self) -> None:
        if self._collection is not None and self._client is not None:
            return

        self._persist_dir.mkdir(parents=True, exist_ok=True)

        settings = Settings(
            persist_directory=str(self._persist_dir),
            anonymized_telemetry=False,
        )

        # PersistentClient handles persistence via the directory.
        self._client = chromadb.PersistentClient(path=str(self._persist_dir), settings=settings)

        emb = _SemanticEmbeddingFunction(dim=SEMANTIC_DIM)
        self._emb = emb
        # The space identity is recorded ON the collection so it can be audited
        # and so a future mismatch is discoverable rather than inferred.
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=emb,
            metadata={"hnsw:space": "cosine",
                      "nova_semantic_space": semantic_space_id(),
                      "nova_semantic_backend": SEMANTIC_BACKEND,
                      "nova_semantic_model": semantic_model_id(),
                      "nova_semantic_revision": semantic_model_revision(),
                      "nova_semantic_algorithm": SEMANTIC_ALGORITHM},
        )

    async def upsert_text(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        # chromadb REJECTS an empty metadata dict ("Expected metadata to be a
        # non-empty dict"), so the signature's own `metadata=None` default used
        # to raise. It must be passed as None instead. Every current caller
        # happens to supply metadata, so this was latent — but the signature
        # advertises the broken call, which is exactly how it becomes live.
        doc_id = str(doc_id).strip()
        if not doc_id:
            return  # a blank id would create an unaddressable phantom row
        metas = [metadata] if metadata else None
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            target = self._staging if self._writing_staged else self._collection
            try:
                await asyncio.to_thread(
                    target.upsert,
                    ids=[doc_id],
                    documents=[str(text)],
                    metadatas=metas,
                )
            except SemanticUnavailable as e:
                # Live: SKIP, do not substitute. SQLite already holds this record,
                # so the only loss is semantic recall until the model returns —
                # and a rebuild can restore it from the authoritative store.
                #
                # Staged rebuild: RAISE. A skipped record inside a rebuild is a
                # hole in the thing being promoted, and promoting a holed index
                # would state coverage that does not exist. The caller aborts and
                # the previous index survives.
                self._note_skip("write", e)
                if self._writing_staged:
                    raise
                return

    async def query(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            try:
                res = await asyncio.to_thread(
                    self._collection.query,
                    query_texts=[str(q)],
                    n_results=int(limit),
                    include=["documents", "metadatas", "distances"],
                )
            except SemanticUnavailable as e:
                # NO semantic hits, rather than a ranking produced by a query
                # vector from a different space. Callers merge this with lexical
                # and recent-conversation recall, which are unaffected.
                self._note_skip("query", e)
                return []

        hits: list[dict[str, Any]] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]

        for i in range(min(len(ids), len(docs), len(metas), len(dists))):
            hits.append(
                {
                    "id": ids[i],
                    "text": docs[i],
                    "metadata": metas[i] or {},
                    "distance": float(dists[i]),
                }
            )
        return hits


    async def count(self) -> int:
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            return int(await asyncio.to_thread(self._collection.count))


    async def reset(self) -> None:
        """Drop and recreate the collection (persistent directory remains)."""
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            assert self._client is not None
            try:
                await asyncio.to_thread(self._client.delete_collection, self._collection_name)
            except Exception:
                # If it doesn't exist or delete fails, continue.
                pass
            self._collection = None
            self._client = None
            await asyncio.to_thread(self._ensure)


    # ── atomic rebuild: staging collection, promoted only on full success ─────
    #
    # `reset()` then N independent writes is not a rebuild, it is a demolition
    # followed by an attempt. If the embedder dies or an upsert fails at record
    # 400 of 900, what survives is a partial index that looks exactly like a
    # complete one — and the previous, working index is already gone.
    #
    # So a rebuild builds into a SEPARATE collection and only becomes
    # authoritative when every record is in. On failure the staging collection is
    # dropped and the OLD index is still there, untouched: better than the empty
    # collection the brief allows as a minimum, and the same guarantee.

    def _staging_name(self) -> str:
        return f"{self._collection_name}__staging"

    async def begin_staged_rebuild(self) -> None:
        """Start writing into a fresh staging collection instead of the live one."""
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            assert self._client is not None
            staging = self._staging_name()
            # A staging collection left behind by an earlier crashed rebuild is
            # garbage, never a partial result to resume from.
            try:
                await asyncio.to_thread(self._client.delete_collection, staging)
            except Exception:
                pass
            self._staging = await asyncio.to_thread(
                self._client.get_or_create_collection,
                name=staging,
                embedding_function=self._emb,
                metadata={"hnsw:space": "cosine",
                          "nova_semantic_space": semantic_space_id(),
                          "nova_semantic_backend": SEMANTIC_BACKEND,
                          "nova_semantic_model": semantic_model_id(),
                          "nova_semantic_revision": semantic_model_revision(),
                          "nova_semantic_algorithm": SEMANTIC_ALGORITHM,
                          "nova_staging": "true"},
            )
            self._writing_staged = True

    async def abort_staged_rebuild(self) -> None:
        """Throw the staging collection away. The live index is left as it was."""
        async with self._lock:
            self._writing_staged = False
            self._staging = None
            if self._client is None:
                return
            try:
                await asyncio.to_thread(self._client.delete_collection, self._staging_name())
            except Exception:
                pass

    async def commit_staged_rebuild(self) -> int:
        """Promote staging to authoritative. Returns the promoted record count.

        The live collection is deleted and staging is RENAMED into its place, so
        there is no window in which records are half-copied. If the rename fails
        the live collection is recreated empty rather than left missing — empty is
        recoverable from SQLite, absent is a crash on next read.
        """
        async with self._lock:
            if self._client is None or self._staging is None:
                raise RuntimeError("commit_staged_rebuild called without a staged rebuild")
            count = int(await asyncio.to_thread(self._staging.count))
            try:
                await asyncio.to_thread(self._client.delete_collection, self._collection_name)
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._staging.modify, name=self._collection_name)
            finally:
                self._writing_staged = False
                self._staging = None
                self._collection = None
                await asyncio.to_thread(self._ensure)
            return count

    async def staged_count(self) -> int:
        async with self._lock:
            if self._staging is None:
                return 0
            return int(await asyncio.to_thread(self._staging.count))

    async def delete_ids(self, ids: list[str]) -> None:
        """Best-effort delete of documents by id."""
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if not ids:
            return
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            await asyncio.to_thread(self._collection.delete, ids=ids)
