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


class SemanticRecoveryError(SemanticUnavailable):
    """An interrupted promotion could not be resolved into a safe authority.

    A SUBCLASS of SemanticUnavailable on purpose: every caller already treats that
    as "skip semantic work, SQLite is unaffected", which is exactly the right
    response. Semantic memory being unavailable is acceptable; destroying the last
    known-good copy to make it available is not.
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

    Uses a real embedding model (see memory/embeddings.py) and NO fallback
    embedder. This docstring used to promise "a deterministic hash fallback so
    query()/upsert() never hard-fail"; that fallback put two orthogonal vector
    spaces in one collection and is gone (D18).

    Failing softly is still the behaviour, but by SKIPPING rather than
    substituting: an unavailable model, or a promotion that cannot be resolved
    into a safe authority, makes writes skip and queries return no hits, while
    SQLite and lexical recall carry on.
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
        #: Observability for the repair paths, so a test (and /status) can tell
        #: "it worked" from "it silently did nothing".
        self._recovered_from_backup = 0
        self._rolled_back = 0
        self._finalized_after_crash = 0
        self._finalization_pending = 0
        #: Non-empty when recovery REFUSED to produce an authority. The store is
        #: intact and untouched; semantic work is skipped until it is resolved.
        self._recovery_error = ""

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
            # Promotion repairs. Non-zero means a rebuild was interrupted and the
            # old index was put back — worth surfacing rather than hiding, since it
            # is the difference between "nothing happened" and "we recovered".
            "promotions_rolled_back": self._rolled_back,
            "recovered_from_backup": self._recovered_from_backup,
            "finalized_after_crash": self._finalized_after_crash,
            "finalization_pending": self._finalization_pending,
            "recovery_error": self._recovery_error or None,
        }

    def _note_skip(self, kind: str, exc: Exception) -> None:
        self._last_skip_reason = str(exc)[:300]
        if kind == "write":
            self._skipped_writes += 1
        else:
            self._skipped_queries += 1

    def _open_client(self) -> None:
        """Open the Chroma client WITHOUT touching any collection.

        Separate from `_ensure` because recovery must inspect the store before
        anything is created: `get_or_create_collection` is precisely the call that
        can manufacture an empty live index next to a complete backup.
        """
        if self._client is not None:
            return
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            persist_directory=str(self._persist_dir),
            anonymized_telemetry=False,
        )
        self._client = chromadb.PersistentClient(path=str(self._persist_dir),
                                                 settings=settings)
        if getattr(self, "_emb", None) is None:
            self._emb = _SemanticEmbeddingFunction(dim=SEMANTIC_DIM)

    def _ensure(self) -> None:
        if self._collection is not None and self._client is not None:
            return

        self._open_client()
        emb = self._emb

        # Recovery runs BEFORE the live collection is opened, and it may REFUSE to
        # produce an authority. When it refuses, `get_or_create_collection` must
        # not run: creating an empty live index beside a good backup is how a
        # transient failure turned into permanent loss, because the next boot saw
        # live+backup and deleted the backup.
        self._recover_interrupted_promotion()

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
            try:
                # Inside the guard: `_ensure` refuses to open an index it cannot
                # prove is safe, and that refusal must degrade exactly like an
                # unavailable embedder rather than crashing the caller.
                await asyncio.to_thread(self._ensure)
                target = self._staging if self._writing_staged else self._collection
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
            try:
                await asyncio.to_thread(self._ensure)
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

    def _backup_name(self) -> str:
        return f"{self._collection_name}__backup"

    def _existing_names(self) -> set[str]:
        assert self._client is not None
        return {c.name for c in self._client.list_collections()}

    # ── durable promotion authority ───────────────────────────────────────────
    #
    # Collection NAMES cannot answer "is this live generation trustworthy?".
    # `live + backup` is ambiguous: it means either (A) the rename to live
    # succeeded and the crash beat verification, or (B) verification FAILED and
    # the rollback could not delete the bad live. Those want opposite actions, and
    # the previous code assumed (A) and deleted the backup — which in case (B)
    # destroys the known-good generation in order to keep the rejected one.
    #
    # So promotion writes a tiny journal next to the store BEFORE it moves
    # anything, and recovery decides from that plus the collection itself. One
    # small JSON file, replaced atomically; a local store needs nothing larger.

    def _journal_path(self) -> Path:
        """Scoped to THIS collection, not to the persistence directory.

        One directory can hold several semantic collections at once: a different
        pinned revision, the orphaned revision-less collection, `nova_memory_v2`.
        A single global `nova_promotion.json` meant a backend for revision B would
        read — and DELETE — the recovery evidence belonging to revision A, so
        switching back to A left A with live+backup and no journal, which A must
        then refuse. No vectors were lost, but the durable proof was, and proof is
        the whole mechanism.

        `_collection_name` is already `nova_sem_bge_<hex>`, i.e. filename-safe.
        """
        return self._persist_dir / f"nova_promotion_{self._collection_name}.json"

    def _journal_is_authoritative(self, journal: Any) -> tuple[bool, str]:
        """Does this journal actually describe THIS collection mid-promotion?

        Every field is bound to the current identity. A journal that does not
        match is not evidence about this collection, and a malformed one is not
        evidence at all — in both cases the caller must fail closed rather than
        infer.
        """
        if not isinstance(journal, dict):
            return False, "journal is not an object"
        if journal.get("state") != "promoting":
            return False, f"state is {journal.get('state')!r}, not 'promoting'"
        if journal.get("collection") != self._collection_name:
            return False, (f"journal names collection {journal.get('collection')!r}, "
                           f"not {self._collection_name!r}")
        if journal.get("backup") != self._backup_name():
            return False, (f"journal names backup {journal.get('backup')!r}, "
                           f"not {self._backup_name()!r}")
        if journal.get("space_id") != semantic_space_id():
            return False, "journal describes a different vector space"
        count = journal.get("expected_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False, f"expected_count {count!r} is not a non-negative integer"
        if not str(journal.get("generation") or "").strip():
            return False, "generation is empty"
        return True, "journal matches this collection"

    def _write_journal(self, entry: dict[str, Any] | None) -> None:
        """Atomically replace (or remove) the promotion journal."""
        import json
        import os

        path = self._journal_path()
        if entry is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)   # atomic on Windows and POSIX

    def _read_journal(self) -> dict[str, Any] | None:
        import json

        try:
            raw = self._journal_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            return None
        try:
            entry = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        return entry if isinstance(entry, dict) else None

    def _collection_is_valid(self, name: str, journal: dict[str, Any]) -> tuple[bool, str]:
        """Does `name` hold the generation the journal describes?

        Checks what can be checked durably: it opens, it holds the expected number
        of records, and it carries the expected vector space.
        """
        assert self._client is not None
        try:
            col = self._client.get_collection(name, embedding_function=self._emb)
        except Exception as e:  # noqa: BLE001
            return False, f"cannot open {name}: {str(e)[:120]}"
        try:
            got = int(col.count())
        except Exception as e:  # noqa: BLE001
            return False, f"cannot count {name}: {str(e)[:120]}"
        want = journal.get("expected_count")
        if want is not None and got != int(want):
            return False, f"{name} holds {got} records, promotion expected {want}"
        space = (col.metadata or {}).get("nova_semantic_space")
        want_space = journal.get("space_id")
        if want_space:
            # MISSING provenance fails, exactly like WRONG provenance. The earlier
            # `and space and` made an absent `nova_semantic_space` pass, so a
            # collection that could not prove which vector space it held was
            # treated as proving the right one. Absence of evidence was being read
            # as evidence.
            if not space:
                return False, (f"{name} carries no vector-space provenance, so it "
                               f"cannot be verified against the promotion")
            if space != want_space:
                return False, f"{name} carries a different vector space ({space})"
        return True, f"{name} verified: {got} records"

    def _finalize_promotion(self, backup: str) -> bool:
        """Drop the stale backup, and clear the journal ONLY once it is gone.

        A cleanup failure must not become `live + backup + no journal`: the next
        startup would then have a provably-good live it can no longer prove, and
        would have to refuse it. So a failed delete keeps ALL THREE — the verified
        live stays authoritative and usable, the backup stays, and the journal
        stays so the next open can verify again and retry the cleanup.
        """
        assert self._client is not None
        try:
            self._client.delete_collection(backup)
        except Exception:  # noqa: BLE001
            self._finalization_pending += 1
            return False
        try:
            if backup in self._existing_names():
                self._finalization_pending += 1
                return False
        except Exception:  # noqa: BLE001
            self._finalization_pending += 1
            return False
        self._write_journal(None)
        return True

    def _restore_backup(self, live: str, backup: str) -> tuple[bool, str]:
        assert self._client is not None
        try:
            col = self._client.get_collection(backup, embedding_function=self._emb)
            col.modify(name=live)
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:160]
        self._recovered_from_backup += 1
        return True, ""

    def _recover_interrupted_promotion(self) -> None:
        """Establish a SAFE authority, or refuse to open the index at all.

        Decided from the journal and the collection contents, never from names:

          no backup                    -> steady state or cold start.
          live absent, backup present  -> restore the backup. If the restore
                                          FAILS, refuse: keep the backup, create
                                          nothing, raise. The old code swallowed
                                          this and let `_ensure` manufacture an
                                          empty live beside the good backup, which
                                          the next boot then deleted — a transient
                                          failure becoming permanent loss.
          live AND backup present      -> ambiguous. Verify live against the
                                          journal. Valid -> finalize and drop the
                                          backup. Invalid -> roll back to the
                                          backup. Cannot decide, or cannot roll
                                          back -> refuse, keeping the backup.

        Refusing raises SemanticRecoveryError, which every caller already handles
        as "skip semantic work": SQLite, lexical and recent recall are unaffected.
        Semantic memory being unavailable is acceptable; destroying the last
        known-good copy to make it available is not.
        """
        assert self._client is not None
        live, backup = self._collection_name, self._backup_name()
        try:
            names = self._existing_names()
        except Exception as e:  # noqa: BLE001
            self._recovery_error = f"cannot list collections: {str(e)[:140]}"
            raise SemanticRecoveryError(self._recovery_error) from None

        journal = self._read_journal()

        if backup not in names:
            # No backup: nothing to protect. A journal here is residue from a
            # crash before anything moved, or from a finished promotion.
            if journal is not None:
                self._write_journal(None)
            self._recovery_error = ""
            return

        if live not in names:
            ok, err = self._restore_backup(live, backup)
            if not ok:
                self._recovery_error = (
                    f"the previous semantic index is present as {backup} but could "
                    f"not be restored ({err}). It has been LEFT INTACT and no empty "
                    f"index was created; semantic memory is unavailable until this "
                    f"is resolved. SQLite memory is unaffected.")
                raise SemanticRecoveryError(self._recovery_error)
            self._write_journal(None)
            self._recovery_error = ""
            return

        # live AND backup: ambiguous by name. Decide from durable state.
        usable, why = self._journal_is_authoritative(journal)
        if not usable:
            # Nothing durable says this live is a completed generation for THIS
            # collection, so it cannot be trusted over a backup that was
            # authoritative by construction. Refuse rather than guess either way.
            self._recovery_error = (
                f"both {live} and {backup} exist but no promotion journal proves "
                f"{live} is a completed, verified generation ({why}). BOTH are "
                f"preserved and semantic memory is unavailable until this is "
                f"resolved. SQLite memory is unaffected.")
            raise SemanticRecoveryError(self._recovery_error)

        valid, detail = self._collection_is_valid(live, journal)
        if valid:
            # The live generation is PROVEN. Cleaning up the stale backup is the
            # only work left, and failing at it must not cost us that proof: keep
            # the journal so the next open can verify again and retry.
            self._finalize_promotion(backup)
            self._finalized_after_crash += 1
            self._recovery_error = ""
            return

        # The promoted generation is not what the journal promised: roll back.
        try:
            self._client.delete_collection(live)
        except Exception as e:  # noqa: BLE001
            self._recovery_error = (
                f"the promoted generation failed verification ({detail}) and the "
                f"rejected {live} could not be removed ({str(e)[:120]}). The "
                f"known-good {backup} is PRESERVED and untouched; semantic memory "
                f"is unavailable until this is resolved. SQLite memory is "
                f"unaffected.")
            raise SemanticRecoveryError(self._recovery_error)

        ok, err = self._restore_backup(live, backup)
        if not ok:
            self._recovery_error = (
                f"the promoted generation failed verification ({detail}) and the "
                f"backup could not be restored ({err}). {backup} is PRESERVED; "
                f"semantic memory is unavailable until this is resolved.")
            raise SemanticRecoveryError(self._recovery_error)
        self._write_journal(None)
        self._rolled_back += 1
        self._recovery_error = ""

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

    async def commit_staged_rebuild(self, expected: int | None = None,
                                    _fail_at: str = "") -> int:
        """Promote staging to authoritative, with rollback. Returns the count.

        The first version of this DELETED the live collection and then renamed
        staging into its place. If the rename failed in between, the previous
        authoritative index was already destroyed, `_ensure()` manufactured an
        empty one, and the rebuild still reported "previous index kept" — false in
        exactly the case where truth mattered most. Worse, the caller's
        `abort_staged_rebuild()` would then delete the only complete copy.

        The old index is now PRESERVED, never deleted, until the new one is in
        place and verified:

            live    -> backup          (nothing destroyed)
            staging -> live
            verify: reopen live, count matches `expected`
            delete backup             (only now)

        Any failure after the first step restores backup -> live. The verification
        reopens the collection through a fresh handle rather than trusting the
        rename's return, because "the rename didn't raise" is not the same claim as
        "the data is readable under the new name".

        `_fail_at` is a test seam: "after_backup" and "after_rename" inject a
        failure exactly at the two boundaries that matter.
        """
        async with self._lock:
            if self._client is None or self._staging is None:
                raise RuntimeError("commit_staged_rebuild called without a staged rebuild")

            live, backup = self._collection_name, self._backup_name()
            count = int(await asyncio.to_thread(self._staging.count))
            if expected is not None and count != int(expected):
                raise RuntimeError(
                    f"staging holds {count} records but {expected} were written; "
                    f"refusing to promote")

            names = await asyncio.to_thread(self._existing_names)
            # A backup here is residue; recovery at open time already resolved the
            # store into a safe authority, and keeping it would block this swap.
            if backup in names:
                try:
                    await asyncio.to_thread(self._client.delete_collection, backup)
                except Exception:  # noqa: BLE001
                    pass
                names.discard(backup)

            had_live = live in names
            backed_up = False

            # The journal goes down BEFORE anything moves, so a crash at any point
            # afterwards leaves durable evidence of what the promoted generation
            # was supposed to contain. Without it, `live + backup` on the next boot
            # is unreadable: it looks identical whether the rename beat the crash
            # or whether verification rejected the new generation.
            import uuid as _uuid
            await asyncio.to_thread(self._write_journal, {
                "state": "promoting",
                "generation": _uuid.uuid4().hex,
                "collection": live,
                "backup": backup,
                "expected_count": count,
                "space_id": semantic_space_id(),
            })
            try:
                if had_live:
                    old = await asyncio.to_thread(
                        self._client.get_collection, live, embedding_function=self._emb)
                    await asyncio.to_thread(old.modify, name=backup)
                    backed_up = True

                if _fail_at == "after_backup":
                    raise RuntimeError("injected failure after the old index was "
                                       "preserved, before staging became live")

                await asyncio.to_thread(self._staging.modify, name=live)

                if _fail_at == "after_rename":
                    raise RuntimeError("injected failure after staging became live, "
                                       "before verification")

                # Verify through a NEW handle, then drop the backup.
                self._collection = None
                probe = await asyncio.to_thread(
                    self._client.get_collection, live, embedding_function=self._emb)
                got = int(await asyncio.to_thread(probe.count))
                if got != count:
                    raise RuntimeError(
                        f"promoted collection reopened with {got} records, "
                        f"expected {count}")

                if backed_up:
                    # Deletes the backup and clears the journal ONLY once it is
                    # confirmed gone. If cleanup fails, live/backup/journal all
                    # stay: the new generation is already proven and stays
                    # authoritative, and the next open re-verifies and retries.
                    # Clearing the journal here on failure would leave a
                    # provably-good live that can no longer be proven.
                    await asyncio.to_thread(self._finalize_promotion, backup)
                else:
                    # Nothing to clean up, so the journal has no more work.
                    await asyncio.to_thread(self._write_journal, None)
                self._writing_staged = False
                self._staging = None
                self._collection = None
                await asyncio.to_thread(self._ensure)
                return count
            except Exception:
                await asyncio.to_thread(self._rollback_promotion, backed_up)
                self._writing_staged = False
                self._staging = None
                self._collection = None
                raise

    def _rollback_promotion(self, backed_up: bool) -> None:
        """Undo a failed promotion: whatever happens, the OLD index comes back."""
        assert self._client is not None
        live, backup = self._collection_name, self._backup_name()
        try:
            names = self._existing_names()
        except Exception:  # noqa: BLE001
            return
        if not backed_up or backup not in names:
            return
        # If the staging rename already claimed `live`, that half-promoted
        # collection is the thing to discard — the backup is authoritative.
        if live in names:
            try:
                self._client.delete_collection(live)
            except Exception:  # noqa: BLE001
                return   # never leave the backup as the only copy AND delete it
        ok, _err = self._restore_backup(live, backup)
        if ok:
            self._rolled_back += 1
            self._write_journal(None)
        # If it did NOT restore, the journal deliberately stays: the backup is the
        # only good copy and open-time recovery must know that this live (if any)
        # was never verified.

    async def authoritative_state(self) -> dict[str, Any]:
        """What is ACTUALLY there right now — for reporting, not for asserting.

        A caller that has just failed a rebuild needs to tell Marcus whether his
        index survived. It must not answer that from an assumption; this reads the
        store.
        """
        async with self._lock:
            # `_open_client` only: asking `_ensure` for an authority would raise
            # in exactly the situation this method exists to describe.
            await asyncio.to_thread(self._open_client)
            assert self._client is not None
            try:
                names = await asyncio.to_thread(self._existing_names)
            except Exception as e:  # noqa: BLE001
                return {"live_present": False, "live_count": 0, "backup_present": False,
                        "summary": f"could not inspect the semantic store: {str(e)[:120]}"}
            live_present = self._collection_name in names
            backup_present = self._backup_name() in names
            count = 0
            if live_present:
                try:
                    col = await asyncio.to_thread(
                        self._client.get_collection, self._collection_name,
                        embedding_function=self._emb)
                    count = int(await asyncio.to_thread(col.count))
                except Exception:  # noqa: BLE001
                    count = -1
            if live_present and not backup_present:
                summary = f"previous index kept, {count} records"
            elif live_present and backup_present:
                summary = (f"an interrupted promotion is unresolved: {count} records "
                           f"under the live name and a BACKUP preserved beside it")
            elif backup_present:
                summary = ("the previous index is present as a BACKUP, preserved "
                           "intact, and is restored on the next open")
            else:
                summary = "no semantic index is present; a rebuild from SQLite is needed"
            return {"live_present": live_present, "live_count": count,
                    "backup_present": backup_present, "summary": summary}

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
