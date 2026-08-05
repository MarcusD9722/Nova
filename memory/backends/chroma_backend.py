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


class _SemanticEmbeddingFunction:
    """Real sentence embeddings (bge-small on GPU) with hash fallback.

    Falls back to the hash embedder per-call if the model is unavailable so
    memory keeps working offline — with degraded semantic quality, not errors.
    Both produce 384-dim vectors, so they share a collection safely enough
    for graceful degradation.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = int(dim)
        self._fallback = _HashEmbeddingFunction(dim=dim)

    def name(self) -> str:
        return "nova-semantic-v1"

    def get_config(self) -> dict[str, int]:
        return {"dim": self._dim}

    def _encode(self, texts: list[str]) -> list[list[float]]:
        from memory import embeddings as emb_mod

        if emb_mod.embedding_available():
            return emb_mod.embed_texts(texts)
        return self._fallback.embed_documents(texts)

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

    # v2: semantic embeddings replaced the hash embedder. A new collection
    # name makes the unifier's startup check see an empty index and rebuild
    # it from SQLite with the new vectors.
    def __init__(self, persist_dir: Path, collection_name: str = "nova_memory_v2") -> None:
        self._persist_dir = Path(persist_dir)
        self._collection_name = collection_name
        self._lock = asyncio.Lock()
        self._client: chromadb.ClientAPI | None = None
        self._collection: Any | None = None

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

        emb = _SemanticEmbeddingFunction(dim=384)
        self._emb = emb
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=emb,
            metadata={"hnsw:space": "cosine"},
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
            await asyncio.to_thread(
                self._collection.upsert,
                ids=[doc_id],
                documents=[str(text)],
                metadatas=metas,
            )

    async def query(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            res = await asyncio.to_thread(
                self._collection.query,
                query_texts=[str(q)],
                n_results=int(limit),
                include=["documents", "metadatas", "distances"],
            )

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


    async def delete_ids(self, ids: list[str]) -> None:
        """Best-effort delete of documents by id."""
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if not ids:
            return
        async with self._lock:
            await asyncio.to_thread(self._ensure)
            await asyncio.to_thread(self._collection.delete, ids=ids)
