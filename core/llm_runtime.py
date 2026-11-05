from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.logging_setup import get_logger


logger = get_logger(__name__)


def _perf_logging() -> bool:
    return (os.getenv("NOVA_LLM_PERF_LOG", "0").strip() or "0").lower() in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def _quiet_generation():
    """Keep a generation from writing to the console, WITHOUT touching globals.

    Every model call used to run inside `contextlib.redirect_stdout(StringIO())`
    to swallow llama.cpp's per-call timing lines. `sys.stdout` is
    process-global, so for the length of a generation anything printed by any
    other thread went into that StringIO and was lost, and two overlapping
    redirects restored it out of order and left stdout pointing at a dead
    object — every later print gone (audit 2026-11-02, M21). This suite's own
    concurrency test lost its output to exactly that.

    The lines are suppressed at the source instead: the model is loaded with
    `verbose=False` unless NOVA_LLM_PERF_LOG asks for them, in which case they
    are wanted and go to the console like any other log. So there is nothing
    left to redirect, and this stays a context manager only so the call sites
    read the same either way.
    """
    yield


@contextlib.contextmanager
def _hold(lock: threading.Lock, timeout_s: float, what: str):
    """Own the model, or fail honestly. Runs on the WORKER thread.

    The point of acquiring here rather than in the coroutine is that this cannot
    be cancelled away: a caller that gives up, times out or is superseded does
    not release the model early, because the model was never theirs to release —
    the thread doing the work holds it (audit 2026-11-02, C2).

    The timeout exists only so a leaked worker cannot starve the thread pool
    forever. Reaching it means something is genuinely stuck, which is worth an
    exception rather than a hang.
    """
    if not lock.acquire(timeout=timeout_s):
        raise GPUEnforcementError(
            f"the model was still busy after {timeout_s:.0f}s, so this {what} "
            f"call was dropped rather than run alongside it")
    try:
        yield
    finally:
        lock.release()


@dataclass(frozen=True)
class _FinishReason:
    """Why llama.cpp stopped, carried through the token queue.

    Not stored on the runtime instance: two concurrent generations would
    overwrite each other's completion state, and the whole point of knowing the
    reason is to trust it.
    """

    reason: str


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
        "On Windows, install a CUDA-enabled build of llama-cpp-python that supports the current model.\n"
        "For this repo's Qwen2-VL setup, the validated Windows build is llama-cpp-python==0.3.23.\n"
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


def _supported_chat_formats() -> set[str]:
    """Best-effort introspection of llama-cpp-python chat handlers.

    Falls back to a conservative set if introspection fails.
    """
    try:
        from llama_cpp import llama_chat_format as lcf  # type: ignore
        reg = lcf.LlamaChatCompletionHandlerRegistry()
        handlers = getattr(reg, "_chat_handlers", None)
        if isinstance(handlers, dict) and handlers:
            return set(handlers.keys())
    except Exception:
        pass
    # Conservative fallback (matches common llama-cpp-python handler names).
    return {
        "llama-2",
        "llama-3",
        "alpaca",
        "qwen",
        "vicuna",
        "oasst_llama",
        "baichuan-2",
        "baichuan",
        "openbuddy",
        "redpajama-incite",
        "snoozy",
        "phind",
        "intel",
        "open-orca",
        "mistrallite",
        "zephyr",
        "pygmalion",
        "chatml",
        "mistral-instruct",
        "chatglm3",
        "openchat",
        "saiga",
        "gemma",
        "functionary",
        "functionary-v2",
        "functionary-v1",
        "chatml-function-calling",
    }


def _pick_chat_format(model_name: str, requested: str | None) -> str | None:
    """Pick a valid llama-cpp chat_format for the current model.

    - Honors NOVA_CHAT_FORMAT if it matches a supported handler
    - Otherwise uses lightweight filename heuristics + safe fallbacks
    """
    supported = _supported_chat_formats()

    def _is_supported(x: str | None) -> bool:
        return bool(x) and x in supported

    name = (model_name or "").lower()

    # First choice: explicit request if valid.
    if _is_supported(requested):
        return requested

    # Filename heuristics.
    candidates: list[str] = []

    if "qwen" in name:
        candidates += ["qwen", "chatml"]
    if "dolphin" in name and ("llama-3" in name or "llama3" in name):
        candidates += ["llama-3", "chatml"]
    elif "llama-3" in name or "llama3" in name:
        candidates += ["llama-3"]
    if "llama-2" in name or "llama2" in name:
        candidates += ["llama-2"]
    if "mistral" in name:
        candidates += ["mistral-instruct", "chatml"]
    if "gemma" in name:
        candidates += ["gemma"]
    if "phi" in name:
        candidates += ["chatml"]

    # Universal fallbacks (ordered).
    candidates += ["qwen", "chatml", "llama-3", "llama-2", "mistral-instruct", "gemma", "vicuna", "alpaca"]

    for c in candidates:
        if c in supported:
            return c

    return None


def _looks_like_gpu_offload(log_text: str) -> bool:
    if not log_text:
        return False
    # Require at least a CUDA-related marker AND an offload marker.
    has_cuda = bool(re.search(r"ggml_cuda|\bCUDA\b", log_text, flags=re.IGNORECASE))
    has_offload = bool(re.search(r"offload|offloading", log_text, flags=re.IGNORECASE))
    return has_cuda and has_offload


