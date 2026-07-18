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
        self._gpu_status = GpuStatus(required=bool(model_path), active=False, status="model_missing" if not model_path else "not_loaded")
        self._init_lock = asyncio.Lock()
        # Rolling token usage for the UI (updated by chat/chat_stream).
        self._usage: dict[str, float | int] = {
            "replies": 0,
            "last_prompt_tokens": 0,
            "last_reply_tokens": 0,
            "avg_reply_tokens": 0.0,
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

    def _load_llama_text_only(self) -> tuple[Any, str]:
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

        combined = "".join(logs) + "\n" + buf.getvalue()
        return llama, combined


    @staticmethod
    def _emit_perf_lines(log_text: str) -> None:
        verbose_perf = (os.getenv("NOVA_LLM_PERF_LOG", "0").strip() or "0").lower() in {"1", "true", "yes", "on"}
        if not verbose_perf:
            return
        if not log_text:
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
        messages = _apply_no_think(messages, thinking=thinking)

        def _run() -> str:
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                # Prefer chat-completions for modern instruct models.
                if hasattr(self._llama, "create_chat_completion"):
                    out = self._llama.create_chat_completion(
                        messages=messages,
                        max_tokens=int(max_tokens),
                        temperature=float(temperature),
                        stop=stop_seq,
                        top_k=40,
                        top_p=0.9,
                        repeat_penalty=1.15,
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
                        repeat_penalty=1.15,
                        stop=stop_seq,
                    )
                    result = str(out.get("choices", [{}])[0].get("text", "")).strip()
            self._emit_perf_lines(sink.getvalue())
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
        messages = _apply_no_think(messages, thinking=thinking)

        loop = asyncio.get_running_loop()

        def _produce(q: asyncio.Queue) -> None:
            sink = io.StringIO()
            reply_tokens = 0
            try:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    stream = self._llama.create_chat_completion(
                        messages=messages,
                        max_tokens=int(max_tokens),
                        temperature=float(temperature),
                        stop=stop_seq,
                        top_k=40,
                        top_p=0.9,
                        repeat_penalty=1.15,
                        stream=True,
                    )
                    for chunk in stream:
                        try:
                            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                            token = delta.get("content")
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
                self._emit_perf_lines(sink.getvalue())

        # Occasionally the reasoning model burns its whole generation on an
        # unclosed <think> block and nothing visible ever comes out. Nothing
        # gets yielded to the caller until real content is seen, so retrying
        # a fresh attempt from scratch is always safe/invisible when an
        # attempt produces zero visible output.
        for attempt in range(3):
            queue: asyncio.Queue[str | None | Exception] = asyncio.Queue(maxsize=512)
            producer = asyncio.create_task(asyncio.to_thread(_produce, queue))
            think_filter = _ThinkStreamFilter()
            produced_any = False
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    visible = think_filter.feed(item)
                    if visible:
                        produced_any = True
                        yield visible
                tail = think_filter.flush()
                if tail:
                    produced_any = True
                    yield tail
            finally:
                await producer
            if produced_any or attempt == 2:
                return
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

        llama = await self._ensure_vision_llama()

        def _run_vision() -> str:
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                out = llama.create_chat_completion(
                    messages=messages,
                    max_tokens=int(max_tokens),
                    temperature=float(temperature),
                    top_k=40,
                    top_p=0.9,
                    repeat_penalty=1.15,
                )
            # Intentionally do not print vision startup/prompt internals to backend logs.
            return _strip_think(_extract_chat_text(out))

        return await asyncio.to_thread(_run_vision)


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


def _apply_no_think(messages: list[dict[str, Any]], thinking: bool = False) -> list[dict[str, Any]]:
    """Append Qwen3's '/no_think' soft switch so ordinary chat/tool calls skip
    the hidden reasoning phase entirely (keeps latency/token budget sane).

    Callers doing hard work (agent decisions, planning, coding) pass
    thinking=True to let the model reason natively — the reasoning is still
    stripped from all output by the think filter, it just happens in the
    background. NOVA_LLM_ALLOW_THINKING=1 forces thinking on everywhere.
    """
    if thinking or os.getenv("NOVA_LLM_ALLOW_THINKING", "").strip() == "1":
        return messages
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
