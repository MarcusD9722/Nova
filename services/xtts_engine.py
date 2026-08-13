from __future__ import annotations

"""The XTTS engine, with no backend attached.

This is the model-side half of Nova's voice: download guard, torch/torchaudio
compatibility shims, model load, and synthesis to WAV bytes. It deliberately
imports nothing from `backend.` or `core.` — it runs inside the isolated TTS
child process (see services/tts_worker.py), where importing the FastAPI app
would boot a second backend.

Device policy lives in `resolve_device()` and is the single source of truth.
The previous arrangement had `core/settings.py` documenting a CPU default while
`backend/app.py` raised "XTTS requires CUDA GPU execution and refuses CPU
fallback" on exactly that branch, which made Nova mute in its default
configuration. There is now one function that decides, and it never claims a
device it did not get.
"""

import io
import os
import shutil
import wave
from pathlib import Path
from typing import Any, Callable

MODEL_ID = "tts_models/multilingual/multi-dataset/xtts_v2"

#: Files Nova pulls straight from HuggingFace. Coqui's own downloader routes
#: through coqui.gateway.scarf.sh, whose TLS chain broke when Coqui shut down.
_HF_FILES = ("config.json", "vocab.json", "hash.md5", "speakers_xtts.pth", "model.pth")

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


class TtsDeviceError(RuntimeError):
    """The requested device policy cannot be satisfied. Never raised for a
    configuration that asked for something achievable."""


