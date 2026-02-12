from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.logging_setup import get_logger


logger = get_logger(__name__)


class GPUEnforcementError(RuntimeError):
    pass


@dataclass
class GpuStatus:
    required: bool
    active: bool
    status: str
    details: str | None = None


def _windows_cuda_install_hint() -> str:
    return (
        "CUDA offload was not detected. Nova requires CUDA GPU offload and refuses CPU fallback.\n\n"
        "On Windows, you must install a CUDA-enabled build of llama-cpp-python==0.3.4.\n"
        "Common approaches:\n"
        "- Install a prebuilt CUDA wheel matching your Python (3.11) and CUDA version.\n"
        "- Or build from source with CMake enabling CUDA (ggml-cuda).\n\n"
        "Verify you see CUDA/ggml_cuda initialization logs during model load, and that n_gpu_layers=-1 offloads layers."
    )


_GPU_LOG_PATTERNS = [
    re.compile(r"ggml_cuda", re.IGNORECASE),
    re.compile(r"\bCUDA\b", re.IGNORECASE),
    re.compile(r"offload", re.IGNORECASE),
    re.compile(r"\bGPU\b", re.IGNORECASE),
]


def _looks_like_gpu_offload(log_text: str) -> bool:
    if not log_text:
        return False
    # Require at least a CUDA-related marker AND an offload marker.
    has_cuda = bool(re.search(r"ggml_cuda|\bCUDA\b", log_text, flags=re.IGNORECASE))
    has_offload = bool(re.search(r"offload|offloading", log_text, flags=re.IGNORECASE))
    return has_cuda and has_offload


class LLMRuntime:
    def __init__(self, model_path: Path | None, context_tokens: int = 8192):
        self._model_path = model_path
        self._context_tokens = int(context_tokens)
        self._llama: Any | None = None
        self._gpu_status = GpuStatus(required=bool(model_path), active=False, status="model_missing" if not model_path else "not_loaded")
        self._init_lock = asyncio.Lock()

    @property
    def model_loaded(self) -> bool:
        return self._llama is not None

    @property
    def gpu_status(self) -> GpuStatus:
        return self._gpu_status

    async def initialize(self) -> None:
        if self._llama is not None or self._model_path is None:
            return
        async with self._init_lock:
            if self._llama is not None:
                return
            llama, logs = await asyncio.to_thread(self._load_llama_strict)
            if not _looks_like_gpu_offload(logs):
                self._gpu_status = GpuStatus(required=True, active=False, status="gpu_offload_not_confirmed", details=_windows_cuda_install_hint())
                raise GPUEnforcementError(self._gpu_status.details)
            self._llama = llama
            self._gpu_status = GpuStatus(required=True, active=True, status="gpu_offload_confirmed")
            logger.info("llm_loaded", model=str(self._model_path), n_ctx=self._context_tokens)

    def _load_llama_strict(self) -> tuple[Any, str]:
        if self._model_path is None:
            raise RuntimeError("Model path is None")
        if not self._model_path.exists():
            raise FileNotFoundError(f"Model not found: {self._model_path}")

        # Import inside thread to keep startup flexible.
        from llama_cpp import Llama  # type: ignore

        buf = io.StringIO()
        logs: list[str] = []

        # Try to capture llama.cpp logs via callback if available.
        log_cb_set: Callable[..., Any] | None = None
        try:
            import llama_cpp  # type: ignore

            log_cb_set = getattr(llama_cpp, "llama_log_set", None)
        except Exception:
            log_cb_set = None

        if callable(log_cb_set):
            def _cb(level: int, text: bytes, user_data: Any) -> None:  # noqa: ARG001
                try:
                    s = text.decode("utf-8", errors="ignore")
                except Exception:
                    s = str(text)
                logs.append(s)

            try:
                log_cb_set(_cb, None)
            except Exception:
                log_cb_set = None

        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            llama = Llama(
                model_path=str(self._model_path),
                n_ctx=self._context_tokens,
                n_gpu_layers=-1,
                main_gpu=int(os.getenv("NOVA_MAIN_GPU", "0")),
                verbose=True,
            )

        combined = "".join(logs) + "\n" + buf.getvalue()
        return llama, combined

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.1,
        stop: list[str] | None = None,
    ) -> str:
        await self.initialize()
        if self._llama is None:
            raise RuntimeError("LLM not loaded")

        default_stop = [
            "\n\nUser:",
            "\n\nAssistant:",
            "\nUser:",
            "\nAssistant:",
            "\nNova:",
            # Defensive stops: terminate if the model begins emitting metadata/code fences.
            "\n#",
            "\n```",
            "```",
        ]
        stop_seq = stop or default_stop

        def _run() -> str:
            out = self._llama(
                prompt,
                max_tokens=int(max_tokens),
                temperature=float(temperature),
                # Reduce loopiness / "ramble" under small models.
                top_k=40,
                top_p=0.9,
                repeat_penalty=1.15,
                # Stop sequences.
                stop=stop_seq,
            )
            return str(out.get("choices", [{}])[0].get("text", "")).strip()

        return await asyncio.to_thread(_run)
