from __future__ import annotations

# Use the Windows certificate store for TLS verification. Without this,
# antivirus HTTPS inspection (e.g. Norton Web Shield re-signing certificates)
# breaks every outbound Python request: model downloads, weather, maps, Discord.
try:
    import truststore as _truststore

    _truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import contextlib
import datetime as _dt
import io
import os
import shutil
import subprocess
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from time import perf_counter
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import (FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from core.event_bus import BUS, clip as _event_clip
from core.file_extract import extract_excerpt as _extract_attachment_excerpt
from core.file_extract import TEXT_SUFFIXES as _ATTACHMENT_TEXT_SUFFIXES
from core.file_extract import MAX_BYTES as _ATTACHMENT_MAX_BYTES
from core.file_extract import MAX_CHARS as _ATTACHMENT_MAX_CHARS
from core.gpu_telemetry import get_gpu_telemetry
from core.llm_runtime import GPUEnforcementError, LLMRuntime
from core.logging_setup import get_logger, setup_logging
from core.runtime import RuntimeManager
from core.tooling import build_tool_router
from core.voice.chunker import SpeechChunker
from core.voice.echo import EchoFilter
from core.voice.speech_text import has_speakable_content, to_spoken
from memory.unifier import MemoryUnifier
from core.brain import Brain
from plugins.registry import PluginConfigError

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:
    pass


logger = get_logger(__name__)


def _bullet(message: str) -> None:
    print(f"• {message}")


class RuntimeConfig(BaseModel):
    # Paths
    repo_root: Path
    model_dir: Path
    model_path: Path | None
    mmproj_path: Path | None
    projects_dir: Path
    memory_dir: Path
    voice_dir: Path
    # Runtime
    context_tokens: int = 8192
    log_level: str = "INFO"
    version: str = "0.1.0"


_ATTACHMENT_MAX_FILES = 4


def _upload_dir(cfg: "RuntimeConfig") -> Path:
    return (cfg.projects_dir / "_uploads").resolve()


def _safe_upload_path(cfg: "RuntimeConfig", stored_name: str) -> Path | None:
    safe_name = Path(stored_name or "").name
    if not safe_name:
        return None

    upload_dir = _upload_dir(cfg)
    path = (upload_dir / safe_name).resolve()
    if path.parent != upload_dir:
        return None
    if not path.exists() or not path.is_file():
        return None
    return path


def _build_attachment_context(cfg: "RuntimeConfig", attachments: list["UploadedAttachment"]) -> str:
    sections: list[str] = []
    for index, attachment in enumerate(attachments[:_ATTACHMENT_MAX_FILES], start=1):
        stored_name = Path(attachment.path).name
        path = _safe_upload_path(cfg, stored_name)
        label = attachment.name or stored_name or f"attachment-{index}"
        if path is None:
            sections.append(f"[Attachment {index}: {label}] upload is missing or inaccessible.")
            continue

        excerpt, error = _extract_attachment_excerpt(path, attachment.content_type)
        if excerpt:
            sections.append(f"[Attachment {index}: {label}]\n{excerpt}")
        else:
            sections.append(f"[Attachment {index}: {label}] {error or 'could not be read' }.")

    remaining = len(attachments) - _ATTACHMENT_MAX_FILES
    if remaining > 0:
        sections.append(f"[{remaining} more attachment(s) omitted]")
    return "\n\n".join(section.strip() for section in sections if section.strip()).strip()


def _compose_chat_message(cfg: "RuntimeConfig", message: str | None, attachments: list["UploadedAttachment"] | None) -> str:
    clean_message = (message or "").strip()
    attachment_list = list(attachments or [])
    if not attachment_list:
        return clean_message

    attachment_context = _build_attachment_context(cfg, attachment_list)
    if not clean_message:
        clean_message = "Please review the attached file(s) and tell me what they contain."
    if not attachment_context:
        return clean_message

    return f"{clean_message}\n\nAttached file context:\n{attachment_context}"




def _repo_root_from_this_file() -> Path:
    # backend/app.py -> repo_root
    return Path(__file__).resolve().parents[1]


def _pick_model_path(model_dir: Path) -> Path | None:
    # Any *.gguf in model_dir, case-insensitive. Pick most recently modified.
    if not model_dir.exists():
        return None
    candidates: list[Path] = []
    for p in model_dir.iterdir():
        if p.is_file() and p.suffix.lower() == ".gguf":
            # Skip multimodal projector files (mmproj-*.gguf)
            if "mmproj" in p.name.lower():
                continue
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return candidates[0]


def _normalize_model_name(name: str) -> str:
    cleaned = (name or "").lower()
    cleaned = re.sub(r"\.gguf$", "", cleaned)
    cleaned = re.sub(r"^mmproj[-_]?", "", cleaned)
    cleaned = re.sub(r"-(q\d.*|iq\d.*|f16|bf16|fp16|fp32)$", "", cleaned)
    cleaned = re.sub(r"[-_](q\d.*|iq\d.*|f16|bf16|fp16|fp32)$", "", cleaned)
    cleaned = re.sub(r"[-_]+", "-", cleaned).strip("-")
    return cleaned


def _pick_mmproj_path(model_dir: Path, model_path: Path | None) -> Path | None:
    if not model_dir.exists():
        return None

    mmproj_candidates = [
        p for p in model_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".gguf" and "mmproj" in p.name.lower()
    ]
    if not mmproj_candidates:
        return None

    mmproj_candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    if model_path is None:
        return mmproj_candidates[0]

    target = _normalize_model_name(model_path.name)
    for candidate in mmproj_candidates:
        candidate_name = _normalize_model_name(candidate.name)
        if candidate_name == target or candidate_name in target or target in candidate_name:
            return candidate

    return mmproj_candidates[0]


def _load_runtime_config() -> RuntimeConfig:
    raw_repo_root = os.getenv("NOVA_REPO_ROOT", "").strip()
    if raw_repo_root:
        repo_root = Path(raw_repo_root).expanduser().resolve()
        try:
            repo_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If this fails (permissions), fall back to code-adjacent root.
            repo_root = _repo_root_from_this_file()
    else:
        repo_root = _repo_root_from_this_file()

    # Allow explicit full model path override
    raw_model_path = os.getenv("NOVA_MODEL_PATH", "").strip()
    if raw_model_path:
        mp = Path(raw_model_path).expanduser().resolve()
        if not mp.exists() or mp.suffix.lower() != ".gguf":
            raise RuntimeError(f"NOVA_MODEL_PATH invalid or not a .gguf: {mp}")
        model_dir = mp.parent
        model_path = mp
    else:
        raw_model_dir = os.getenv("NOVA_MODEL_DIR", "").strip()
        model_dir = (Path(raw_model_dir).expanduser().resolve() if raw_model_dir else (repo_root / "model"))
        try:
            model_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        model_path = _pick_model_path(model_dir)

    raw_mmproj_path = os.getenv("NOVA_MMPROJ_PATH", "").strip()
    if raw_mmproj_path:
        mmproj_path = Path(raw_mmproj_path).expanduser().resolve()
        if not mmproj_path.exists() or mmproj_path.suffix.lower() != ".gguf":
            raise RuntimeError(f"NOVA_MMPROJ_PATH invalid or not a .gguf: {mmproj_path}")
    else:
        mmproj_path = _pick_mmproj_path(model_dir, model_path)
        if mmproj_path is not None:
            os.environ["NOVA_MMPROJ_PATH"] = str(mmproj_path)

    projects_dir = Path(os.getenv("NOVA_PROJECTS_DIR", str(repo_root / "projects"))).expanduser().resolve()
    memory_dir = Path(os.getenv("NOVA_MEMORY_DIR", str(repo_root / "memory_data"))).expanduser().resolve()
    voice_dir = Path(os.getenv("NOVA_VOICE_DIR", str(repo_root / "voices"))).expanduser().resolve()

    ctx = int(os.getenv("NOVA_CONTEXT_TOKENS", "8192").strip() or "8192")
    log_level = (os.getenv("NOVA_LOG_LEVEL", "INFO").strip() or "INFO").upper()

    return RuntimeConfig(
        repo_root=repo_root,
        model_dir=model_dir,
        model_path=model_path,
        mmproj_path=mmproj_path,
        projects_dir=projects_dir,
        memory_dir=memory_dir,
        voice_dir=voice_dir,
        context_tokens=ctx,
        log_level=log_level,
        version=os.getenv("NOVA_VERSION", "0.1.0").strip() or "0.1.0",
    )



class UploadedAttachment(BaseModel):
    name: str
    path: str
    bytes: int | None = None
    url: str | None = None
    content_type: str | None = None


class ClientLocation(BaseModel):
    lat: float
    lng: float
    accuracy_m: float | None = None


class ChatRequest(BaseModel):
    message: str | None = None
    conversation_id: UUID | None = None
    current_location: ClientLocation | None = None
    attachments: list[UploadedAttachment] = Field(default_factory=list)
    # V3 P5.1. OPAQUE handle from /stt — the ONLY speaker-related thing a client
    # may send. There is deliberately no speaker_name, profile_id or role field:
    # a browser that could assert "Marcus" would defeat the whole namespace
    # separation, so identity is resolved backend-side from this handle alone.
    voice_turn_id: str | None = None
    #: Transport hint that this came from the microphone. It cannot grant
    #: identity — it only lets the backend tell "voice whose handle failed"
    #: (unverified) apart from "typed" (legacy owner).
    input_source: str | None = None



class TtsRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "nova.wav"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "nova.wav"


class SpeakerInfo(BaseModel):
    """Backend-derived speaker metadata (V3 P5). Never contains an embedding."""

    status: str = "unavailable"      # known | unknown | ambiguous | too_short | unavailable
    #: Why there is no identity, when there is none. Actionable and non-biometric:
    #: "empty_transcript" and "disabled" and "no embedding" are different problems.
    reason: str | None = None
    #: Was identification actually attempted? False only when the feature is off.
    attempted: bool = False
    #: The ASSERTED identity — populated only for `known`. Attribution and
    #: personalisation read this and nothing else below it.
    profile_id: str | None = None
    display_name: str | None = None
    similarity: float | None = None
    second_best_similarity: float | None = None
    threshold: float | None = None
    #: ── diagnostics (V3 P5.2 final closure) ─────────────────────────────────
    #:
    #: `SpeakerMatch.for_response()` already emitted these and Pydantic dropped
    #: them on the floor: extra keys are ignored by default, so the model object
    #: was right, the HTTP response was missing fields, and nothing failed
    #: loudly. The browser calibration harness reads them, which is exactly the
    #: class of bug this phase exists to stop — correct in Python, lost across
    #: the serialization boundary.
    #:
    #: All non-biometric. No embedding, no centroid, no enrollment vector, ever.
    threshold_source: str | None = None
    margin: float | None = None
    second_best_profile_id: str | None = None
    second_best_name: str | None = None
    #: Who merely RANKED FIRST — set even when the answer was `unknown`. This is
    #: calibration evidence, NOT identity; see core/speaker/matcher.py.
    top_scored_profile_id: str | None = None
    top_scored_display_name: str | None = None
    model_id: str | None = None
    #: Short-lived handle the client quotes back on /chat so identity stays
    #: backend-derived. Not a session token and grants nothing.
    voice_turn_id: str | None = None


class SttResponse(BaseModel):
    text: str
    duration_ms: int
    sample_rate: int
    empty: bool = False
    #: OPTIONAL and additive: callers that only read `.text` are unaffected.
    speaker: SpeakerInfo | None = None


class ChatStreamRequest(BaseModel):
    # Frontend historically sent `msg`; some callers send `message`.
    msg: str | None = None
    message: str | None = None
    conversation_id: UUID | None = None
    current_location: ClientLocation | None = None
    attachments: list[UploadedAttachment] = Field(default_factory=list)
    #: See ChatRequest — opaque handle only, plus a transport hint.
    voice_turn_id: str | None = None
    input_source: str | None = None
    speak: bool = False
    voice: str = "nova.wav"


app = FastAPI(title="Nova Backend", version="0.1.0")


@app.middleware("http")
async def _bind_request_id(request, call_next):  # noqa: ANN001
    """Bind a request_id to structlog contextvars for correlation."""
    import structlog

    rid = request.headers.get("x-request-id") or str(UUID(bytes=os.urandom(16)))
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        resp = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    resp.headers["x-request-id"] = rid
    return resp


# ── API authentication (Phase 0.3 of docs/ROADMAP.md) ───────────────────────
# Enforced only when NOVA_API_TOKEN is set: every HTTP request must present
# Authorization: Bearer <token> (or ?token= for clients that can't set
# headers, e.g. the WebSocket). Unset = current localhost-only behavior, with
# a boot warning so the gap is never silent. /health stays open so process
# supervisors can probe liveness without credentials.
_AUTH_EXEMPT_PATHS = {"/health"}


def _api_token() -> str:
    return os.getenv("NOVA_API_TOKEN", "").strip()


def _request_token_ok(supplied: str | None) -> bool:
    import hmac

    expected = _api_token()
    if not expected:
        return True
    return bool(supplied) and hmac.compare_digest(supplied.strip(), expected)


@app.middleware("http")
async def _require_api_token(request, call_next):  # noqa: ANN001
    if not _api_token() or request.method == "OPTIONS" or request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
    if not _request_token_ok(supplied):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API token."})
    return await call_next(request)


def _parse_allowed_origins() -> list[str]:
    raw = os.getenv("NOVA_ALLOWED_ORIGINS", "").strip()
    if not raw:
        # Local dev (Vite) + Electron (file:// uses Origin: null)
        return ["http://localhost:5173", "http://127.0.0.1:5173", "null"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or ["null"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers (Phase 0.6): domain endpoints live in backend/routers/ ───────────
from backend.routers import autonomy as _r_autonomy  # noqa: E402
from backend.routers import dev as _r_dev  # noqa: E402
from backend.routers import memory_api as _r_memory  # noqa: E402
from backend.routers import speaker as _r_speaker  # noqa: E402
from backend.routers import web_maps as _r_web_maps  # noqa: E402

app.include_router(_r_dev.router)
app.include_router(_r_autonomy.router)
app.include_router(_r_memory.router)
app.include_router(_r_speaker.router)
app.include_router(_r_web_maps.router)


from backend.state import STATE, _TTS_CACHE_MAX  # Phase 0.6: shared state lives in backend/state.py


def _require_model_present() -> None:
    cfg = STATE.config
    llm = STATE.llm
    if cfg is None or llm is None:
        raise HTTPException(status_code=503, detail="Not ready")
    if cfg.model_path is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No GGUF model found. Download any *.gguf model and place it in: "
                f"{cfg.model_dir}  (or set NOVA_MODEL_PATH to a full .gguf path)."
            ),
        )



def _ffmpeg_executable() -> str:
    """Return an ffmpeg executable path and ensure it's usable.

    On Windows, VS Code/Electron/uvicorn processes sometimes don't inherit the
    user's updated PATH after installing ffmpeg. We try common locations and
    allow an explicit override.
    """

    override = (os.getenv("NOVA_FFMPEG_PATH", "") or os.getenv("FFMPEG_PATH", "")).strip()
    candidates: list[str] = []

    if override:
        candidates.append(override)

    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"))

    candidates.extend(
        [
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        ]
    )

    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.exists() and p.is_file():
            ffmpeg_dir = str(p.parent)
            path = os.environ.get("PATH", "")
            if ffmpeg_dir and ffmpeg_dir not in path.split(os.pathsep):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + path
            return str(p)

    raise RuntimeError(
        "FFmpeg is required to decode audio for STT (webm/ogg/etc) and to read certain reference voices for XTTS. "
        "Install FFmpeg and ensure `ffmpeg` is on PATH, or set NOVA_FFMPEG_PATH to the full ffmpeg.exe path."
    )


def _voice_needs_ffmpeg(voice_path: Path) -> bool:
    # Formats that often require ffmpeg for decoding.
    return voice_path.suffix.lower() in {".mp3", ".m4a", ".ogg", ".webm"}


def _list_voice_files(voice_dir: Path) -> list[Path]:
    exts = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
    try:
        files = [p for p in voice_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    except FileNotFoundError:
        return []

    preferred_order = {
        ".wav": 0,
        ".flac": 1,
        ".m4a": 2,
        ".ogg": 3,
        ".mp3": 4,
    }
    return sorted(files, key=lambda p: (preferred_order.get(p.suffix.lower(), 99), p.name.lower()))


def _resolve_voice_path(cfg: "RuntimeConfig", requested: str | None) -> Path:
    """Resolve a speaker reference audio file under cfg.voice_dir.

    - Prevents path traversal by forcing basename.
    - If requested doesn't exist, falls back to NOVA_DEFAULT_VOICE, then first file in voices dir.
    """

    voice_dir = cfg.voice_dir

    requested_name = (requested or "").strip()
    requested_name = Path(requested_name).name if requested_name else ""

    if requested_name:
        candidate = (voice_dir / requested_name)
        try:
            resolved = candidate.resolve()
            if voice_dir.resolve() not in resolved.parents and resolved != voice_dir.resolve():
                raise HTTPException(status_code=400, detail="Invalid voice path")
        except FileNotFoundError:
            resolved = candidate

        if resolved.exists() and resolved.is_file():
            return resolved

    env_default = os.getenv("NOVA_DEFAULT_VOICE", "").strip()
    if env_default:
        env_name = Path(env_default).name
        env_path = (voice_dir / env_name)
        if env_path.exists() and env_path.is_file():
            return env_path

    voices = _list_voice_files(voice_dir)
    if voices:
        return voices[0]

    raise HTTPException(status_code=404, detail="No voices found in voice_dir")




def _silent_warn_call(fn, *args, **kwargs):
    sink = io.StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return fn(*args, **kwargs)

def _load_tts_model():
    """Legacy in-process XTTS load (NOVA_TTS_ISOLATED=0 only).

    Kept for debugging the model itself without the process boundary in the
    way. It is NOT the normal path: in-process CUDA XTTS beside llama.cpp is
    what produced the illegal-memory-access aborts documented in core/gpu.py,
    so this path is only safe on CPU. Device policy is shared with the isolated
    worker so the two can never disagree about what "auto" means.
    """
    from services.xtts_engine import load_engine, resolve_device

    device, reason = resolve_device()
    if device != "cpu":
        _bullet(f"WARNING: in-process XTTS on {device} shares a CUDA context with llama.cpp "
                "and can abort the backend. Set NOVA_TTS_ISOLATED=1 (the default).")
    tts, sample_rate = load_engine(device, progress=_bullet)
    STATE.tts_sample_rate = sample_rate
    STATE.tts_device_reason = reason
    return tts, device


def _tts_isolated_enabled() -> bool:
    return os.getenv("NOVA_TTS_ISOLATED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _tts_engine():
    """The isolated XTTS client, created on first use.

    Creating it is cheap and starts nothing — the child process spawns on the
    first `ensure_started()`. That keeps `import backend.app` free of any CUDA
    or model side effects, which the test suite depends on.
    """
    if STATE.tts_engine is None:
        from services.tts_client import IsolatedTtsEngine

        def _emit(event: str, payload: dict) -> None:
            BUS.publish(event, payload)
            if event == "tts.loading" and payload.get("message"):
                _bullet(str(payload["message"]))
            elif event == "tts.worker_died":
                _bullet("XTTS worker died — voice degraded, text chat unaffected")
            elif event == "tts.worker_restarting":
                _bullet(f"Restarting XTTS worker (attempt {payload.get('attempt')})")

        STATE.tts_engine = IsolatedTtsEngine(
            cfg={
                "device": os.getenv("NOVA_TTS_DEVICE", "auto"),
                "allow_cpu_fallback": os.getenv("NOVA_TTS_ALLOW_CPU_FALLBACK", "0").strip().lower()
                in {"1", "true", "yes", "on"},
            },
            max_backlog=int(os.getenv("NOVA_TTS_MAX_BACKLOG", "32").strip() or "32"),
            max_restarts=int(os.getenv("NOVA_TTS_MAX_RESTARTS", "3").strip() or "3"),
            on_event=_emit,
        )
    return STATE.tts_engine


def _load_mcp_configs():
    """MCP servers from NOVA_MCP_SERVERS. Absent config means no MCP at all."""
    try:
        from core.mcp.manager import load_server_configs

        return load_server_configs()
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp_config_load_failed", error=str(e)[:200])
        return []


def tts_status() -> dict[str, Any]:
    """What the voice subsystem is ACTUALLY doing right now. Never inferred."""
    if _tts_isolated_enabled():
        engine = STATE.tts_engine
        if engine is None:
            return {
                "mode": "isolated",
                "state": "stopped",
                "configured_device": os.getenv("NOVA_TTS_DEVICE", "auto"),
                "actual_device": None,
                "detail": "worker not started yet (starts on first speech)",
            }
        return {"mode": "isolated", **engine.status()}
    return {
        "mode": "in_process",
        "state": "ready" if STATE.tts is not None else "stopped",
        "configured_device": os.getenv("NOVA_TTS_DEVICE", "auto"),
        "actual_device": STATE.tts_device,
        "device_reason": STATE.tts_device_reason,
        "detail": "NOVA_TTS_ISOLATED=0: legacy in-process loader, CPU-safe only",
    }


async def _ensure_tts_loaded(reason: str = "request") -> Any:
    if _tts_isolated_enabled():
        engine = _tts_engine()
        if engine.state != "ready":
            _bullet(f"Starting XTTS worker... ({reason})")
            BUS.publish("tts.loading", {"reason": reason})
        if not await engine.ensure_started():
            raise RuntimeError(engine.last_error or "XTTS worker unavailable")
        if STATE.tts_device != engine.device:
            STATE.tts_device = engine.device
            STATE.tts_device_reason = engine.device_reason
            STATE.tts_sample_rate = engine.sample_rate
            _bullet(f"XTTS ready on {engine.device} (isolated process, pid {engine.pid})")
        return engine

    if STATE.tts is not None:
        return STATE.tts

    if STATE.tts_load_task is None:
        async def _load() -> Any:
            started = perf_counter()
            _bullet(f"Loading XTTS... ({reason})")
            BUS.publish("tts.loading", {"reason": reason})
            tts, device = await asyncio.to_thread(_load_tts_model)
            STATE.tts = tts
            STATE.tts_device = device
            _bullet("XTTS loaded")
            BUS.publish("tts.loaded", {"device": device})
            return tts

        STATE.tts_load_task = asyncio.create_task(_load())

    try:
        return await STATE.tts_load_task
    except Exception:
        STATE.tts_load_task = None
        raise


def _voice_cache_key(voice_path: Path) -> str:
    stat = voice_path.stat()
    return f"{voice_path.resolve()}::{stat.st_mtime_ns}::{stat.st_size}"


async def _speaker_wav_for_voice(voice_path: Path) -> str:
    if not _voice_needs_ffmpeg(voice_path):
        return str(voice_path)

    cache_key = _voice_cache_key(voice_path)
    cached = STATE.tts_voice_cache.get(cache_key)
    if cached and Path(cached).exists():
        return cached

    existing_task = STATE.tts_voice_tasks.get(cache_key)
    if existing_task is not None:
        return await existing_task

    async def _convert() -> str:
        ffmpeg = _ffmpeg_executable()

        def _run_convert() -> str:
            cache_dir = STATE.tts_voice_cache_dir
            if cache_dir is None:
                cache_dir = Path(tempfile.mkdtemp(prefix="nova-tts-voices-"))
                STATE.tts_voice_cache_dir = cache_dir

            out_path = cache_dir / f"{uuid4().hex}.wav"
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(voice_path),
                "-ac",
                "1",
                "-ar",
                "22050",
                "-f",
                "wav",
                str(out_path),
            ]
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            return str(out_path)

        started = perf_counter()
        converted = await asyncio.to_thread(_run_convert)
        logger.info(
            "tts_voice_prepared",
            voice=voice_path.name,
            source_suffix=voice_path.suffix.lower(),
            seconds=round(perf_counter() - started, 3),
        )
        STATE.tts_voice_cache[cache_key] = converted
        return converted

    task = asyncio.create_task(_convert())
    STATE.tts_voice_tasks[cache_key] = task
    try:
        return await task
    finally:
        if STATE.tts_voice_tasks.get(cache_key) is task:
            STATE.tts_voice_tasks.pop(cache_key, None)


async def _prewarm_tts() -> None:
    if (os.getenv("NOVA_TTS_PREWARM", "0").strip() or "0").lower() in {"0", "false", "no", "off"}:
        _bullet("XTTS prewarm skipped")
        return

    cfg = STATE.config
    if cfg is None:
        return

    try:
        voice_path = _resolve_voice_path(cfg, os.getenv("NOVA_DEFAULT_VOICE", "") or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("tts_prewarm_skipped", error=str(e))
        return

    try:
        started = perf_counter()
        await _ensure_tts_loaded("prewarm")
        await _speaker_wav_for_voice(voice_path)
        warmup_text = (
            os.getenv("NOVA_TTS_WARMUP_TEXT", "Hello there. I am ready to help.").strip()
            or "Hello there. I am ready to help."
        )
        await _tts_bytes(warmup_text, voice_path=voice_path, reason="prewarm")
        _bullet(f"XTTS prewarm complete: {voice_path.name}")
    except Exception as e:  # noqa: BLE001
        logger.warning("tts_prewarm_failed", error=str(e))


def _normalize_tts_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _should_cache_tts_phrase(text: str, reason: str) -> bool:
    if reason not in {"reply", "system", "prewarm"}:
        return False
    if not text or len(text) > 160:
        return False
    return text.count("\n") <= 1


# WS-H: subtle mood-aware pacing. XTTS 0.22.0 has no working emotion/style
# parameter (Coqui-Studio-only, discontinued), but `speed` is real synthesis-
# level control — so the achievable slice of "prosody follows mood" is a small
# pace shift keyed off the mood signal Nova already tracks. Deliberately
# narrow multipliers: a hint, not a performance.
_MOOD_TTS_SPEED = {
    "sad": 0.92, "stressed": 0.93, "tired": 0.93, "anxious": 0.94, "frustrated": 0.96,
    "happy": 1.05, "excited": 1.06, "content": 1.0,
}


async def _mood_speed_multiplier() -> float:
    """Speed multiplier from the most recent mood reading, if it's fresh
    (today/yesterday). 1.0 whenever unavailable, stale, or disabled — never
    guesses a mood that wasn't actually detected."""
    if os.getenv("NOVA_TTS_MOOD_PACING", "1").strip().lower() in {"0", "false", "no", "off"}:
        return 1.0
    memory = STATE.memory
    if memory is None:
        return 1.0
    try:
        rows = await memory.get_facts(entity="mood", limit=1, newest_first=True)
        if not rows:
            return 1.0
        row = rows[0]
        # Mood facts are singleton-per-day with attribute = "YYYY-MM-DD".
        reading_day = _dt.date.fromisoformat(str(row.attribute))
        if (_dt.date.today() - reading_day).days > 1:
            return 1.0
        return _MOOD_TTS_SPEED.get(str(row.value).strip().lower(), 1.0)
    except Exception:
        return 1.0


async def _tts_bytes(text: str, voice_path: Path, reason: str = "reply",
                     turn_id: str = "") -> bytes:
    """Synthesise one utterance.

    Deliberately does NOT take core.gpu.GPU_SEM. That was tried and deadlocks:
    the reply stream holds the permit for the whole generation (`async with
    chat_model.semaphore` wraps the token loop) while the SSE handler drains
    TTS clips mid-stream, so the synthesis it waits on can never get the permit
    (measured: a 195 s hang, then access violations on every later turn).
    Sentence-streamed TTS overlaps generation *by design* and so cannot be
    serialised against it.

    The resolution is not a permit but a process: XTTS runs in its own child
    process with its own CUDA context (services/tts_worker.py), so it shares the
    card with llama.cpp without sharing the context that made them corrupt each
    other. See core/gpu.py for the original evidence.
    """
    text = _normalize_tts_text(text)
    if not text:
        raise RuntimeError("tts_text_empty")

    mood_mult = await _mood_speed_multiplier() if reason == "reply" else 1.0

    cache_key = f"{voice_path.name}::{reason}::{mood_mult}::{text}"
    if _should_cache_tts_phrase(text, reason):
        cached = STATE.tts_phrase_cache.get(cache_key)
        if cached is not None:
            return cached

    engine = await _ensure_tts_loaded(reason)
    speaker_wav = await _speaker_wav_for_voice(voice_path)

    try:
        tts_speed = float(os.getenv("NOVA_TTS_SPEED", "1.0") or "1.0")
    except ValueError:
        tts_speed = 1.0
    # Layer the mood hint on the configured base, clamped to a natural range.
    tts_speed = max(0.8, min(1.25, tts_speed * mood_mult))

    if _tts_isolated_enabled():
        audio = await engine.synthesize(
            text, speaker_wav=speaker_wav, turn_id=turn_id, language="en", speed=tts_speed
        )
    else:
        def _run() -> bytes:
            from services.xtts_engine import synthesize

            sr = STATE.tts_sample_rate or int(
                getattr(STATE.tts.synthesizer, "output_sample_rate", 24000)
            )
            return synthesize(STATE.tts, sr, text=text, speaker_wav=speaker_wav,
                              language="en", speed=tts_speed)

        audio = await asyncio.to_thread(_run)

    if _should_cache_tts_phrase(text, reason):
        STATE.tts_phrase_cache[cache_key] = audio
    return audio


def _chunk_text(s: str, chunk_size: int = 18) -> list[str]:
    s = s or ""
    if chunk_size <= 1:
        return list(s)
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


#: Terms Whisper has no reason to know, but Marcus says constantly. Fed to the
#: decoder as an initial prompt rather than corrected afterwards — biasing the
#: decode is the mechanism that actually exists for this, and a post-hoc
#: search/replace table would happily "fix" a word he really said.
_STT_VOCABULARY = (
    "Nova", "Jellyfin", "StreamNChill", "llama.cpp", "Qwen", "XTTS", "CUDA",
    "Chroma", "SQLite", "Raspberry Pi", "Orange Pi", "RTX 5080", "RTX 5090",
    "faster-whisper", "GGUF", "VRAM", "Moonraker", "OctoPrint", "build123d",
)


def _stt_vocabulary_prompt() -> str | None:
    """Decoder bias prompt. NOVA_STT_VOCABULARY appends comma-separated extras."""
    extra = [w.strip() for w in os.getenv("NOVA_STT_VOCABULARY", "").split(",") if w.strip()]
    if os.getenv("NOVA_STT_BIAS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    words = list(_STT_VOCABULARY) + extra
    return "Vocabulary: " + ", ".join(words) + "."


def _speaker_service():
    """Lazily create the process-wide speaker service (V3 P5).

    Held on STATE beside the other optional subsystems. Creating it is cheap —
    the 89 MB model is loaded lazily on first use, so a Nova that never enables
    speaker ID never pays for it.
    """
    if getattr(STATE, "speaker", None) is None:
        try:
            from core.speaker.service import SpeakerService
            cfg = STATE.config
            db = (cfg.memory_dir if cfg else Path("memory_data")) / "sqlite" / "nova.sqlite3"
            STATE.speaker = SpeakerService(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("speaker_service_unavailable", error=str(e)[:200])
            return None
    return STATE.speaker


async def _identify_speaker(pcm, sample_rate: int, *,
                            skip_reason: str | None = None) -> "SpeakerInfo":
    """Classify the speaker of an already-decoded utterance.

    `skip_reason` short-circuits the model while still producing a real,
    attempted voice-turn outcome (V3 P5.1a). It exists for the case where the
    ASR layer already decided there is no utterance: asking ECAPA who spoke a
    silence Whisper rejected wastes 40 ms to answer a question nobody asked, and
    a buffer can carry enough energy to pass `command_quality()` while Whisper
    returns nothing at all.

    Wrapped so that NOTHING here can fail the request. Whisper has already
    succeeded by this point; returning HTTP 500 because an optional biometric
    model misbehaved would turn enrichment into an outage.
    """
    from core.speaker.backend import MODEL_ID, enabled as speaker_enabled
    from core.speaker.matcher import STATUS_UNAVAILABLE, SpeakerMatch
    from core.speaker.voice_turns import VOICE_TURNS

    def _unverified(reason: str) -> "SpeakerInfo":
        """An ENABLED subsystem failure is still a voice turn (V3 P5.1b).

        The distinction this protects is the whole point of `attempted`:

            disabled     nobody asked. Legacy Nova, typed semantics.
            unavailable  the question WAS asked and could not be answered.
                         Unverified voice; personal memory must not be written.

        Before P5.1b both produced `attempted=False` and no handle, so a
        subsystem that failed erased the evidence it was ever supposed to run —
        and "no speaker metadata" is exactly what would later read as
        typed-Marcus. A failure must never be able to launder itself into an
        identity by disappearing.

        The handle comes from the process-wide registry rather than the service,
        so it exists even when the service does not.
        """
        if not speaker_enabled():
            return SpeakerInfo(status="unavailable", reason="disabled",
                               attempted=False, model_id=MODEL_ID)
        match = SpeakerMatch(status=STATUS_UNAVAILABLE, reason=reason, attempted=True)
        return SpeakerInfo(status=STATUS_UNAVAILABLE, reason=reason, attempted=True,
                           model_id=MODEL_ID,
                           voice_turn_id=VOICE_TURNS.issue(match))

    try:
        svc = _speaker_service()
        if svc is None:
            return _unverified("service unavailable")
        if skip_reason is not None:
            if not speaker_enabled():
                return SpeakerInfo(status="unavailable", reason="disabled",
                                   model_id=MODEL_ID)
            # Attempted, deliberately unresolved. Never `known`, no profile, no
            # similarity — but still a voice turn, with a handle, so downstream
            # cannot mistake the absence of identity for typed Marcus.
            match = SpeakerMatch(status=STATUS_UNAVAILABLE, reason=skip_reason,
                                 attempted=True)
        else:
            match = await svc.identify(pcm, sample_rate)
        info = SpeakerInfo(**match.for_response(model_id=MODEL_ID))
        # A handle is minted for every classified turn, including unknown ones:
        # "an unrecognised voice said this" is itself identity the frontend must
        # not be able to forge or upgrade.
        info.voice_turn_id = svc.issue_voice_turn(match)
        BUS.publish("speaker.identified", {"status": info.status,
                                           "name": info.display_name})
        return info
    except Exception as e:  # noqa: BLE001
        logger.warning("speaker_identify_error", error=str(e)[:200])
        # Same rule for an unexpected fault anywhere above: enabled and failed
        # is an unverified voice turn, never an absence of one. `_unverified`
        # is itself defensive — if even minting a handle fails, the status and
        # `attempted` flag still go out correctly.
        try:
            return _unverified("identify error")
        except Exception:  # noqa: BLE001
            return SpeakerInfo(status="unavailable", reason="identify error",
                               attempted=speaker_enabled(), model_id=MODEL_ID)


async def _stt_transcribe(upload: UploadFile, *, identify_speaker: bool = False) -> SttResponse:
    """Transcribe an uploaded audio file using Whisper via transformers.

    Returns richer metadata so the frontend can reason about empty/short captures.
    """

    ffmpeg = _ffmpeg_executable()

    def _load_stt_engine():
        """Prefer faster-whisper (3-5x faster). Fall back to transformers whisper.

        Returns ("faster", model) or ("transformers", pipeline).
        """
        model_size = os.getenv("NOVA_STT_MODEL_SIZE", "base").strip() or "base"
        try:
            import torch  # type: ignore
            from faster_whisper import WhisperModel  # type: ignore

            # ctranslate2 CUDA support on very new GPU architectures can lag;
            # try GPU first, then CPU int8 (still fast), before giving up.
            last_err: Exception | None = None
            attempts = (
                [("cuda", "float16"), ("cpu", "int8")]
                if torch.cuda.is_available()
                else [("cpu", "int8")]
            )
            for device, compute_type in attempts:
                try:
                    model = _silent_warn_call(WhisperModel, model_size, device=device, compute_type=compute_type)
                    # Sanity-run on a beep of silence so device failures surface now.
                    import numpy as np

                    list(model.transcribe(np.zeros(1600, dtype=np.float32), language="en")[0])
                    logger.info("stt_engine_loaded", engine="faster-whisper", device=device, model=model_size)
                    return ("faster", model)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    continue
            raise RuntimeError(f"faster-whisper unusable: {last_err}")
        except Exception as e:  # noqa: BLE001
            logger.warning("stt_faster_whisper_unavailable_falling_back", error=str(e)[:200])

        import torch  # type: ignore
        from transformers import pipeline  # type: ignore

        model_id = os.getenv("NOVA_STT_MODEL", "openai/whisper-base")
        device = 0 if torch.cuda.is_available() else -1
        torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        pipe = _silent_warn_call(
            pipeline,
            "automatic-speech-recognition",
            model=model_id,
            device=device,
            torch_dtype=torch_dtype,
            chunk_length_s=20,
            return_timestamps=False,
            generate_kwargs={"language": "en"},
        )
        logger.info("stt_engine_loaded", engine="transformers", model=model_id)
        return ("transformers", pipe)

    if STATE.stt is None:
        BUS.publish("stt.loading", {"model": os.getenv("NOVA_STT_MODEL_SIZE", "base")})
        STATE.stt = await asyncio.to_thread(_load_stt_engine)
        BUS.publish("stt.loaded", {"engine": STATE.stt[0]})

    suffix = Path(upload.filename or "audio").suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="nova-stt-") as td:
        in_path = Path(td) / f"in{suffix}"
        out_path = Path(td) / "out.wav"
        data = await upload.read()
        await asyncio.to_thread(in_path.write_bytes, data)

        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(in_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(out_path),
        ]
        try:
            # ffmpeg ran DIRECTLY on the event loop here, for up to 60s. While a
            # voice clip decoded, nothing else in Nova could make progress —
            # no chat, no token streaming, no WS events, no workers. The decode
            # itself is fine; doing it on the loop was not.
            await asyncio.to_thread(
                lambda: subprocess.run(
                    cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
                )
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"ffmpeg_decode_failed: {e}") from e

        def _run_asr() -> SttResponse:
            import numpy as np
            import soundfile as sf  # type: ignore

            audio, sr = sf.read(str(out_path), dtype="float32")
            if getattr(audio, "ndim", 1) > 1:
                audio = np.mean(audio, axis=1)
            audio = np.asarray(audio, dtype=np.float32)
            duration_ms = int(round((len(audio) / max(int(sr), 1)) * 1000)) if len(audio) else 0

            engine, model = STATE.stt
            if engine == "faster":
                # Measured in tests/bench_stt_v3.py on this machine (base model,
                # CUDA float16, 7 probe utterances):
                #
                #   as shipped                          82ms mean
                #   + condition_on_previous_text=False  58ms
                #   + without_timestamps=True           50ms
                #
                # condition_on_previous_text is for long-form continuity across
                # chunks; Nova transcribes ONE utterance per request, so it buys
                # nothing here and is a known hallucination source. Word
                # timestamps are computed and then discarded — Nova only reads
                # `.text`. vad_filter is KEPT: dropping it measured fastest of
                # all (42ms) but it is what trims leading/trailing silence, and
                # trading real-audio robustness for 8ms is a bad deal.
                #
                # initial_prompt biases decoding toward vocabulary Marcus
                # actually uses. Measured on synthetic speech, the unbiased model
                # produced "Lama.cpp", "QN" and "XCTs"; biased it produced
                # "llama.cpp", "Qwen" and "XTTS". This is proper decoder
                # conditioning, not a post-hoc search/replace table.
                segments, _info = model.transcribe(
                    audio,
                    language="en",
                    beam_size=1,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    without_timestamps=True,
                    initial_prompt=_stt_vocabulary_prompt(),
                )
                text = " ".join(seg.text.strip() for seg in segments).strip()
            else:
                result = _silent_warn_call(model, {"array": audio, "sampling_rate": int(sr)})
                if isinstance(result, dict):
                    text = str(result.get("text") or "").strip()
                else:
                    text = str(result or "").strip()

            return SttResponse(
                text=text,
                duration_ms=duration_ms,
                sample_rate=int(sr),
                empty=not bool(text),
            ), audio, int(sr)

        BUS.publish("stt.transcribing", {})
        # The decoded mono float32 PCM comes back with the transcript so speaker
        # embedding can reuse it. ffmpeg runs ONCE per request: a second decode
        # (or a second upload) for speaker ID would double the most expensive
        # part of this path to recompute bytes we already have.
        result, pcm, pcm_sr = await asyncio.to_thread(_run_asr)
        BUS.publish("stt.transcript_final", {"chars": len(result.text), "empty": result.empty})

        # Speaker identification is OPT-IN per request. The fallback wake loop
        # calls /stt continuously on short chunks while waiting for "Hey Nova";
        # embedding every one of those would burn CPU forever to identify the
        # speaker of a word Nova is going to discard.
        if identify_speaker:
            # An empty transcript means Whisper found no utterance to send to
            # chat. Classifying it anyway would let background noise that
            # happens to clear the energy gate come back as a KNOWN speaker for
            # a turn that has no words in it. Zero embedding calls, and the turn
            # still carries its unverified voice state.
            result.speaker = await _identify_speaker(
                pcm, pcm_sr,
                skip_reason="empty_transcript" if result.empty else None)
        return result

@app.on_event("startup")
async def _startup() -> None:
    cfg = _load_runtime_config()
    setup_logging(cfg.log_level)

    BUS.bind_loop()
    BUS.publish("nova.startup.begin", {"version": cfg.version})

    STATE.config = cfg
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    cfg.voice_dir.mkdir(parents=True, exist_ok=True)

    memory = MemoryUnifier(cfg.memory_dir)
    await memory.initialize()

    # Avoid replaying stale background jobs from previous runs unless explicitly enabled.
    resume_bg = (os.getenv("NOVA_RESUME_BACKGROUND_TASKS", "0").strip() or "0").lower() in {"1", "true", "yes", "on"}
    if not resume_bg:
        try:
            cancelled = await memory.cancel_pending_background_work()
            if int(cancelled.get("autonomy_tasks", 0)) or int(cancelled.get("goal_tasks", 0)):
                _bullet(
                    "Cleared stale background tasks "
                    f"(autonomy={int(cancelled.get('autonomy_tasks', 0))}, goals={int(cancelled.get('goal_tasks', 0))})"
                )
        except Exception:
            pass

    llm = LLMRuntime(model_path=cfg.model_path, context_tokens=cfg.context_tokens)
    # If a model exists, enforce GPU offload at startup.
    if cfg.model_path is not None:
        try:
            BUS.publish("model.loading", {"model": cfg.model_path.name, "context_tokens": cfg.context_tokens})
            # NOTE: no "Model loaded" print here — llm.initialize() already logs
            # `llm_loaded`, which logging_setup renders as that exact line. This
            # printed the same thing a second time from the *config* values; the
            # logger reports what actually loaded, so it's the one to keep.
            await llm.initialize()
            BUS.publish("model.loaded", {"model": cfg.model_path.name, "context_tokens": cfg.context_tokens})
            BUS.publish("model.gpu_confirmed", {"status": llm.gpu_status.status})
            if cfg.mmproj_path is not None:
                print(f"• Vision model loaded: {cfg.mmproj_path.name}")
            print("• Startup complete: gpu_offload_confirmed")
        except GPUEnforcementError as e:
            logger.error("gpu_enforcement_failed", error=str(e))
            BUS.publish("model.error", {"error": _event_clip(e, 300)})
            raise

    from plugins import init as _plugins_init  # noqa: F401

    STATE.memory = memory
    STATE.llm = llm

    router = build_tool_router(repo_root=cfg.repo_root, projects_dir=cfg.projects_dir, memory=memory)
    runtime = RuntimeManager(
        repo_root=cfg.repo_root,
        projects_dir=cfg.projects_dir,
        memory=memory,
        llm=llm,
        router=router,
        memory_dir=cfg.memory_dir,
    )
    brain = Brain(repo_root=cfg.repo_root, projects_dir=cfg.projects_dir, memory=memory, llm=llm, runtime=runtime, memory_dir=cfg.memory_dir)

    STATE.runtime = runtime
    STATE.brain = brain
    brain.start()

    # ── MCP capability servers (V3 P3) ──────────────────────────────────────
    # Started in the BACKGROUND: an MCP server is somebody else's process, and
    # a slow or wedged one must not hold up Nova's boot. Capabilities appear in
    # the registry when discovery finishes; until then Nova simply has fewer
    # tools, which every layer already handles.
    STATE.mcp = None
    _mcp_configs = _load_mcp_configs()
    if _mcp_configs:
        async def _start_mcp() -> None:
            from core.mcp.manager import McpManager

            mgr = McpManager(
                permission_broker=runtime.permission_broker,
                artifact_store=getattr(runtime, "_artifacts", None),
            )
            ok = 0
            for scfg in _mcp_configs:
                if await mgr.add_server(scfg):
                    ok += 1
            if ok:
                # Registering on the ordinary ToolRouter is what gives MCP calls
                # Nova's timeout, retry, failure taxonomy and audit for free.
                n = mgr.register_with_router(runtime.router)
                _bullet(f"MCP ready: {ok}/{len(_mcp_configs)} servers, {n} capabilities")
            else:
                _bullet(f"MCP: no servers connected ({len(_mcp_configs)} configured)")
            STATE.mcp = mgr

        STATE.mcp_task = asyncio.create_task(_start_mcp())

    if (os.getenv("NOVA_TTS_PREWARM", "0").strip() or "0").lower() not in {"0", "false", "no", "off"}:
        # Load + warm XTTS in the background so Nova can speak immediately.
        STATE.tts_prewarm_task = asyncio.create_task(_prewarm_tts())
        _bullet("XTTS prewarm scheduled")

    # Phase 0.4: surface config typos/parse errors once, at boot, where
    # they're actually visible — a misspelled NOVA_* var is otherwise
    # silently ignored forever.
    try:
        from core.settings import log_environment_validation

        log_environment_validation()
    except Exception as e:  # noqa: BLE001
        logger.debug("config_validation_failed", error=str(e)[:200])

    if not _api_token():
        logger.warning(
            "api_auth_disabled",
            note="NOVA_API_TOKEN is not set — every endpoint is open to anything that can reach this port. "
                 "Fine for localhost-only use; set a token before any 24/7 or remote exposure.",
        )

    BUS.publish(
        "nova.startup.complete",
        {
            "model": cfg.model_path.name if cfg.model_path else None,
            "vision": bool(cfg.mmproj_path),
            "gpu": llm.gpu_status.status,
        },
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if STATE.tts_prewarm_task is not None:
        STATE.tts_prewarm_task.cancel()
        try:
            await STATE.tts_prewarm_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if STATE.tts_engine is not None:
        # Must happen before the loop closes: the worker is a real child
        # process, and leaving it running orphans a CUDA context holding VRAM.
        try:
            await STATE.tts_engine.stop()
        except Exception:
            pass
        STATE.tts_engine = None
    if STATE.brain is not None:
        try:
            await STATE.brain.stop()
        except Exception:
            pass
    if STATE.tts_voice_cache_dir is not None:
        await asyncio.to_thread(shutil.rmtree, STATE.tts_voice_cache_dir, ignore_errors=True)


@app.get("/health")
async def health() -> dict:
    cfg = STATE.config
    llm = STATE.llm
    if cfg is None or llm is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {
        "version": cfg.version,
        "gpu": llm.gpu_status.__dict__,
        "tts": {
            **tts_status(),
            "voice_dir": str(cfg.voice_dir),
            "default_voice": os.getenv("NOVA_DEFAULT_VOICE", "").strip() or None,
        },
        "vision": llm.vision_status,
        "model": str(cfg.model_path) if cfg.model_path else None,
        "mmproj": str(cfg.mmproj_path) if cfg.mmproj_path else None,
        "repo_root": str(cfg.repo_root),
        "model_dir": str(cfg.model_dir),
    }


def _plugin_config_status() -> dict[str, dict[str, Any]]:
    """Which optional integrations are configured (never exposes values)."""

    def _set(*keys: str) -> bool:
        return all(bool(os.getenv(k, "").strip()) for k in keys)

    return {
        "weather": {"configured": _set("OPENWEATHER_API_KEY"), "requires": ["OPENWEATHER_API_KEY"]},
        "google_maps": {"configured": _set("GOOGLE_MAPS_API_KEY"), "requires": ["GOOGLE_MAPS_API_KEY"]},
        "discord": {
            "configured": _set("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"),
            "requires": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
        },
        "web_search": {"configured": True, "requires": []},
        "google": {
            "configured": (Path(__file__).resolve().parent.parent / "credentials" / "google_token.json").exists(),
            "requires": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                         "one-time setup: python tools/google_oauth_setup.py"],
        },
    }


@app.get("/status")
async def status() -> dict:
    """Full system status for the UI: model, GPU telemetry, subsystems, integrations."""
    cfg = STATE.config
    llm = STATE.llm
    if cfg is None or llm is None:
        raise HTTPException(status_code=503, detail="Not ready")

    gpu = await get_gpu_telemetry()
    tools = STATE.runtime.router.list_tools() if STATE.runtime is not None else []

    return {
        "version": cfg.version,
        "model": {
            "name": cfg.model_path.name if cfg.model_path else None,
            "loaded": llm.model_loaded,
            "context_tokens": cfg.context_tokens,
            "enforcement": llm.gpu_status.__dict__,
            "usage": llm.usage_stats,
        },
        "gpu": gpu.to_dict(),
        "vision": llm.vision_status,
        "tts": tts_status(),
        "stt": {
            "loaded": STATE.stt is not None,
            "engine": STATE.stt[0] if STATE.stt is not None else None,
            "model": os.getenv("NOVA_STT_MODEL_SIZE", "base"),
        },
        "integrations": _plugin_config_status(),
        # Chroma writes are best-effort so a broken index can never break
        # memory — which also meant a degraded index was invisible after one
        # log line. `degraded: true` here says recall is impaired even though
        # every fact was still saved to SQLite.
        "semantic_index": (STATE.memory.semantic_index_health() if STATE.memory is not None else {}),
        "tools": tools,
        "models": (STATE.runtime.models.describe() if STATE.runtime is not None else {}),
        # Which roles are remote vs local, plus cloud state (never the API key).
        "cloud": (STATE.runtime.cloud.status() if STATE.runtime is not None else {"enabled": False}),
        "dev_mode": (os.getenv("NOVA_DEV_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}),
    }


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Structured event stream powering the UI's live activity states."""
    # HTTP middleware doesn't cover WebSockets — enforce the API token here
    # via query param (browsers can't set WS headers). 4401 = auth failure.
    if not _request_token_ok(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    await ws.accept()
    queue = BUS.subscribe()
    try:
        # Replay recent events so a reconnecting UI can catch up.
        for event in BUS.recent(50):
            await ws.send_json(event.to_dict())
        while True:
            event = await queue.get()
            await ws.send_json(event.to_dict())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        BUS.unsubscribe(queue)



async def _resolve_turn_identity(voice_turn_id: str | None,
                                 input_source: str | None) -> "TurnIdentity":
    """Turn a client's opaque handle into backend-derived identity (V3 P5.1).

    Every failure resolves to an UNVERIFIED voice turn, never to typed/Marcus.
    Missing, invented, expired and already-redeemed handles are the same
    outcome, deliberately: a client cannot improve its identity by supplying a
    worse handle.

    The three states that must stay distinguishable:

        typed                     legacy owner semantics
        voice, speaker ID off     legacy owner semantics (nobody asked)
        voice, identity failed    UNVERIFIED — no personal-memory write

    The last one is never inferred from absence. `input_source="voice"` is a
    transport hint that grants nothing; it only prevents a failed voice turn
    from being mistaken for a typed one.
    """
    from core.speaker.backend import enabled as speaker_enabled
    from core.turn_identity import SOURCE_VOICE, TurnIdentity
    from core.speaker.voice_turns import VOICE_TURNS

    is_voice = (input_source or "").strip().lower() == SOURCE_VOICE or bool(voice_turn_id)
    if not is_voice:
        return TurnIdentity.typed()
    if not speaker_enabled():
        # Legacy Nova: voice, but no speaker question was ever asked.
        return TurnIdentity.voice_legacy()

    match = VOICE_TURNS.redeem(voice_turn_id)
    if match is None:
        return TurnIdentity.voice_unverified(
            "no handle" if not voice_turn_id else "handle invalid, expired or already used")

    # `stored_role` comes from the durable profile, never from the request.
    profile = None
    try:
        svc = _speaker_service()
        if svc is not None and getattr(match, "profile_id", None):
            profile = await svc.registry.get(match.profile_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("speaker_profile_lookup_failed", error=str(e)[:160])
    return TurnIdentity.from_match(match, profile=profile)


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    if STATE.brain is None:
        raise HTTPException(status_code=503, detail="Not ready")
    _require_model_present()
    # Validation OUTSIDE the try, so a real client error (missing message)
    # returns 422 instead of being swallowed by the generic 500 handler below.
    user_text = _compose_chat_message(STATE.config, req.message, req.attachments)
    if not user_text:
        raise HTTPException(status_code=422, detail="Missing 'message' or 'attachments'")
    try:
        BUS.publish("chat.user_message", {"chars": len(user_text)})
        BUS.publish("chat.thinking_start", {})
        try:
            resp = await STATE.brain.chat(
                user_text,
                conversation_id=req.conversation_id,
                current_location=req.current_location.model_dump() if req.current_location is not None else None,
                identity=await _resolve_turn_identity(req.voice_turn_id, req.input_source),
            )
        finally:
            BUS.publish("chat.thinking_end", {})
        BUS.publish("chat.assistant_done", {"chars": len(resp.assistant_text or ""), "tools_used": len(resp.tool_calls or [])})
        return {
            "conversation_id": str(resp.conversation_id),
            "assistant": resp.assistant_text,
            "tool_calls": resp.tool_calls,
        }
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("chat_failed", error=str(e))
        raise HTTPException(status_code=500, detail="chat_failed") from e




@app.post("/vision/analyze")
async def vision_analyze(
    file: UploadFile = File(...),
    question: str = Query("Describe the image in detail."),
) -> dict:
    """Analyze a single image (camera frame, screenshot, upload) with a multimodal model.

    Notes:
    - Requires NOVA_MMPROJ_PATH to point at the mmproj *.gguf for Qwen2.5-VL (or compatible VLM).
    - Uses the same loaded LLMRuntime instance as chat.
    """
    if STATE.llm is None:
        raise HTTPException(status_code=503, detail="Not ready")
    vision = STATE.llm.vision_status
    if not bool(vision.get("enabled")):
        raise HTTPException(status_code=503, detail=str(vision.get("reason") or "Vision is not configured"))
    # Validate OUTSIDE the try so an empty upload returns 422, not a generic 500.
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    try:
        BUS.publish("vision.analysis_start", {"bytes": len(data)})
        text = await STATE.llm.vision_analyze(data, question=question)
        BUS.publish("vision.analysis_done", {"chars": len(text or "")})
        return {"text": text}
    except GPUEnforcementError as e:
        BUS.publish("vision.error", {"error": _event_clip(e, 200)})
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("vision_analyze_failed", error=str(e))
        BUS.publish("vision.error", {"error": _event_clip(e, 200)})
        raise HTTPException(status_code=500, detail="vision_analyze_failed") from e


@app.post("/vision/screen_capture_result")
async def vision_screen_capture_result(payload: dict) -> dict:
    """Frontend calls this after the user responds to a screen.capture_requested
    event (PI1) — approve+result, or decline. Resolves the matching
    vision.look_at_screen tool call waiting in core/screen_broker.py."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    request_id = str(payload.get("request_id") or "")
    if not request_id:
        raise HTTPException(status_code=422, detail="Missing 'request_id'")
    resolved = STATE.runtime.screen_broker.resolve(request_id, {
        "approved": bool(payload.get("approved")),
        "text": str(payload.get("text") or ""),
        "error": str(payload.get("error") or ""),
    })
    return {"ok": resolved}


@app.post("/chat/stream")
async def chat_stream(req: ChatStreamRequest) -> StreamingResponse:
    if STATE.brain is None or STATE.config is None:
        raise HTTPException(status_code=503, detail="Not ready")

    _require_model_present()

    user_text = _compose_chat_message(STATE.config, req.msg or req.message, req.attachments)
    if not user_text:
        raise HTTPException(status_code=422, detail="Missing 'msg', 'message', or 'attachments'")

    # Redeem ONCE, here, before the generator — not inside it. The handle is
    # single-use, so resolving it per-yield or on a retried stream would burn
    # it and silently downgrade a recognised speaker to unverified (V3 P5.1).
    turn_identity = await _resolve_turn_identity(req.voice_turn_id, req.input_source)

    def _sse(event: str, payload: dict) -> bytes:
        import json as _json

        return f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    async def gen():
        conv_id = req.conversation_id or uuid4()
        speak = bool(req.speak)

        # Turn identity: everything asynchronous below carries this id, and a
        # barge-in on /voice/interrupt cancels it. Starting a turn also
        # supersedes whatever was live in this conversation, so an abandoned
        # previous turn stops producing audio immediately.
        turn = STATE.turns.start(str(conv_id))
        turn_id = turn.turn_id

        yield _sse("meta", {"conversation_id": str(conv_id), "turn_id": turn_id})

        # ── Sentence-streamed TTS worker (runs alongside token streaming) ────
        sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
        audio_q: asyncio.Queue[dict | None] = asyncio.Queue()

        async def tts_worker() -> None:
            try:
                voice_path = _resolve_voice_path(STATE.config, req.voice)
            except Exception as e:  # noqa: BLE001
                await audio_q.put({"error": f"voice unavailable: {e}"})
                while await sentence_q.get() is not None:
                    pass
                await audio_q.put(None)
                return

            while True:
                sentence = await sentence_q.get()
                if sentence is None:
                    break
                if STATE.turns.is_cancelled(turn_id):
                    # Barge-in landed while this sentence was queued. Drop the
                    # rest of the turn rather than synthesising audio nobody
                    # will be allowed to hear.
                    continue
                try:
                    BUS.publish("tts.generate_start", {"chars": len(sentence), "turn_id": turn_id})
                    audio = await _tts_bytes(sentence, voice_path=voice_path, turn_id=turn_id)
                    if STATE.turns.is_cancelled(turn_id):
                        continue
                    STATE.turns.record_spoken(turn_id, sentence)
                    BUS.publish("tts.generate_done", {"bytes": len(audio)})
                    audio_id = str(UUID(bytes=os.urandom(16)))
                    STATE.tts_cache[audio_id] = audio
                    # Bound the cache: every spoken sentence lands here and was
                    # never evicted (unbounded WAV growth). Keep the most recent
                    # clips (dict preserves insertion order); the frontend fetches
                    # each clip once, right after it's produced.
                    while len(STATE.tts_cache) > _TTS_CACHE_MAX:
                        STATE.tts_cache.pop(next(iter(STATE.tts_cache)), None)
                    await audio_q.put({"audio_url": f"/tts/{audio_id}", "turn_id": turn_id,
                                       "text": sentence})
                except asyncio.CancelledError:
                    # The isolated engine cancels a synthesis whose turn was
                    # interrupted. That is a normal barge-in, not an error.
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.debug("tts_sentence_failed", error=str(e))
                    await audio_q.put({"error": str(e)})
            await audio_q.put(None)

        worker = asyncio.create_task(tts_worker()) if speak else None

        # ── Token stream (real streaming via the function-calling pipeline) ──
        tool_calls_result: list[dict] = []
        full_text = ""
        sent_any_token = False
        # Speech chunker V2: abbreviation/decimal/URL aware, and willing to cut
        # at a clause boundary so the first audio starts sooner (core/voice).
        chunker = SpeechChunker()

        async def queue_for_speech(chunks: list[str]) -> None:
            """Convert display text to spoken text, then queue it."""
            for raw in chunks:
                spoken = to_spoken(raw)
                if spoken and has_speakable_content(spoken):
                    await sentence_q.put(spoken)

        async def source():
            BUS.publish("chat.user_message", {"chars": len(user_text)})
            BUS.publish("chat.thinking_start", {})
            try:
                async for ev in STATE.brain.chat_stream(
                    user_text,
                    conversation_id=conv_id,
                    current_location=req.current_location.model_dump() if req.current_location is not None else None,
                    identity=turn_identity,
                ):
                    yield ev
            finally:
                BUS.publish("chat.thinking_end", {})

        try:
            async for ev in source():
                if ev.get("type") == "token":
                    token = str(ev.get("text") or "")
                    if not token:
                        continue
                    full_text += token
                    sent_any_token = True
                    yield _sse("message", {"content": token})

                    if speak:
                        await queue_for_speech(chunker.feed(token))
                        # Forward any finished audio without blocking the tokens.
                        while True:
                            try:
                                item = audio_q.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            if item is None:
                                break
                            if STATE.turns.is_cancelled(turn_id):
                                continue
                            yield _sse("tts_error" if "error" in item else "tts", item)

                elif ev.get("type") == "done":
                    full_text = str(ev.get("full_text") or full_text)
                    tool_calls_result = ev.get("tool_calls") or []
        except Exception as e:  # noqa: BLE001
            logger.error("chat_stream_failed", error=str(e))
            # Always signal the crash so the frontend can distinguish a mid-stream
            # failure from a clean finish (previously both looked like `done`).
            yield _sse("error", {"message": "The reply was interrupted by an internal error.", "where": "stream"})
            if not sent_any_token:
                apology = "Sorry — I hit an internal error on that one."
                full_text = apology
                sent_any_token = True
                yield _sse("message", {"content": apology})
                if speak:
                    await queue_for_speech([apology])

        # The pipeline can legitimately finish with no visible tokens (e.g. a
        # reasoning model whose entire generation got spent on hidden
        # chain-of-thought). Never leave the user staring at a blank reply.
        if not sent_any_token:
            apology = full_text.strip() or "Sorry — I came up empty on that one."
            full_text = apology
            yield _sse("message", {"content": apology})
            if speak:
                await queue_for_speech([apology])

        BUS.publish("chat.assistant_done", {"chars": len(full_text), "tools_used": len(tool_calls_result)})

        # ── Flush remaining speech and drain the audio queue in order ───────
        if speak and worker is not None:
            await queue_for_speech(chunker.flush())
            await sentence_q.put(None)
            while True:
                item = await audio_q.get()
                if item is None:
                    break
                if STATE.turns.is_cancelled(turn_id):
                    # A late clip from a turn the user already interrupted. This
                    # is the leak that would otherwise let turn 105 speak over
                    # turn 106; drop it rather than emit it.
                    continue
                yield _sse("tts_error" if "error" in item else "tts", item)
            await worker

        # ── Action events (open maps overlay for location results) ──────────
        for tc in tool_calls_result:
            if tc.get("tool") in {"maps.directions", "maps.places_nearby", "maps.place_search", "maps.geocode"} and tc.get("ok") and tc.get("result"):
                yield _sse("action", {"type": "open_overlay", "overlay": "maps", "map_payload": tc["result"]})
            if tc.get("tool") == "image.generate" and tc.get("ok") and isinstance(tc.get("result"), dict):
                data_url = tc["result"].get("image_data_url")
                if data_url:
                    yield _sse("action", {"type": "image_generated", "image_url": data_url, "prompt": tc["result"].get("prompt", "")})

        STATE.turns.finish(turn_id)
        yield _sse("done", {"turn_id": turn_id})

    return StreamingResponse(gen(), media_type="text/event-stream")




async def tts(req: TtsRequest) -> Response:
    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")

    voice_path = _resolve_voice_path(cfg, req.voice)

    try:
        audio = await _tts_bytes(req.text, voice_path=voice_path)
        # Do not write any generated audio to disk.
        return Response(content=audio, media_type="audio/wav")
    except Exception as e:  # noqa: BLE001
        logger.error("tts_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


class InterruptRequest(BaseModel):
    conversation_id: str | None = None
    turn_id: str | None = None
    #: Optional STT transcript of what the user said over Nova. Used to decide
    #: whether this was a genuine interruption or the microphone hearing Nova.
    transcript: str | None = None
    reason: str = "user_interrupt"


@app.post("/voice/interrupt")
async def voice_interrupt(req: InterruptRequest) -> dict:
    """Barge-in. Stop speaking now, and say what survived of the user's words.

    The frontend calls this the moment it detects speech over playback. It is
    deliberately cheap and synchronous — the whole value is in how fast the
    audio stops.

    When a transcript is supplied it is run through echo suppression first, so
    Nova's own voice coming back through the speakers cannot trigger a
    "cancellation" of the very sentence it is currently saying. A genuine
    interruption cancels; pure echo does not.
    """
    turns = STATE.turns
    verdict = None

    if req.transcript:
        verdict = EchoFilter(turns).check(req.transcript, conversation_id=req.conversation_id)
        if not verdict.is_user_speech:
            # Nova heard herself. Keep talking.
            BUS.publish("voice.echo_rejected", {"matched": verdict.matched_tokens,
                                                "of": verdict.total_tokens})
            return {
                "interrupted": False,
                "classification": verdict.kind,
                "reason": verdict.reason,
                "text": "",
            }

    if req.turn_id:
        cancelled = req.turn_id if turns.cancel(req.turn_id, reason=req.reason) else None
    elif req.conversation_id:
        cancelled = turns.cancel_active(req.conversation_id, reason=req.reason)
    else:
        raise HTTPException(status_code=422, detail="Provide conversation_id or turn_id")

    dropped = 0
    if cancelled and STATE.tts_engine is not None:
        # Stop synthesis that has not started, and guarantee that anything
        # already in flight can never be delivered.
        dropped = STATE.tts_engine.cancel_turn(cancelled)

    if cancelled:
        BUS.publish("voice.interrupted", {"turn_id": cancelled, "dropped": dropped})

    return {
        "interrupted": bool(cancelled),
        "turn_id": cancelled,
        "dropped_clips": dropped,
        "classification": (verdict.kind if verdict else "user"),
        "text": (verdict.text if verdict else (req.transcript or "")),
    }


@app.get("/tts/{audio_id}")
async def tts_get(audio_id: str) -> Response:
    audio = STATE.tts_cache.get(audio_id)
    if audio is None:
        raise HTTPException(status_code=404, detail="Audio not found")
    return Response(content=audio, media_type="audio/wav")


@app.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    # Frontend expects /speak returning audio bytes.
    return await tts(TtsRequest(text=req.text, voice=req.voice))


@app.post("/stt", response_model=SttResponse)
async def stt(file: UploadFile = File(...),
              speaker: bool = Form(False)) -> SttResponse:
    """Transcribe. `speaker=true` additionally identifies who spoke (V3 P5).

    Default OFF so the continuous wake loop stays free.
    """
    try:
        return await _stt_transcribe(file, identify_speaker=bool(speaker))
    except Exception as e:  # noqa: BLE001
        logger.error("stt_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/file-upload")
async def file_upload(files: list[UploadFile] = File(...)) -> dict:
    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")

    upload_dir = _upload_dir(cfg)
    upload_dir.mkdir(parents=True, exist_ok=True)

    stored: list[dict[str, Any]] = []
    for f in files:
        name = Path(f.filename or "file.bin").name
        data = await f.read()
        stored_name = f"{uuid4().hex}_{name}"
        out = upload_dir / stored_name
        # Upload size is caller-controlled and unbounded, so this write does
        # not belong on the event loop — a large attachment would stall chat,
        # streaming and every worker for its duration.
        await asyncio.to_thread(out.write_bytes, data)
        stored.append(
            {
                "name": name,
                "path": str(out),
                "bytes": len(data),
                "url": f"/uploads/{stored_name}",
                "content_type": f.content_type or None,
            }
        )

    return {"files": stored}


@app.get("/uploads/{stored_name}")
async def upload_get(stored_name: str) -> FileResponse:
    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")

    path = _safe_upload_path(cfg, stored_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return FileResponse(path)
