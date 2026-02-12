from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from core.llm_runtime import GPUEnforcementError, LLMRuntime
from core.logging_setup import get_logger, setup_logging
from memory.unifier import MemoryUnifier
from core.brain import Brain
from plugins.registry import PluginConfigError, REGISTRY


logger = get_logger(__name__)

class RuntimeConfig(BaseModel):
    # Paths
    repo_root: Path
    model_dir: Path
    model_path: Path | None
    projects_dir: Path
    memory_dir: Path
    voice_dir: Path
    # Runtime
    context_tokens: int = 8192
    log_level: str = "INFO"
    version: str = "0.1.0"



class MemoryPurgeRequest(BaseModel):
    entity: str = Field(..., description="Fact entity (e.g. 'user').")
    attribute: str | None = Field(None, description="Optional attribute filter (e.g. 'child').")
    value_in: list[str] | None = Field(None, description="Delete values whose LOWER(value) is in this list.")
    value_ilike: str | None = Field(None, description="Delete values whose LOWER(value) matches LIKE '%value_ilike%'.")
    dry_run: bool = Field(True, description="If true, return matches without deleting.")
    limit: int = Field(5000, ge=1, le=20000, description="Max facts to match/delete in one call.")

class MemoryPurgeResponse(BaseModel):
    entity: str
    attribute: str | None
    value_in: list[str] | None
    value_ilike: str | None
    dry_run: bool
    matched: int
    deleted: int
    ids: list[str]


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
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return candidates[0]


def _load_runtime_config() -> RuntimeConfig:
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
        model_path = _pick_model_path(model_dir)

    projects_dir = Path(os.getenv("NOVA_PROJECTS_DIR", str(repo_root / "projects"))).expanduser().resolve()
    memory_dir = Path(os.getenv("NOVA_MEMORY_DIR", str(repo_root / "memory_data"))).expanduser().resolve()
    voice_dir = Path(os.getenv("NOVA_VOICE_DIR", str(repo_root / "voices"))).expanduser().resolve()

    ctx = int(os.getenv("NOVA_CONTEXT_TOKENS", "8192").strip() or "8192")
    log_level = (os.getenv("NOVA_LOG_LEVEL", "INFO").strip() or "INFO").upper()

    return RuntimeConfig(
        repo_root=repo_root,
        model_dir=model_dir,
        model_path=model_path,
        projects_dir=projects_dir,
        memory_dir=memory_dir,
        voice_dir=voice_dir,
        context_tokens=ctx,
        log_level=log_level,
        version=os.getenv("NOVA_VERSION", "0.1.0").strip() or "0.1.0",
    )



class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: UUID | None = None