def _normalize_model_name(name: str) -> str:
    cleaned = (name or "").lower()
    cleaned = re.sub(r"\.gguf$", "", cleaned)
    cleaned = re.sub(r"^mmproj[-_]?", "", cleaned)
    cleaned = re.sub(r"-(q\d.*|iq\d.*|f16|bf16|fp16|fp32)$", "", cleaned)
    cleaned = re.sub(r"[-_](q\d.*|iq\d.*|f16|bf16|fp16|fp32)$", "", cleaned)
    cleaned = re.sub(r"[-_]+", "-", cleaned).strip("-")
    return cleaned


def _auto_pick_mmproj(model_path: Path | None) -> Path | None:
    if model_path is None:
        return None

    model_dir = model_path.parent
    if not model_dir.exists():
        return None

    mmproj_candidates = [
        p for p in model_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".gguf" and "mmproj" in p.name.lower()
    ]
    if not mmproj_candidates:
        return None

    mmproj_candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    target = _normalize_model_name(model_path.name)
    for candidate in mmproj_candidates:
        candidate_name = _normalize_model_name(candidate.name)
        if candidate_name == target or candidate_name in target or target in candidate_name:
            return candidate

    return mmproj_candidates[0]


def _looks_like_vision_model(model_name: str) -> bool:
    name = (model_name or "").lower()
    return ("vl" in name) or ("vision" in name) or ("llava" in name)


def _vision_reason(model_path: Path | None, mmproj_path: Path | None) -> str:
    """Whether vision is usable right now.

    The model's FILENAME is not the real requirement — a resolved, existing
    mmproj file is. `_auto_pick_mmproj` already finds a matching mmproj next to
    the model regardless of naming (e.g. Qwen3.5-9B-Q6_K.gguf + a sibling
    mmproj-Qwen3.5-9B-BF16.gguf), so that discovery result must be trusted
    first. The name heuristic is only used as a last resort, to give a more
    specific "you look vision-capable but no mmproj was found" message instead
    of a blanket "text-only" one when discovery genuinely comes up empty.
    """
    if model_path is None:
        return "No model is loaded."
    if os.getenv("NOVA_VISION_FORCE", "").strip() == "1":
        if mmproj_path is not None and mmproj_path.exists():
            return "ready"
        return "NOVA_VISION_FORCE=1 is set but no mmproj file was found or NOVA_MMPROJ_PATH is invalid."
    if mmproj_path is not None:
        return "ready" if mmproj_path.exists() else f"Vision projector file not found: {mmproj_path}"
    if _looks_like_vision_model(model_path.name):
        return (
            "Vision projector not found. Put a matching mmproj-*.gguf next to the model "
            "or set NOVA_MMPROJ_PATH."
        )
    return "Current model is text-only; no mmproj is needed."


#: Repetition penalty for PROSE. A reply that keeps re-using the same phrase
#: reads badly, and 1.15 is what was measured as natural here.
PROSE_REPEAT_PENALTY = 1.15

#: Repetition penalty for CODE, which is repetitive BY DESIGN: `self.`,
#: `return`, `if not`, a closing brace, the same indentation. Penalising that
#: pushes the model off the correct token, and it was being applied to every
#: generated source file (audit, L12). 1.0 is no penalty at all.
CODE_REPEAT_PENALTY = 1.0

