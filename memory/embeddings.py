from __future__ import annotations

"""Real semantic embeddings for Nova's memory index.

Uses a small sentence-embedding model (default: BAAI/bge-small-en-v1.5,
~130 MB, 384 dims) at a PINNED repository commit, loaded once via transformers.

There is NO fallback embedder. This docstring used to say the opposite — that
callers "should degrade to the hash embedding so memory keeps working" — and that
instruction was a corruption: for the same text, cosine(BGE(t), HASH(t)) = -0.0162,
so the two are orthogonal spaces that merely share a dimension. Mixing them into
one persistent collection silently destroyed recall (see D18).

The contract is:

    the model is unavailable
      -> semantic reads and writes are SKIPPED (and counted)
      -> SQLite, lexical and recent-conversation recall continue unaffected
      -> NEVER substitute a different vector space

The model id AND its revision are part of the collection identity, so a repository
that changes its weights under the same id gets its own collection instead of
poisoning this one.
"""

import os
import re
import threading
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

#: The EXACT model repository commit Nova embeds with. This is the revision
#: already cached on this machine (verified in
#: ~/.cache/huggingface/hub/models--BAAI--bge-small-en-v1.5/refs/main), so pinning
#: it changes no weights and triggers no download.
#:
#: A repository can change its weights while keeping the same model id. Same id,
#: same 384 dimensions, same pooling — different vectors. Without the revision in
#: the identity, a silent upstream reupload would reuse the same persistent Chroma
#: collection and mix two vector spaces, which is the exact class of corruption
#: this pin exists to prevent. "main" and "latest" are not identities; a commit is.
_DEFAULT_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"

#: A revision is a full 40-character hex commit sha, or it is not a revision.
#:
#: The first version of this check blacklisted a handful of names
#: (main/master/latest/head/none) and accepted everything else, which is the wrong
#: shape: `dev`, `release/2026`, `refs/heads/main` and `abc1234` all sailed
#: through, and every one of them can move. A moving ref that keeps its STRING
#: while its weights change is precisely the hole the revision exists to close, so
#: the rule is an allow-list on form, not a deny-list on names.
_SHA40_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")

_lock = threading.Lock()
_model: Any | None = None
_tokenizer: Any | None = None
_device: str | None = None
_load_failed: str | None = None


