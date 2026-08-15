from __future__ import annotations

"""PCM -> speaker embedding. The only part of P5 that touches a neural model.

MODEL CHOICE, and why it was not assumed
----------------------------------------
`speechbrain/spkrec-ecapa-voxceleb` (ECAPA-TDNN), measured on this machine
before anything was built:

    params            20.8 M
    on disk           89 MB
    embedding dim     192
    revision          0f99f2d0ebe89ac095bcc5903c4dd8f72b367286

Dependency impact was checked with `pip install --dry-run` FIRST, because the
brief's hard constraint is that Nova's working Torch/CUDA/XTTS/STT stack must
not be disturbed to satisfy a speaker library. It adds five small packages
(hyperpyyaml, ruamel.yaml, ruamel.yaml.clib, sentencepiece, speechbrain) and
leaves torch 2.11.0+cu128, torchaudio and numpy 1.26.4 untouched. A candidate
that had demanded a torch downgrade would have been rejected on that alone.

DEVICE: CPU, DELIBERATELY
-------------------------
CUDA is roughly 7x faster in isolation and that is not the deciding number:

    CPU,  3s audio                 41-58 ms
    CUDA, 3s audio                  5.7 ms   (+90 MB VRAM)
    CPU,  3s audio, GPU saturated  46 ms     <- unchanged

V3 P1 measured what a third CUDA consumer costs on this machine while the 9B
model generates: whisper +185%, XTTS +209%. Speaker ID would be that third
consumer, on the same device that already aborts (D1) when pushed. Meanwhile
the CPU path is provably indifferent to GPU load, and 41 ms sits inside an /stt
request that already spends hundreds of milliseconds on ffmpeg and Whisper.

So CPU is not a fallback here, it is the choice. `NOVA_SPEAKER_DEVICE=cuda`
exists for anyone who wants to re-measure that trade on different hardware.

Everything in this module is optional. If the model cannot load, every caller
gets `None` and Nova keeps working — speaker identity is enrichment, never a
prerequisite for hearing Marcus.
"""

import os
import threading
import time
from typing import Any

import numpy as np

from core.logging_setup import get_logger

logger = get_logger(__name__)

#: The model this build compares embeddings against. Persisted with every
#: profile: vectors from a different model share no vector space, and comparing
#: them would produce confident nonsense rather than an error.
MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"
MODEL_REVISION = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
EMBEDDING_DIM = 192
TARGET_SR = 16000

#: Below this, an utterance carries too little voice to identify anybody.
#: ECAPA degrades quickly under ~1s; Nova reports `too_short` rather than
#: guessing from a syllable.
MIN_SPEECH_S = 1.0

#: Runtime command-audio quality (V3 P5.1). Enrollment already rejects silence,
#: clipping and fragments; ORDINARY identification had no equivalent gate, so a
#: long-enough stretch of near-silence would be embedded and could score against
#: a profile. An empty room must never come back `known`.
#:
#: These bars are deliberately LOWER than enrollment's. Enrollment can demand
#: 1.5s of clean speech because it happens once and can ask for a retake; a real
#: command is often "stop", "yes", "louder" — rejecting those would break normal
#: use to fix a problem that only silence causes.
CMD_MIN_RMS = 0.004          # about half the enrollment floor
CMD_MAX_CLIP_FRACTION = 0.05  # a command may peak harder than a read prompt

#: Above this we truncate. Longer audio does not improve the embedding enough
#: to justify the latency, which grows roughly linearly (85 ms at 8 s on CPU).
MAX_SPEECH_S = 10.0


def enabled() -> bool:
    return os.getenv("NOVA_SPEAKER_ID", "1").strip().lower() not in {"0", "false", "no", "off"}


def device_preference() -> str:
    return (os.getenv("NOVA_SPEAKER_DEVICE", "cpu").strip() or "cpu").lower()