class LLMRuntime:
    def __init__(self, model_path: Path | None, context_tokens: int = 8192):
        self._model_path = model_path
        self._context_tokens = int(context_tokens)
        # Optional chat/multimodal wiring (used for Qwen2.5-VL and other chat-formatted models).
        self._chat_format = os.getenv("NOVA_CHAT_FORMAT", "").strip() or None
        raw_mmproj = os.getenv("NOVA_MMPROJ_PATH", "").strip()
        if raw_mmproj:
            self._mmproj_path = str(Path(raw_mmproj).expanduser().resolve())
        else:
            auto_mmproj = _auto_pick_mmproj(model_path)
            self._mmproj_path = str(auto_mmproj) if auto_mmproj is not None else None
        # Auto-detect chat format for common models (optional).
        if self._chat_format is None and model_path is not None:
            name = model_path.name.lower()
            if "qwen" in name:
                self._chat_format = "qwen"  # llama-cpp chat handler name
            elif "llama-3" in name or "llama3" in name:
                self._chat_format = "llama-3"
        self._llama: Any | None = None
        self._vision_llama: Any | None = None
        # ── THE CHOKEPOINT (audit 2026-11-02, C2) ────────────────────────────
        #
        # `core/gpu.py`'s semaphore was the only thing serialising the model,
        # and it cannot do the job: the work runs in `asyncio.to_thread`, which
        # CANNOT be cancelled, so any timeout or early exit releases the permit
        # while the worker thread is still inside `Llama`. The next caller then
        # walks in and llama.cpp aborts the whole process —
        # `ggml-cuda.cu:102 CUDA error`, exit 127, no traceback.
        #
        # Measured: closing a reply stream partway through killed Nova 7 times
        # out of 7. The Stop button, sending while she is replying, and voice
        # barge-in all do exactly that.
        #
        # A threading.Lock acquired INSIDE the worker and held across the whole
        # llama call is the one guard cancellation cannot step around: a second
        # thread blocks here instead of entering the model. The semaphore stays
        # — it keeps callers from queueing up in the first place — but it is no
        # longer what makes this safe.
        self._llama_lock = threading.Lock()
        #: How long a queued generation waits for the model before giving up.
        #: Generous: this is "wait your turn", and the only thing it protects
        #: against is a leaked thread holding the lock forever and starving the
        #: thread pool. An honest failure beats a hung assistant.
        self._lock_wait_s = float(os.getenv("NOVA_LLM_LOCK_WAIT_S", "300") or 300)
        #: Vision requests queued or in flight — see `vision_analyze`.
        self._vision_pending = 0
        self._warmed = False
        self._gpu_status = GpuStatus(required=bool(model_path), active=False, status="model_missing" if not model_path else "not_loaded")
        self._init_lock = asyncio.Lock()
        # Rolling token usage for the UI (updated by chat/chat_stream).
        self._usage: dict[str, float | int] = {
            "replies": 0,
            "last_prompt_tokens": 0,
            "last_reply_tokens": 0,
            "avg_reply_tokens": 0.0,
            # A generation that produces no VISIBLE output (the reasoning model
            # spending its whole budget on an unclosed <think>) is silently
            # retried below. Each retry is a full extra generation, so this is
            # pure latency the user feels and nothing else reported. Measured at
            # up to two wasted generations on a single conversational turn.
            "empty_retries": 0,
            "empty_exhausted": 0,
            # A stream that produced nothing and had to be rescued by the
            # runtime's non-streaming call. Counted separately from
            # `empty_exhausted` so "we recovered" and "we gave up" stay
            # distinguishable in telemetry.
            "empty_salvaged": 0,
        }

    @property
    def usage_stats(self) -> dict[str, float | int]:
        return dict(self._usage)

    def _record_usage(self, *, prompt_tokens: int | None, reply_tokens: int) -> None:
        try:
            self._usage["replies"] = int(self._usage["replies"]) + 1
            if prompt_tokens is not None:
                self._usage["last_prompt_tokens"] = int(prompt_tokens)
            self._usage["last_reply_tokens"] = int(reply_tokens)
            prev_avg = float(self._usage["avg_reply_tokens"])
            n = int(self._usage["replies"])
            self._usage["avg_reply_tokens"] = round(prev_avg + (reply_tokens - prev_avg) / max(n, 1), 1)
        except Exception:
            pass

    @property
    def context_tokens(self) -> int:
        """The window a prompt has to fit inside. See core/prompt_budget.py."""
        return int(self._context_tokens)

    @property
    def model_loaded(self) -> bool:
        return self._llama is not None

    @property
    def gpu_status(self) -> GpuStatus:
        return self._gpu_status

    @property
    def vision_status(self) -> dict[str, str | bool | None]:
        mmproj_path = Path(self._mmproj_path) if self._mmproj_path else None
        reason = _vision_reason(self._model_path, mmproj_path)
        return {
            "enabled": reason == "ready",
            "reason": reason,
            "mmproj_path": str(mmproj_path) if mmproj_path is not None else None,
        }

    async def initialize(self) -> None:
        if self._llama is not None or self._model_path is None:
            return
        async with self._init_lock:
            if self._llama is not None:
                return
            llama, logs = await asyncio.to_thread(self._load_llama_text_only)
            if not _looks_like_gpu_offload(logs):
                self._gpu_status = GpuStatus(required=True, active=False, status="gpu_offload_not_confirmed", details=_windows_cuda_install_hint())
                raise GPUEnforcementError(self._gpu_status.details)
            self._llama = llama
            self._gpu_status = GpuStatus(required=True, active=True, status="gpu_offload_confirmed")
            logger.info("llm_loaded", model=str(self._model_path), n_ctx=self._context_tokens)

    async def warm_up(self) -> float:
        """Burn the first inference at BOOT, where nothing is waiting on it.

        The build of llama-cpp-python in this venv ships no `sm_120` kernels, so
        on an RTX 5080 they are JIT-compiled on first use and cached by the
        driver. That takes ~15.7s cold and ~0.3s warm — and the first thing a
        turn does is a model call with a 12s timeout around it, so a cold cache
        meant the timeout fired mid-compile, released the GPU permit, and the
        next call entered the model beside a thread that was still in it: 4
        aborted boots out of 4 (audit 2026-11-02, C2).

        Doing it here means the compile happens once, with no deadline, before
        anyone can be let down by it. Anything that empties the driver's cache —
        a driver update, cache eviction — is absorbed the same way.

        Returns the seconds it took; 0.0 when there is nothing to warm.
        """
        if self._warmed or self._model_path is None:
            return 0.0
        self._warmed = True
        import time as _time

        t0 = _time.perf_counter()
        try:
            await self.initialize()
            await self.chat([{"role": "user", "content": "ok"}],
                            max_tokens=4, temperature=0.0)
        except Exception as e:  # noqa: BLE001
            # A failed warm-up is not a failed boot: the next real call will
            # report whatever is wrong. Saying so is the point.
            logger.warning("llm_warmup_failed", error=str(e)[:200])
            return 0.0
        took = _time.perf_counter() - t0
        logger.info("llm_warmed", seconds=round(took, 2))
        return took

    def _load_llama_text_only(self) -> tuple[Any, str]:
        if self._model_path is None:
            raise RuntimeError("Model path is None")
        if not self._model_path.exists():
            raise FileNotFoundError(f"Model not found: {self._model_path}")

        # Import inside thread to keep startup flexible.
        from llama_cpp import Llama  # type: ignore

        buf = io.StringIO()
        # BOUNDED. The callback installed below lives for the life of the
        # process — llama.cpp keeps calling it on every generation — and it used
        # to append into a plain list captured by this closure, so every log
        # line llama ever emitted was retained forever (audit, M21 neighbourhood).
        # Only the load-time lines are ever read, and there are a few dozen.
        logs: deque[str] = deque(maxlen=400)

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
            llama_kwargs: dict[str, Any] = dict(
                model_path=str(self._model_path),
                n_ctx=self._context_tokens,
                n_gpu_layers=-1,
                main_gpu=int(os.getenv("NOVA_MAIN_GPU", "0")),
                verbose=True,
            )

            # Normal chat stays text-only to avoid dumping serialized prompts/history
            # and to avoid loading the vision projector path on every text request.
            requested_format = (self._chat_format or "").strip() or None
            picked_format = _pick_chat_format(self._model_path.name if self._model_path else "", requested_format)
            if picked_format:
                llama_kwargs["chat_format"] = picked_format

            llama = Llama(**llama_kwargs)
            _install_thinking_switch(llama)

        combined = "".join(logs) + "\n" + buf.getvalue()
        # Generation is QUIET from here on, and that is what lets the per-call
        # `redirect_stdout` go away. `verbose=True` is needed above — the
        # GPU-offload confirmation is only in those load logs — but keeping it on
        # meant every generation printed timings, which is why each call wrapped
        # itself in a redirect of the process-global stdout. That swallowed
        # anything ANOTHER thread printed during the window, and restored out of
        # order under concurrency (audit, M21). Nothing to suppress, nothing to
        # redirect.
        if not _perf_logging():
            with contextlib.suppress(Exception):
                llama.verbose = False
        return llama, combined


    @staticmethod
    def _emit_perf_lines(log_text: str) -> None:
        """Print llama's own timing lines out of a captured log.

        No longer called per generation: with NOVA_LLM_PERF_LOG off the model is
        loaded quiet, and with it on llama prints these itself. Kept because the
        LOAD path still captures a log, and a caller that wants to surface the
        timings from it should use this rather than inventing a second filter.
        """
        if not _perf_logging() or not log_text:
            return
        keep_prefixes = ("Llama.generate:", "llama_perf_context_print:")
        seen: set[str] = set()
        for raw in log_text.splitlines():
            line = raw.strip()
            if not line or line in seen:
                continue
            if any(line.startswith(prefix) for prefix in keep_prefixes):
                print(line)
                seen.add(line)

    def _load_vision_llama(self) -> Any:
        if self._model_path is None:
            raise RuntimeError("Model path is None")
        mmproj = (self._mmproj_path or "").strip()
        if not mmproj:
            raise RuntimeError("Vision projector is not configured.")
        mmproj_path = Path(mmproj)
        if not mmproj_path.exists():
            raise FileNotFoundError(f"mmproj not found: {mmproj_path}")

        from llama_cpp import Llama  # type: ignore
        try:
            from llama_cpp.llama_chat_format import Qwen25VLChatHandler  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "Qwen2.5-VL chat handler not available in your llama-cpp-python build. "
                "Upgrade llama-cpp-python to a version that includes Qwen25VLChatHandler."
            ) from e

        requested_format = (self._chat_format or "").strip() or None
        picked_format = _pick_chat_format(self._model_path.name if self._model_path else "", requested_format)

        # Vision runtime is loaded only on demand, and stays quiet.
        llama_kwargs: dict[str, Any] = dict(
            model_path=str(self._model_path),
            n_ctx=self._context_tokens,
            n_gpu_layers=-1,
            main_gpu=int(os.getenv("NOVA_MAIN_GPU", "0")),
            verbose=False,
            chat_handler=Qwen25VLChatHandler(clip_model_path=str(mmproj_path)),
        )
        if picked_format:
            llama_kwargs["chat_format"] = picked_format
        return Llama(**llama_kwargs)

    async def _ensure_vision_llama(self) -> Any:
        await self.initialize()
        if self._vision_llama is None:
            self._vision_llama = await asyncio.to_thread(self._load_vision_llama)
        return self._vision_llama

    def _free_vision_llama(self) -> None:
        """Give the VRAM back.

        `_load_vision_llama` loads the SAME weights a second time with a vision
        projector attached, and nothing ever unloaded it. On the 16 GB card that
        took usage from 11.5 GB to 15.9 GB and left it there, so every later
        chat turn ran at 55-108s instead of 4-5s, until a restart. Measured 3
        times out of 3 (audit 2026-11-02, H21).
        """
        llama, self._vision_llama = self._vision_llama, None
        if llama is None:
            return
        try:
            closer = getattr(llama, "close", None)
            if callable(closer):
                closer()
        except Exception as e:  # noqa: BLE001
            # DEBUG on purpose (audit 2026-11-02, M12, which raised 56 real
            # failures to warning): a close() on the way out, with nothing pending.
            logger.debug("vision_close_failed", error=str(e)[:160])
        del llama
        import gc

        gc.collect()
        logger.info("vision_model_unloaded")

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.1,
        stop: list[str] | None = None,
    ) -> str:
        """Legacy text-only completion API.

        For chat-formatted models (e.g., Qwen2.5-VL), this routes through chat completion under the hood.
        """
        messages = [{"role": "user", "content": prompt}]
        return await self.chat(messages, max_tokens=max_tokens, temperature=temperature, stop=stop)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        thinking: bool = False,
        repeat_penalty: float | None = None,
    ) -> str:
        await self.initialize()
        if self._llama is None:
            raise RuntimeError("LLM not loaded")

        # NOTE: deliberately no single-newline "\nUser:"/"\nAssistant:" stops here.
        # Reasoning models (Qwen3.5) narrate hypothetical dialogue turns like
        # "User: ... Assistant: ..." inside their own <think> block using plain
        # single newlines, which matched these stops and truncated generation
        # before the real answer ever came out. Double-newline variants are kept
        # as a much lower-risk safety net against genuine runaway continuation.
        default_stop = [
            "\n\nUser:",
            "\n\nAssistant:",
        ]
        # `stop=[]` must mean "no stop sequences" (used by callers writing raw
        # file content that may legitimately contain these words) — `stop or
        # default_stop` would wrongly treat an empty list as falsy and still
        # apply the defaults, so check for None explicitly.
        stop_seq = default_stop if stop is None else stop
        _penalty = float(PROSE_REPEAT_PENALTY if repeat_penalty is None
                         else repeat_penalty)
        messages = _apply_no_think(messages, thinking=thinking)

        def _run() -> str:
            with _hold(self._llama_lock, self._lock_wait_s, "chat"):
                with _quiet_generation():
                    # Prefer chat-completions for modern instruct models.
                    if hasattr(self._llama, "create_chat_completion"):
                        out = self._llama.create_chat_completion(
                            messages=messages,
                            max_tokens=int(max_tokens),
                            temperature=float(temperature),
                            stop=stop_seq,
                            top_k=40,
                            top_p=0.9,
                            repeat_penalty=_penalty,
                        )
                        result = _extract_chat_text(out)
                        usage = (out or {}).get("usage") or {}
                        self._record_usage(
                            prompt_tokens=usage.get("prompt_tokens"),
                            reply_tokens=int(usage.get("completion_tokens") or 0),
                        )
                    else:
                        # Fallback: raw completion
                        out = self._llama(
                            "\n".join([m.get("content", "") for m in messages if isinstance(m.get("content"), str)]),
                            max_tokens=int(max_tokens),
                            temperature=float(temperature),
                            top_k=40,
                            top_p=0.9,
                            repeat_penalty=_penalty,
                            stop=stop_seq,
                        )
                        result = str(out.get("choices", [{}])[0].get("text", "")).strip()
            return _strip_think(result)

        # Occasionally the reasoning model burns its whole generation on an
        # unclosed <think> block and nothing visible ever comes out. That's
        # invisible to any caller (nothing was returned yet), so it's always
        # safe to silently retry a few times before giving up.
        result = ""
        for attempt in range(3):
            result = await asyncio.to_thread(_run)
            if result:
                break
            if attempt < 2:
                logger.debug("llm_chat_empty_retry", attempt=attempt + 1)
        return result

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        thinking: bool = False,
        repeat_penalty: float | None = None,
    ):
        """Yield response text deltas. Unchanged contract for every caller.

        Callers that need to know WHY generation ended use `chat_stream_ex`.
        """
        # `aclosing` so that closing THIS generator deterministically closes the
        # one underneath. Without it the inner generator waited for garbage
        # collection, and until it was collected its worker thread kept decoding
        # tokens nobody wanted — which is the abandoned-stream half of C2.
        inner = self.chat_stream_ex(
            messages, max_tokens=max_tokens, temperature=temperature,
            stop=stop, thinking=thinking, repeat_penalty=repeat_penalty,
        )
        async with contextlib.aclosing(inner):
            async for event in inner:
                if event.get("type") == "token":
                    yield event["text"]

    async def chat_stream_ex(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        thinking: bool = False,
        repeat_penalty: float | None = None,
    ):
        """Async generator yielding response text deltas as llama.cpp produces them.

        Runs the blocking llama-cpp stream in a worker thread and hands tokens
        to the event loop through a queue, so the SSE endpoint can forward each
        token the moment it exists.
        """
        await self.initialize()
        if self._llama is None:
            raise RuntimeError("LLM not loaded")

        # See the matching note in chat() above: single-newline stops falsely
        # trigger inside the model's own <think> narration and must be avoided.
        default_stop = ["\n\nUser:", "\n\nAssistant:"]
        # See chat() above: an explicit empty list must disable stop sequences
        # entirely rather than falling back to the defaults.
        stop_seq = default_stop if stop is None else stop
        _penalty = float(PROSE_REPEAT_PENALTY if repeat_penalty is None
                         else repeat_penalty)
        base_messages = list(messages)

        def _attempt_messages(attempt: int) -> tuple[list[dict[str, Any]], int]:
            """Messages and budget for attempt N.

            Attempts ESCALATE rather than repeat. The previous loop retried
            identically, so a prompt that reliably made the model spend its whole
            budget on an unclosed <think> burned three full generations and still
            returned nothing.

            The escalation DIRECTION inverted in V3 P2.5. It used to force
            /no_think on retry, because native reasoning was what overflowed.
            Now the fast path prefills a closed reasoning block and is the
            default, so the failure mode is the opposite: a genuinely hard
            question opens a SECOND block on top of the prefilled one and
            overflows inside it. Measured — the complex-reasoning scenario in
            tests/bench_nova_v3.py returned empty under a pure fast contract.

            So a fast attempt that produces nothing escalates to real reasoning
            rather than retrying fast. That is a self-correcting complexity
            router with no classifier to misjudge: turns that can be answered
            directly are, in ~126 ms, and turns that genuinely need thinking ask
            for it by failing the cheap path first.
            """
            if attempt == 0:
                return _apply_no_think(base_messages, thinking=thinking), int(max_tokens)
            if attempt == 1 and not thinking:
                # Retry the CHEAP path once before paying for reasoning.
                #
                # Fast attempts cost ~130ms, so a second one is nearly free,
                # while jumping straight to DEEP costs 14-20s. Escalating
                # immediately put a 19.7s P90 back on turns as simple as "Good
                # morning" — the failure is stochastic (the model sometimes
                # opens a second block on top of the prefilled one), so most of
                # them succeed on a second cheap try. Only a turn that fails the
                # cheap contract TWICE is treated as genuinely needing thought.
                return _apply_no_think(base_messages, thinking=False), int(max_tokens)
            # Final attempt: real reasoning, FULL budget, no extra instruction.
            #
            # This used to shrink the budget to max_tokens//3 and append a
            # "no reasoning block" nudge. Both were wrong here and measurably so:
            # a small budget WITH reasoning enabled is the exact combination
            # that overflows (the whole 512-token failure mode), and the nudge
            # contradicts the reasoning this attempt is deliberately asking for.
            # Together they made the complex-reasoning scenario fail all three
            # attempts. A turn that has already failed the cheap path twice has
            # earned the full budget.
            return _apply_no_think(base_messages, thinking=True), int(max_tokens)

        loop = asyncio.get_running_loop()

        def _produce(q: asyncio.Queue, messages: list[dict[str, Any]], max_tokens: int,
                     cancel: threading.Event) -> None:
            reply_tokens = 0
            try:
                # Held across the WHOLE stream, including iteration: each
                # `next(stream)` is a decode step inside llama.cpp, so the model
                # is in use until the loop ends (audit, C2).
                with _hold(self._llama_lock, self._lock_wait_s, "chat_stream"):
                    with _quiet_generation():
                        stream = self._llama.create_chat_completion(
                            messages=messages,
                            max_tokens=int(max_tokens),
                            temperature=float(temperature),
                            stop=stop_seq,
                            top_k=40,
                            top_p=0.9,
                            repeat_penalty=_penalty,
                            stream=True,
                        )
                        for chunk in stream:
                            # Nobody is reading any more: the turn was abandoned,
                            # the Stop button was pressed, or a new message
                            # superseded this one. Stop decoding rather than
                            # finishing a reply that will be thrown away — and,
                            # more importantly, release the model promptly so the
                            # next turn is not waiting on dead work.
                            if cancel.is_set():
                                with contextlib.suppress(Exception):
                                    stream.close()
                                break
                            try:
                                choice = (chunk.get("choices") or [{}])[0]
                                delta = choice.get("delta") or {}
                                token = delta.get("content")
                                reason = choice.get("finish_reason")
                                if reason:
                                    loop.call_soon_threadsafe(
                                        q.put_nowait, _FinishReason(str(reason)))
                            except Exception:
                                token = None
                            if token:
                                reply_tokens += 1
                                loop.call_soon_threadsafe(q.put_nowait, token)
                loop.call_soon_threadsafe(q.put_nowait, None)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                if reply_tokens:
                    # Each stream delta is one token; prompt size isn't reported
                    # by the streaming API, so leave the last known value.
                    self._record_usage(prompt_tokens=None, reply_tokens=reply_tokens)

        # Occasionally the reasoning model burns its whole generation on an
        # unclosed <think> block and nothing visible ever comes out. Nothing
        # gets yielded to the caller until real content is seen, so retrying
        # a fresh attempt from scratch is always safe/invisible when an
        # attempt produces zero visible output.
        for attempt in range(3):
            attempt_messages, attempt_budget = _attempt_messages(attempt)
            queue: asyncio.Queue[str | None | Exception] = asyncio.Queue(maxsize=512)
            cancel = threading.Event()
            producer = asyncio.create_task(
                asyncio.to_thread(_produce, queue, attempt_messages, attempt_budget, cancel)
            )
            think_filter = _ThinkStreamFilter()
            produced_any = False
            finish_reason = ""
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, _FinishReason):
                        # WHY this travels IN the stream rather than on the
                        # instance: two concurrent generations would otherwise
                        # overwrite each other's completion state.
                        finish_reason = item.reason
                        continue
                    if isinstance(item, Exception):
                        raise item
                    visible = think_filter.feed(item)
                    if visible:
                        produced_any = True
                        yield {"type": "token", "text": visible}
                tail = think_filter.flush()
                if tail:
                    produced_any = True
                    yield {"type": "token", "text": tail}
            finally:
                # Tell the worker to stop BEFORE waiting for it. This block runs
                # on a normal finish, on `aclose()`, and on GeneratorExit — which
                # is the abandoned-stream case: the consumer stopped reading and
                # the model would otherwise decode to the end of its budget
                # while nobody wanted the words.
                cancel.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(producer)
            if produced_any:
                # A caller cannot otherwise tell "the model finished" from "the
                # model ran out of budget mid-sentence" — which is exactly how a
                # long story ended mid-sentence with nothing downstream noticing.
                yield {"type": "done", "finish_reason": finish_reason or "stop"}
                return
            if attempt == 2:
                # All three attempts produced nothing visible. The caller gets an
                # empty stream and will substitute an apology; record it so the
                # failure is countable rather than invisible.
                self._usage["empty_exhausted"] = int(self._usage["empty_exhausted"]) + 1
                logger.warning("llm_chat_stream_empty_exhausted", attempts=3,
                               max_tokens=int(max_tokens),
                               finish_reason=finish_reason or "unknown")
                yield {"type": "done", "finish_reason": "empty",
                       "model_finish_reason": finish_reason or "unknown"}
                return
            self._usage["empty_retries"] = int(self._usage["empty_retries"]) + 1
            logger.debug("llm_chat_stream_empty_retry", attempt=attempt + 1)

    async def vision_analyze(
        self,
        image_bytes: bytes,
        question: str = "Describe the image in detail.",
        max_tokens: int = 768,
        temperature: float = 0.2,
    ) -> str:
        """Analyze an image using a multimodal model (e.g., Qwen2.5-VL).

        Requires NOVA_MMPROJ_PATH to be set (or the model to be otherwise configured for vision).
        """
        vision = self.vision_status
        if not bool(vision.get("enabled")):
            raise RuntimeError(str(vision.get("reason") or "Vision is not configured."))

        import base64

        data_uri = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('utf-8')}"

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": question},
                ],
            }
        ]

        # Two things this counter does. It lets a burst of image questions share
        # one load, and it tells the end of this call whether anybody else still
        # needs the vision model — which is what makes freeing it safe.
        self._vision_pending += 1
        try:
            llama = await self._ensure_vision_llama()

            def _run_vision() -> str:
                # Same lock as chat. Two llama.cpp contexts in one process are
                # two uncoordinated CUDA consumers, which is the arrangement
                # core/gpu.py exists because of — and vision took no permit at
                # all (audit, C2/H21).
                with _hold(self._llama_lock, self._lock_wait_s, "vision"):
                    with _quiet_generation():
                        out = llama.create_chat_completion(
                            messages=messages,
                            max_tokens=int(max_tokens),
                            temperature=float(temperature),
                            top_k=40,
                            top_p=0.9,
                            # Describing an image is prose.
                            repeat_penalty=PROSE_REPEAT_PENALTY,
                        )
                # Intentionally do not print vision startup/prompt internals to backend logs.
                return _strip_think(_extract_chat_text(out))

            return await asyncio.to_thread(_run_vision)
        finally:
            self._vision_pending -= 1
            if self._vision_pending <= 0:
                # Last one out frees the VRAM. A reload costs ~20s on the next
                # image; keeping it loaded cost 20x on every chat turn forever.
                await asyncio.to_thread(self._free_vision_llama)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_BLOCK_RE = re.compile(r"(?is)<think>.*?</think>")