def resolve_device(
    requested: str | None = None,
    *,
    allow_cpu_fallback: bool | None = None,
    cuda_available: bool | None = None,
) -> tuple[str, str]:
    """Decide the device XTTS will actually run on.

    Returns ``(device, reason)``. Raises :class:`TtsDeviceError` only when the
    configuration asks for something this machine cannot provide.

    The contract, stated once so the code and the docs cannot drift apart:

    ``auto`` (default)
        Use CUDA when it is available. This is safe now *because* XTTS runs in
        its own process with its own CUDA context — the illegal-memory-access
        crash documented in core/gpu.py came from sharing a process with
        llama.cpp, not from sharing the card. When CUDA is unavailable, fall
        back to CPU only if ``NOVA_TTS_ALLOW_CPU_FALLBACK`` is set; otherwise
        fail loudly, because silently degrading a GPU-only assistant to a
        4x-slower voice is exactly the kind of quiet lie Nova is not allowed
        to tell.

    ``cuda``
        Require CUDA. Error if it is unavailable, regardless of the fallback
        flag — an explicit request is a hard requirement.

    ``cpu``
        Run on CPU. This is a legitimate, supported choice and never errors.
    """
    req = (requested if requested is not None else os.getenv("NOVA_TTS_DEVICE", "auto"))
    req = (str(req).strip() or "auto").lower()

    if allow_cpu_fallback is None:
        allow_cpu_fallback = os.getenv("NOVA_TTS_ALLOW_CPU_FALLBACK", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }

    if cuda_available is None:
        cuda_available = _cuda_available()

    if req == "cpu":
        return "cpu", "configured: NOVA_TTS_DEVICE=cpu"

    if req.startswith("cuda"):
        if not cuda_available:
            raise TtsDeviceError(
                f"NOVA_TTS_DEVICE={req} requires CUDA, but torch.cuda.is_available() is false. "
                "Install a CUDA-enabled PyTorch/TorchAudio stack, or set NOVA_TTS_DEVICE=cpu "
                "to run the voice on CPU deliberately."
            )
        return req, f"configured: NOVA_TTS_DEVICE={req}"

    if req != "auto":
        raise TtsDeviceError(
            f"NOVA_TTS_DEVICE={req!r} is not a valid device. Use auto, cuda, or cpu."
        )

    if cuda_available:
        return "cuda", "auto: CUDA available, XTTS isolated in its own process"
    if allow_cpu_fallback:
        return "cpu", "auto: CUDA unavailable, NOVA_TTS_ALLOW_CPU_FALLBACK=1"
    raise TtsDeviceError(
        "NOVA_TTS_DEVICE=auto wanted CUDA but torch.cuda.is_available() is false. "
        "Nova will not silently drop the voice to CPU. Either fix the CUDA install, "
        "set NOVA_TTS_ALLOW_CPU_FALLBACK=1 to accept the slower CPU voice, or set "
        "NOVA_TTS_DEVICE=cpu to choose CPU outright."
    )


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def wav_bytes_from_f32(samples: Any, sample_rate: int) -> bytes:
    """Float32 [-1, 1] mono samples -> 16-bit PCM WAV bytes."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def patch_torchaudio_load_for_soundfile() -> None:
    """Work around TorchAudio 2.9's hard dependency on torchcodec.

    Coqui TTS calls `torchaudio.load(...)` to read reference audio. In
    torchaudio 2.9 that routes through torchcodec unconditionally, which fails
    to load its native DLLs on Windows. Route it through `soundfile` instead
    (already a Nova dependency). Pragmatic shim: it covers the subset of
    `torchaudio.load` behaviour Coqui actually uses.
    """
    try:
        import numpy as np
        import soundfile as sf  # type: ignore
        import torch  # type: ignore
        import torchaudio  # type: ignore
    except Exception:
        return

    if getattr(torchaudio, "__nova_soundfile_load_patched__", False):
        return

    def _load_with_soundfile(
        uri,
        frame_offset: int = 0,
        num_frames: int = -1,
        normalize: bool = True,  # noqa: ARG001
        channels_first: bool = True,
        format: str | None = None,  # noqa: ARG001
        buffer_size: int = 4096,  # noqa: ARG001
        backend: str | None = None,  # noqa: ARG001
    ):
        if hasattr(uri, "read"):
            raw = uri.read()
            data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        else:
            data, sr = sf.read(uri, dtype="float32", always_2d=True)

        if frame_offset:
            data = data[int(frame_offset):]
        if num_frames is not None and int(num_frames) > 0:
            data = data[: int(num_frames)]

        tensor = torch.from_numpy(np.ascontiguousarray(data.T if channels_first else data))
        return tensor, int(sr)

    torchaudio.load = _load_with_soundfile  # type: ignore[assignment]
    torchaudio.__nova_soundfile_load_patched__ = True


def ensure_model_downloaded(progress: Progress = _noop) -> None:
    """Guard the XTTS first-download failure modes.

    Coqui gates XTTS v2 behind an interactive licence prompt. Inside a child
    process with no usable stdin that prompt blocks forever — Nova would type
    but never speak, with no visible error. An aborted download also leaves an
    empty cache dir that makes ModelManager skip the download and then fail on
    missing files.
    """
    try:
        from TTS.utils.generic_utils import get_user_data_dir  # type: ignore

        cache_dir = Path(get_user_data_dir("tts")) / "tts_models--multilingual--multi-dataset--xtts_v2"
    except Exception:
        return

    if (cache_dir / "config.json").exists() and (cache_dir / "model.pth").exists():
        return

    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

    if os.getenv("COQUI_TOS_AGREED", "").strip() != "1":
        raise RuntimeError(
            "XTTS v2 is not downloaded yet, and downloading it requires accepting the Coqui CPML "
            "license (non-commercial). Set COQUI_TOS_AGREED=1 in .env, restart Nova, and the model "
            "(~1.9 GB) will download on first use. License: https://coqui.ai/cpml"
        )

    progress("Downloading XTTS v2 from HuggingFace (~1.9 GB, one-time)...")
    try:
        from huggingface_hub import hf_hub_download  # type: ignore

        cache_dir.mkdir(parents=True, exist_ok=True)
        for filename in _HF_FILES:
            hf_hub_download(repo_id="coqui/XTTS-v2", filename=filename, local_dir=str(cache_dir))
        # The marker TTS writes after an interactive TOS agreement; stops its
        # downloader from ever re-prompting.
        (cache_dir / "tos_agreed.txt").write_text(
            "I have read, understood and agreed to the Terms and Conditions.\n", encoding="utf-8"
        )
        progress("XTTS v2 download complete")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(cache_dir, ignore_errors=True)
        raise RuntimeError(
            f"XTTS v2 download from HuggingFace failed: {e}. "
            "Check your internet connection and try again."
        ) from e


def _silent_call(fn, *args, **kwargs):
    import contextlib

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        return fn(*args, **kwargs)


def load_engine(device: str, progress: Progress = _noop) -> tuple[Any, int]:
    """Load XTTS onto `device`. Returns (tts, output_sample_rate).

    The retry loop exists because PyTorch 2.6+ defaults to `weights_only`
    loading, which rejects Coqui checkpoints until the specific TTS.* classes
    they pickle are allowlisted. torch reports them one at a time, so we
    allowlist exactly what it names and retry — never a blanket
    `weights_only=False`.
    """
    import importlib
    import re
    import warnings

    import torch  # type: ignore

    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API\..*",
        category=UserWarning,
        module=r"jieba\._compat",
    )

    patch_torchaudio_load_for_soundfile()
    from TTS.api import TTS  # type: ignore

    ensure_model_downloaded(progress)
    allowlisted: set[str] = set()

    for _ in range(32):
        try:
            tts = _silent_call(TTS, MODEL_ID)
            if device != "cpu":
                tts = tts.to(device)
            sr = int(getattr(tts.synthesizer, "output_sample_rate", 24000))
            return tts, sr
        except Exception as e:  # noqa: BLE001
            m = re.search(r"Unsupported global: GLOBAL ([A-Za-z0-9_\.]+)", str(e))
            if not m:
                raise
            dotted = m.group(1).strip()
            if not dotted.startswith("TTS.") or dotted in allowlisted:
                raise
            mod_name, attr = dotted.rsplit(".", 1)
            obj = getattr(importlib.import_module(mod_name), attr)
            torch.serialization.add_safe_globals([(obj, dotted)])  # type: ignore[attr-defined]
            allowlisted.add(dotted)

    raise RuntimeError("tts_model_load_failed: exceeded allowlist attempts")


def synthesize(tts: Any, sample_rate: int, *, text: str, speaker_wav: str,
               language: str = "en", speed: float = 1.0) -> bytes:
    """One synthesis call -> WAV bytes. Synchronous and blocking by design;
    the child process has nothing else to do."""
    wav = _silent_call(
        tts.tts, text=text, speaker_wav=speaker_wav, language=language, speed=float(speed)
    )
    return wav_bytes_from_f32(wav, sample_rate)