class PluginExecuteRequest(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TtsRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "nova.mp3"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "nova.mp3"


class ChatStreamRequest(BaseModel):
    # Frontend historically sent `msg`; some callers send `message`.
    msg: str | None = None
    message: str | None = None
    conversation_id: UUID | None = None
    speak: bool = False
    voice: str = "nova.mp3"


app = FastAPI(title="Nova Backend", version="0.1.0")


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


class _State:
    config: RuntimeConfig | None = None
    memory: MemoryUnifier | None = None
    llm: LLMRuntime | None = None
    brain: Brain | None = None
    tts = None
    stt = None
    tts_cache: dict[str, bytes] = {}


STATE = _State()


def _require_admin_token(token: str | None) -> None:
    expected = os.getenv("NOVA_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    if (token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg is required to read .mp3 reference voices for XTTS. "
            "Install FFmpeg and ensure `ffmpeg` is on PATH."
        )


def _wav_bytes_from_f32(samples, sample_rate: int) -> bytes:
    # samples expected float32 in [-1, 1]
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


async def _tts_bytes(text: str, voice_path: Path) -> bytes:
    # Load lazily and run in a thread (TTS is sync + heavy)
    def _load_tts():
        from TTS.api import TTS  # type: ignore

        return TTS("tts_models/multilingual/multi-dataset/xtts_v2")

    if STATE.tts is None:
        _ensure_ffmpeg()
        STATE.tts = await asyncio.to_thread(_load_tts)

    def _run() -> bytes:
        # XTTS accepts speaker_wav as file path
        wav = STATE.tts.tts(text=text, speaker_wav=str(voice_path), language="en")
        sr = int(getattr(STATE.tts.synthesizer, "output_sample_rate", 24000))
        return _wav_bytes_from_f32(wav, sr)

    return await asyncio.to_thread(_run)


def _chunk_text(s: str, chunk_size: int = 18) -> list[str]:
    s = s or ""
    if chunk_size <= 1:
        return list(s)
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


async def _stt_transcribe(upload: UploadFile) -> str:
    """Transcribe an uploaded audio file using Whisper via transformers.

    Notes:
    - We rely on ffmpeg to decode webm/ogg/etc into 16k mono wav.
    - The model weights are downloaded on first run.
    """

    _ensure_ffmpeg()

    # Lazily load ASR pipeline
    def _load_pipeline():
        import torch  # type: ignore
        from transformers import pipeline  # type: ignore

        model_id = os.getenv("NOVA_STT_MODEL", "openai/whisper-base")
        device = 0 if torch.cuda.is_available() else -1
        return pipeline("automatic-speech-recognition", model=model_id, device=device)

    if STATE.stt is None:
        STATE.stt = await asyncio.to_thread(_load_pipeline)

    # Write upload to a temp file for ffmpeg
    suffix = Path(upload.filename or "audio").suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="nova-stt-") as td:
        in_path = Path(td) / f"in{suffix}"
        out_path = Path(td) / "out.wav"
        data = await upload.read()
        in_path.write_bytes(data)

        # Decode to 16k mono wav
        cmd = [
            "ffmpeg",
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
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"ffmpeg_decode_failed: {e}") from e

        # Load wav and run ASR
        def _run_asr() -> str:
            import soundfile as sf  # type: ignore

            audio, sr = sf.read(str(out_path), dtype="float32")
            # transformers ASR pipeline accepts dict with array + sampling_rate
            result = STATE.stt({"array": audio, "sampling_rate": int(sr)})
            if isinstance(result, dict):
                return str(result.get("text") or "").strip()
            return str(result or "").strip()

        return await asyncio.to_thread(_run_asr)


@app.on_event("startup")
async def _startup() -> None:
    cfg = _load_runtime_config()
    setup_logging(cfg.log_level)

    STATE.config = cfg
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    cfg.voice_dir.mkdir(parents=True, exist_ok=True)

    memory = MemoryUnifier(cfg.memory_dir)
    await memory.initialize()

    llm = LLMRuntime(model_path=cfg.model_path, context_tokens=cfg.context_tokens)
    # If a model exists, enforce GPU offload at startup.
    if cfg.model_path is not None:
        try:
            await llm.initialize()
        except GPUEnforcementError as e:
            logger.error("gpu_enforcement_failed", error=str(e))
            raise

    from plugins import init as _plugins_init  # noqa: F401

    STATE.memory = memory
    STATE.llm = llm
    STATE.brain = Brain(repo_root=cfg.repo_root, projects_dir=cfg.projects_dir, memory=memory, llm=llm)

    logger.info(
        "startup_complete",
        version=cfg.version,
        model=str(cfg.model_path) if cfg.model_path else None,
        gpu_status=llm.gpu_status.__dict__,
        tools=list(REGISTRY.get_tools().keys()),
    )


@app.get("/health")
async def health() -> dict:
    cfg = STATE.config
    llm = STATE.llm
    if cfg is None or llm is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {
        "version": cfg.version,
        "gpu": llm.gpu_status.__dict__,
        "model": str(cfg.model_path) if cfg.model_path else None,
        "repo_root": str(cfg.repo_root),
        "model_dir": str(cfg.model_dir),
    }


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    if STATE.brain is None:
        raise HTTPException(status_code=503, detail="Not ready")
    try:
        resp = await STATE.brain.chat(req.message, conversation_id=req.conversation_id)
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