def _load() -> None:
    """Load tokenizer+model once. Raises on failure (caller records it)."""
    global _model, _tokenizer, _device

    # Norton AV re-signs HTTPS on this machine; use the Windows cert store for
    # the one-time model download (idempotent if the backend already injected).
    try:
        import truststore  # type: ignore

        truststore.inject_into_ssl()
    except Exception:
        pass

    import torch  # type: ignore
    from transformers import AutoModel, AutoTokenizer  # type: ignore

    model_id = os.getenv("NOVA_EMBED_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    # Defaults to CPU, deliberately. This is a 33M-parameter model whose work
    # happens in the background, so the GPU buys almost nothing — but it is a
    # THIRD independent CUDA consumer in a process that already runs llama.cpp
    # and XTTS on the one device, and uncoordinated torch allocations there
    # abort llama.cpp with an illegal memory access (see core/gpu.py).
    # Unlike XTTS, this path is synchronous and cannot take the async GPU
    # semaphore, so keeping it off the device is the honest fix.
    # NOVA_EMBED_DEVICE=cuda puts it back — reasonable once a second GPU exists.
    requested = (os.getenv("NOVA_EMBED_DEVICE", "cpu").strip() or "cpu").lower()
    if requested == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = requested

    # ONE revision value, passed to BOTH loads. Loading the tokenizer from one
    # commit and the weights from another would be a third vector space nobody
    # named.
    revision = embedding_revision()
    tokenizer = AutoTokenizer.from_pretrained(model_id, revision=revision)
    model = AutoModel.from_pretrained(
        model_id,
        revision=revision,
        torch_dtype=torch.float16 if device.startswith("cuda") else torch.float32,
    )
    model = model.to(device).eval()

    _tokenizer = tokenizer
    _model = model
    _device = device
    logger.info("embedding_model_loaded", model=model_id, revision=revision, device=device)


def embedding_model_id() -> str:
    return os.getenv("NOVA_EMBED_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def revision_is_valid(value: str | None) -> bool:
    """True only for a full 40-hex commit sha.

    Not a branch, not a tag, not a short sha, not `refs/heads/anything`. Anything
    that can point at different weights tomorrow while spelling the same today is
    invalid by construction.
    """
    return bool(_SHA40_RE.match((value or "").strip()))


def embedding_revision() -> str:
    """The pinned model repository commit. THE authoritative revision value.

    Always a lowercase 40-hex sha. An override is honoured only if it IS one:
    `NOVA_EMBED_REVISION=main` (or `dev`, or `release/2026`, or a short sha) is
    refused with a warning and the pinned default is used, because accepting it
    would let weights change underneath an unchanged `semantic_space_id()`.

    Overriding to a real, different commit is fine and yields its own collection,
    since the space identity includes this value.
    """
    raw = (os.getenv("NOVA_EMBED_REVISION", "") or "").strip()
    if not raw:
        return _DEFAULT_REVISION
    if revision_is_valid(raw):
        # Canonical lowercase: the same commit spelled in two cases must not
        # produce two collection names for one vector space.
        return raw.lower()
    logger.warning("embedding_revision_override_refused", requested=raw[:80],
                   using=_DEFAULT_REVISION,
                   reason="not a 40-character hex commit sha; a value that can "
                          "move is not a vector-space identity")
    return _DEFAULT_REVISION


def embedding_available() -> bool:
    """True once the model is loaded (or loadable). Never raises."""
    global _load_failed
    if _model is not None:
        return True
    if _load_failed is not None:
        return False
    with _lock:
        if _model is not None:
            return True
        if _load_failed is not None:
            return False
        try:
            _load()
            return True
        except Exception as e:  # noqa: BLE001
            _load_failed = str(e)[:300]
            logger.warning("embedding_model_unavailable", error=_load_failed)
            return False


def embedding_loaded() -> bool:
    """True only if the model is ALREADY in memory. Never triggers a load.

    `embedding_available()` loads on first call, which takes seconds. Callers on
    a latency-critical path (the tool selector runs before every turn's first
    token) need to ask "can I have embeddings *right now*" without paying for
    the load, and degrade gracefully when the answer is no.
    """
    return _model is not None


def warm_in_background() -> None:
    """Kick off the model load off the hot path. Safe to call repeatedly."""
    if _model is not None or _load_failed is not None:
        return

    def _warm() -> None:
        try:
            embedding_available()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_warm, name="nova-embed-warm", daemon=True).start()


def load_error() -> str | None:
    return _load_failed


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Encode texts to normalized 384-dim vectors (CLS pooling, bge-style).

    Raises if the model is unavailable — call embedding_available() first.
    """
    if not embedding_available():
        # `_load_failed` is None when the model simply is not loaded rather than
        # having failed to load, and "unavailable: None" reads like a bug in the
        # error path instead of a state.
        raise RuntimeError("embedding model unavailable: "
                           + (_load_failed or "not loaded"))

    import torch  # type: ignore

    cleaned = [(t or "").strip()[:2000] or " " for t in texts]
    out: list[list[float]] = []
    batch_size = 32

    with torch.inference_mode():
        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i : i + batch_size]
            enc = _tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt").to(_device)
            hidden = _model(**enc).last_hidden_state
            # bge models: CLS token pooling + L2 normalize.
            cls = hidden[:, 0]
            cls = torch.nn.functional.normalize(cls, p=2, dim=1)
            out.extend(cls.float().cpu().tolist())

    return out
