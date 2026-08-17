from __future__ import annotations

"""Real semantic embeddings for Nova's memory index.

Uses a small sentence-embedding model (default: BAAI/bge-small-en-v1.5,
~130 MB, 384 dims) loaded once on GPU via transformers. Falls back cleanly:
if the model can't load (offline, OOM), callers should degrade to the hash
embedding so memory keeps working — semantic quality degrades, nothing breaks.
"""

import os
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

#: Loose labels a caller might set that do NOT identify a fixed vector space.
_UNPINNED_REVISIONS = {"", "main", "master", "latest", "head", "none"}

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


def embedding_revision() -> str:
    """The pinned model repository commit. THE authoritative revision value.

    An override that is not a fixed commit is refused rather than honoured: a
    caller setting NOVA_EMBED_REVISION=main would silently re-open exactly the
    hole this closes. Overriding to a real commit is fine and yields its own
    collection, because `semantic_space_id()` includes this.
    """
    raw = (os.getenv("NOVA_EMBED_REVISION", "") or "").strip()
    if raw and raw.lower() not in _UNPINNED_REVISIONS:
        return raw
    if raw:
        logger.warning("embedding_revision_override_ignored", requested=raw,
                       using=_DEFAULT_REVISION,
                       reason="a moving ref is not a vector-space identity")
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