@app.post("/chat/stream")
async def chat_stream(req: ChatStreamRequest) -> StreamingResponse:
    if STATE.brain is None or STATE.config is None:
        raise HTTPException(status_code=503, detail="Not ready")

    user_text = (req.msg or req.message or "").strip()
    if not user_text:
        raise HTTPException(status_code=422, detail="Missing 'msg' or 'message'")

    async def gen():
        # Establish / reuse a stable conversation id
        conv_id = req.conversation_id or uuid4()

        # Generate assistant response first
        resp = await STATE.brain.chat(user_text, conversation_id=conv_id)
        text = resp.assistant_text or ""

        # --- META EVENT ---
        yield b"event: meta\n"
        yield (
            "data: "
            + __import__("json").dumps(
                {"conversation_id": str(conv_id)},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode("utf-8")

        # --- MESSAGE STREAM ---
        for chunk in _chunk_text(text, chunk_size=18):
            yield b"event: message\n"
            yield (
                "data: "
                + __import__("json").dumps(
                    {"content": chunk},
                    ensure_ascii=False,
                )
                + "\n\n"
            ).encode("utf-8")
            await asyncio.sleep(0.01)

        # --- OPTIONAL TTS ---
        if req.speak:
            try:
                voice_path = (STATE.config.voice_dir / req.voice).resolve()
                audio = await _tts_bytes(text, voice_path=voice_path)
                audio_id = str(UUID(bytes=os.urandom(16)))
                STATE.tts_cache[audio_id] = audio

                yield b"event: tts\n"
                yield (
                    "data: "
                    + __import__("json").dumps(
                        {"audio_url": f"/tts/{audio_id}"},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
            except Exception as e:
                logger.debug("tts_failed", error=str(e))

        # --- DONE ---
        yield b"event: done\n"
        yield b"data: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/memory/search")
async def memory_search(q: str = Query(min_length=1)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    hits = await STATE.memory.search(q=q, conversation_id=None, limit=12)
    return {"q": q, "results": [h.model_dump() for h in hits]}



@app.post("/memory/purge", response_model=MemoryPurgeResponse)
async def memory_purge(req: MemoryPurgeRequest, admin_token: str | None = Query(None, alias="admin_token")) -> MemoryPurgeResponse:
    """Maintenance endpoint to purge bad/legacy facts from memory stores.

    If env NOVA_ADMIN_TOKEN is set, caller must provide ?admin_token=... .
    """
    _require_admin_token(admin_token)
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Memory not initialized")
    result = await STATE.memory.purge_facts(
        entity=req.entity,
        attribute=req.attribute,
        value_in=req.value_in,
        value_ilike=req.value_ilike,
        dry_run=req.dry_run,
        limit=req.limit,
    )
    return MemoryPurgeResponse(**result)



@app.post("/plugins/execute")
async def plugins_execute(req: PluginExecuteRequest) -> dict:
    tools = REGISTRY.get_tools()
    if req.name not in tools:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.name}")
    try:
        result = await tools[req.name].fn(req.args)
        return {"name": req.name, "ok": True, "result": result}
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("plugin_execute_failed", tool=req.name, error=str(e))
        raise HTTPException(status_code=500, detail="plugin_execute_failed") from e


@app.post("/tts")
async def tts(req: TtsRequest) -> Response:
    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")

    voice_path = (cfg.voice_dir / req.voice).resolve()
    if not voice_path.exists():
        raise HTTPException(status_code=404, detail=f"Voice not found: {req.voice}")

    try:
        audio = await _tts_bytes(req.text, voice_path=voice_path)
        # Do not write any generated audio to disk.
        return Response(content=audio, media_type="audio/wav")
    except Exception as e:  # noqa: BLE001
        logger.error("tts_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


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


@app.post("/stt")
async def stt(file: UploadFile = File(...)) -> dict:
    try:
        text = await _stt_transcribe(file)
        return {"text": text}
    except Exception as e:  # noqa: BLE001
        logger.error("stt_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/file-upload")
async def file_upload(files: list[UploadFile] = File(...)) -> dict:
    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")

    upload_dir = (cfg.projects_dir / "_uploads").resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)

    stored: list[dict[str, Any]] = []
    for f in files:
        name = Path(f.filename or "file.bin").name
        data = await f.read()
        out = upload_dir / name
        out.write_bytes(data)
        stored.append({"name": name, "path": str(out), "bytes": len(data)})

    return {"files": stored}