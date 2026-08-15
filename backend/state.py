from __future__ import annotations

"""Shared mutable application state (Phase 0.6 of docs/ROADMAP.md).

Extracted verbatim from backend/app.py so router modules can import STATE
without importing the app module (which would be circular). app.py populates
these fields during startup/shutdown; routers only read them. This is the
one intentional global in the backend.
"""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported only for annotations; no runtime cycle
    from backend.app import RuntimeConfig
    from core.brain import Brain
    from core.llm_runtime import LLMRuntime
    from core.runtime import RuntimeManager
    from memory.unifier import MemoryUnifier


class _State:
    config: RuntimeConfig | None = None
    memory: MemoryUnifier | None = None
    llm: LLMRuntime | None = None
    brain: Brain | None = None
    runtime: RuntimeManager | None = None
    tts = None
    stt = None
    tts_cache: dict[str, bytes] = {}  # bounded to _TTS_CACHE_MAX most-recent clips
    tts_device: str | None = None
    tts_device_reason: str | None = None
    tts_sample_rate: int | None = None
    # The isolated XTTS worker client (services/tts_client.IsolatedTtsEngine).
    # None until first use; it owns its own process and CUDA context, which is
    # what makes GPU voice safe beside llama.cpp. See services/tts_worker.py.
    tts_engine = None
    # Turn identity for the voice pipeline (core/voice/turn.py). Created eagerly
    # because barge-in must be able to cancel a turn before anything else has
    # touched the voice subsystem.
    turns = None
    # MCP capability layer (core/mcp). None until servers are configured and
    # discovery finishes; started in the background so a slow server cannot
    # hold up boot.
    mcp = None
    mcp_task = None
    tts_load_task: asyncio.Task | None = None
    tts_prewarm_task: asyncio.Task | None = None
    tts_voice_cache: dict[str, str] = {}
    tts_voice_tasks: dict[str, asyncio.Task[str]] = {}
    tts_voice_cache_dir: Path | None = None
    tts_phrase_cache: dict[str, bytes] = {}
    dev_mode = None  # lazily created DevMode instance (see core/dev_mode.py)
    # Local speaker identification (V3 P5). None until the first request that
    # opts in; the embedding model itself loads lazily inside the service.
    speaker = None


# Max number of recent TTS clips to retain in memory (see tts_worker eviction).
_TTS_CACHE_MAX = int(os.getenv("NOVA_TTS_CACHE_MAX", "64").strip() or "64")

STATE = _State()

from core.voice.turn import TurnRegistry  # noqa: E402  (after _State, avoids a cycle)

STATE.turns = TurnRegistry()