_THINK_UNCLOSED_RE = re.compile(r"(?is)<think>.*$")


def _strip_think(text: str) -> str:
    """Remove Qwen3-style <think>...</think> reasoning blocks from a full reply.

    Reasoning models (e.g. Qwen3.5) emit their chain-of-thought inline as
    <think>...</think> before the real answer. That reasoning is meant to stay
    internal — it should never be shown or spoken. Also handles the case where
    generation got cut off mid-thought (no closing tag) by dropping everything
    from the unclosed <think> onward.
    """
    if "<think>" not in text and "</think>" not in text:
        return text.strip()
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


#: Seeds the assistant turn with an ALREADY-CLOSED reasoning block, so the model
#: continues from after it rather than opening its own. The think filter strips
#: the echoed prefix, so nothing reaches the user.
#:
#: MODEL-SPECIFIC. `<think>`/`</think>` is Qwen3-family syntax and this is
#: validated only against Qwen3.5-9B and its chat template. On another model it
#: would at best do nothing and at worst leak a literal "<think>" into the
#: spoken answer — or, given what V2 found about naming the tag, trigger the
#: pathology it exists to prevent. A model swap MUST re-measure with
#: tests/bench_ttft_v3c.py and select a contract per template rather than
#: assuming this one. See docs/NOVA_DECISIONS.md D3.
#: The marker `_apply_no_think` appends to say "render this turn with thinking
#: OFF". It is the same text the model's own chat template emits for
#: `enable_thinking=false`, which is why it used to be passed through as a real
#: assistant message — see `_install_thinking_switch` for why that was wrong.
_CLOSED_THINK_PREFILL = "<think>\n\n</think>\n\n"