class SpeakerEmbedder:
    """Lazily-loaded ECAPA encoder. Thread-safe, and never fatal."""

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = threading.Lock()
        self._load_failed = False
        self._load_error: str | None = None
        self._load_ms: float | None = None
        self._device = device_preference()
        #: True once a load has actually gone through the pinned-revision path.
        #: Reported in status() so "pinned" is an observation, not a claim.
        self._pinned = False
        self.calls = 0
        self.failures = 0
        self._latencies: list[float] = []

    # -- lifecycle ------------------------------------------------------------

    @property
    def available(self) -> bool:
        return enabled() and not self._load_failed

    def _ensure_model(self) -> Any:
        """Load once. Holds a lock because /stt requests are concurrent and two
        simultaneous first-requests must not both pull the model."""
        if self._model is not None or self._load_failed:
            return self._model
        with self._lock:
            if self._model is not None or self._load_failed:
                return self._model
            t0 = time.perf_counter()
            try:
                # Norton intercepts TLS on this machine, so anything reaching
                # HuggingFace needs the system trust store (see the XTTS
                # download path, which learned the same thing).
                try:
                    import truststore  # type: ignore
                    truststore.inject_into_ssl()
                except Exception:  # noqa: BLE001
                    pass

                from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
                from speechbrain.utils.fetching import FetchConfig  # type: ignore

                savedir = os.getenv("NOVA_SPEAKER_MODEL_DIR", "").strip() or None
                if not savedir:
                    from pathlib import Path
                    savedir = str(Path(os.getenv("NOVA_REPO_ROOT", ".")) / "model" / "speaker" / "ecapa")
                # PIN THE REVISION FOR REAL (V3 P5.1).
                #
                # P5 part 1 persisted MODEL_REVISION into every profile and used
                # it to decide compatibility — while loading whatever HEAD of the
                # repo happened to be. So the metadata was an assertion nobody
                # checked: upstream could have republished the weights and Nova
                # would have kept comparing new embeddings against old centroids,
                # confidently, with a revision string that was simply untrue.
                #
                # `fetch_config` is SpeechBrain's supported mechanism. Every file
                # in the model now comes from this exact commit.
                self._model = EncoderClassifier.from_hparams(
                    source=MODEL_ID, savedir=savedir,
                    run_opts={"device": self._device},
                    fetch_config=FetchConfig(revision=MODEL_REVISION),
                )
                self._pinned = True
                self._load_ms = (time.perf_counter() - t0) * 1000
                logger.info("speaker_model_loaded", model=MODEL_ID, device=self._device,
                            ms=round(self._load_ms))
            except Exception as e:  # noqa: BLE001
                self._load_failed = True
                self._load_error = f"{type(e).__name__}: {str(e)[:200]}"
                logger.warning("speaker_model_unavailable", error=self._load_error)
        return self._model

    def warm(self) -> bool:
        """Optional pre-load so the first real command does not pay for it."""
        return self._ensure_model() is not None

    # -- embedding ------------------------------------------------------------

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """Mono float32 PCM -> L2-normalised 192-d embedding, or None.

        Returns None rather than raising for EVERY failure mode. A caller that
        has to wrap this in try/except would eventually forget, and a speaker
        model is not permitted to break transcription.
        """
        if not self.available:
            return None
        model = self._ensure_model()
        if model is None:
            return None
        try:
            x = _prepare(audio, sample_rate)
            if x is None:
                return None
            import torch  # type: ignore

            wav = torch.from_numpy(x).unsqueeze(0)
            if self._device != "cpu":
                wav = wav.to(self._device)
            t0 = time.perf_counter()
            with torch.no_grad():
                emb = model.encode_batch(wav)
            vec = emb.squeeze().detach().cpu().numpy().astype(np.float32)
            self.calls += 1
            self._latencies.append((time.perf_counter() - t0) * 1000)
            del self._latencies[:-200]
            norm = float(np.linalg.norm(vec))
            if not np.isfinite(norm) or norm < 1e-6:
                return None
            # Normalising here means cosine similarity downstream is a dot
            # product, and every stored centroid is on the unit sphere.
            return (vec / norm).astype(np.float32)
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            logger.warning("speaker_embed_failed", error=str(e)[:200])
            return None

    # -- diagnostics ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        lat = sorted(self._latencies)
        return {
            "enabled": enabled(),
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION[:12],
            "embedding_dim": EMBEDDING_DIM,
            "device": self._device,
            "loaded": self._model is not None,
            "revision_pinned": self._pinned,
            "load_failed": self._load_failed,
            "load_error": self._load_error,
            "load_ms": round(self._load_ms) if self._load_ms else None,
            "embed_calls": self.calls,
            "embed_failures": self.failures,
            "embed_ms_median": round(lat[len(lat) // 2], 1) if lat else None,
            "embed_ms_last": round(self._latencies[-1], 1) if self._latencies else None,
        }


def command_quality(audio: np.ndarray, sample_rate: int) -> tuple[bool, str]:
    """Is this command audio worth identifying a speaker from? (V3 P5.1)

    Separate from enrollment's `check_sample`, and deliberately more permissive:
    a command is often one word, and rejecting "stop" to guard against silence
    would break normal use to fix a problem only silence causes.

    What it does catch is the case P5 part 1 missed entirely — a long-enough
    stretch of near-silence or a malformed buffer being embedded and scored
    against a profile. An empty room must never come back `known`.
    """
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size == 0:
        return False, "empty audio"
    if not np.isfinite(x).all():
        return False, "malformed audio (non-finite samples)"
    dur = x.size / max(int(sample_rate), 1)
    if dur < MIN_SPEECH_S:
        return False, f"too short ({dur:.2f}s)"
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms < CMD_MIN_RMS:
        return False, f"near-silence (rms {rms:.5f})"
    if float(np.mean(np.abs(x) >= 0.999)) > CMD_MAX_CLIP_FRACTION:
        return False, "heavily clipped"
    return True, "ok"


def _prepare(audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
    """Mono, 16 kHz, bounded length. Returns None if there is nothing to embed."""
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size == 0 or not np.isfinite(x).all():
        return None
    if sample_rate != TARGET_SR and sample_rate > 0:
        # Linear resample. The decoded /stt PCM is already 16 kHz — this exists
        # for enrollment uploads that arrive at another rate, and a better
        # resampler would be measurable only if that path were hot.
        n = int(round(x.size * TARGET_SR / sample_rate))
        if n <= 0:
            return None
        x = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)
    if x.size < int(MIN_SPEECH_S * TARGET_SR):
        return None
    limit = int(MAX_SPEECH_S * TARGET_SR)
    if x.size > limit:
        x = x[:limit]
    return x


#: One embedder per process. The model is ~220 MB of RSS; loading it per request
#: would be absurd and loading it per conversation only slightly less so.
EMBEDDER = SpeakerEmbedder()