def _install_thinking_switch(llama: Any) -> bool:
    """Make the fast path use the template's OWN thinking switch.

    AUDIT 2026-11-02, M10. The fast path appended a trailing assistant message
    containing a closed, empty think block, and the comment here said the chat
    template renders that as the start of the assistant's turn, so the model
    continues it. It does not. Rendered against this model's real template:

        <|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n<|im_end|>\\n
        <|im_start|>assistant\\n<think>\\n

    — a COMPLETE assistant turn that said nothing, followed by a NEW turn with
    `<think>` opened again. So thinking was never actually off, and the model
    was shown an example of an assistant turn producing no answer. It mimicked
    it: 4 of 22 replies came back empty, all of them after tool calls.

    The template has a first-class switch for this:

        {%- if enable_thinking is defined and enable_thinking is false %}
        {{- '<think>\\n\\n</think>\\n\\n' }}
        {%- else %}
        {{- '<think>\\n' }}

    With `enable_thinking=False` the generation prompt itself carries the
    closed block and the model continues with the ANSWER — which is what the
    prefill was trying to achieve, without the fabricated turn in front of it.

    llama-cpp-python 0.3.23 has no `chat_template_kwargs`, but
    `Jinja2ChatFormatter.__call__` forwards **kwargs straight into the Jinja
    render. So the handler installed here strips the marker message and passes
    the flag. Returns False, leaving the old behaviour in place, if the model
    exposes no template to render — nothing here is allowed to stop a boot.
    """
    try:
        from llama_cpp import llama_chat_format as lcf

        template = (getattr(llama, "metadata", {}) or {}).get(
            "tokenizer.chat_template")
        if not template:
            return False

        def _token(ids: Any, fallback: str) -> str:
            try:
                tid = int(ids)
                return llama.detokenize([tid]).decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                return fallback

        meta = getattr(llama, "metadata", {}) or {}
        eos = _token(meta.get("tokenizer.ggml.eos_token_id"), "<|im_end|>")
        bos = _token(meta.get("tokenizer.ggml.bos_token_id"), "")
        formatter = lcf.Jinja2ChatFormatter(
            template=template, eos_token=eos, bos_token=bos,
            add_generation_prompt=True)

        class _ThinkingAware(lcf.Jinja2ChatFormatter):
            """Reads the marker, drops it, and sets `enable_thinking`."""

            def __init__(self) -> None:  # noqa: D107
                self.__dict__.update(formatter.__dict__)

            def __call__(self, *, messages, **kwargs):  # type: ignore[override]
                msgs = list(messages or [])
                thinking_off = bool(
                    msgs and msgs[-1].get("role") == "assistant"
                    and str(msgs[-1].get("content") or "").strip()
                    == _CLOSED_THINK_PREFILL.strip())
                if thinking_off:
                    msgs = msgs[:-1]
                    kwargs["enable_thinking"] = False
                return super().__call__(messages=msgs, **kwargs)

        llama.chat_handler = lcf.chat_formatter_to_chat_completion_handler(
            _ThinkingAware())
        logger.info("thinking_switch_installed")
        return True
    except Exception as e:  # noqa: BLE001
        # The prefill path still works as it did; it is just slower and
        # occasionally empty. A template change must never fail a boot.
        logger.warning("thinking_switch_unavailable", error=str(e)[:200])
        return False


def _apply_no_think(messages: list[dict[str, Any]], thinking: bool = False) -> list[dict[str, Any]]:
    """Give ordinary chat/tool calls a FAST contract: no hidden reasoning.

    This used to append Qwen3's '/no_think' soft switch. Measured on this model
    (tests/bench_ttft_v3*.py), that barely worked and made things worse in one
    respect — it left ~7s median before the first visible token and RAISED the
    empty-reply rate to 6/18, because the model kept opening a <think> block
    anyway and sometimes spent the whole budget inside it.

    The V3 P2.5 investigation found where the time actually goes:

        model compute TTFT              35 ms
        first VISIBLE token         10,336 ms   <- the whole problem
        i.e. ~10.3 s of hidden reasoning that is generated and then discarded

    Prompt wording does not fix it. Five rewordings were measured and the
    shipped prompt was the best of them; a promising candidate from an earlier
    run failed to reproduce and was variance. The model reasons because the chat
    template tells it to, so the control has to be at the template level.

    Prefilling a closed reasoning block does work, decisively:

        production      simple median 8,169 ms   P90 10,877 ms   worst 20,949 ms   5/18 empty
        prefill         simple median    36 ms   P90     38 ms   worst     39 ms   0/18 empty

    Answer quality is preserved — the replies are still real answers, and the
    empty-generation rate goes to zero because nothing can overflow inside a
    block that was never opened.

    Callers doing hard work (agent decisions, planning, deep mode) pass
    thinking=True and keep full native reasoning. NOVA_LLM_ALLOW_THINKING=1
    forces reasoning on everywhere; NOVA_LLM_FAST_PREFILL=0 falls back to the
    old '/no_think' switch if this ever needs to be disabled in the field.
    """
    if thinking or os.getenv("NOVA_LLM_ALLOW_THINKING", "").strip() == "1":
        return messages

    if os.getenv("NOVA_LLM_FAST_PREFILL", "1").strip().lower() not in {"0", "false", "no", "off"}:
        # A MARKER, not a turn. `_install_thinking_switch` removes it and
        # renders the prompt with the template's `enable_thinking=false`, so
        # the generation prompt itself carries the closed think block.
        #
        # Without that handler this is still passed through as a real assistant
        # message, which is what it used to be everywhere — a complete empty
        # assistant turn followed by a fresh one with `<think>` reopened, which
        # the model copied into empty replies (audit, M10).
        return [*messages, {"role": "assistant", "content": _CLOSED_THINK_PREFILL}]

    msgs = [dict(m) for m in messages]
    for i, m in enumerate(msgs):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            msgs[i]["content"] = (m["content"].rstrip() + "\n\n/no_think").strip()
            return msgs
    return [{"role": "system", "content": "/no_think"}, *msgs]


class _ThinkStreamFilter:
    """Incrementally strips <think>...</think> spans from a token stream.

    Tags can arrive split across multiple token chunks, so this buffers just
    enough text to detect a boundary before releasing it.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, token: str) -> str:
        self._buf += token
        out: list[str] = []
        while True:
            if not self._in_think:
                idx = self._buf.find(_THINK_OPEN)
                if idx == -1:
                    safe_len = len(self._buf) - (len(_THINK_OPEN) - 1)
                    if safe_len > 0:
                        out.append(self._buf[:safe_len])
                        self._buf = self._buf[safe_len:]
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_THINK_OPEN):]
                self._in_think = True
            else:
                idx = self._buf.find(_THINK_CLOSE)
                if idx == -1:
                    keep = len(_THINK_CLOSE) - 1
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:] if keep else ""
                    break
                self._buf = self._buf[idx + len(_THINK_CLOSE):]
                self._in_think = False
        return "".join(out)

    def flush(self) -> str:
        """Call once the stream ends; releases any safe trailing text."""
        if self._in_think:
            # Generation was cut off mid-thought — discard the dangling partial tag/text.
            self._buf = ""
            return ""
        out = self._buf
        self._buf = ""
        return out


def _extract_chat_text(out: Any) -> str:
    """Extract assistant text from llama-cpp-python chat completion responses."""
    try:
        choice0 = (out or {}).get("choices", [{}])[0]
        msg = choice0.get("message") or {}
        # OpenAI-like: {message: {role, content}}
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"].strip()
        # Older shape: {text: ...}
        if isinstance(choice0.get("text"), str):
            return str(choice0.get("text")).strip()
    except Exception:
        pass
    # Best-effort fallback
    return str(out).strip()
