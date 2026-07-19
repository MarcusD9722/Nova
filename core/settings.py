from __future__ import annotations

"""Central catalog + boot validation for every NOVA_* environment variable.

Phase 0.4 of docs/ROADMAP.md. Before this module, 63 env vars were read
ad-hoc at call sites with no schema: a typo'd var name silently fell back to
its default and misconfiguration was invisible. This module fixes that
without call-site churn:

- ``CATALOG`` documents every variable (type, default, description) in one
  place — the single source of truth for what Nova can be configured with.
- ``validate_environment()`` runs at boot: it flags NOVA_* vars present in
  the environment that Nova doesn't know (typo detection) and values that
  don't parse as their declared type. Warnings only — Nova never refuses to
  boot over config style.
- Typed accessors (``get_str``/``get_bool``/``get_int``/``get_float``) are
  for NEW code; existing call sites migrate opportunistically under the
  strangler rule (docs/ARCHITECTURE.md §4.2). tests/test_settings_p04.py
  scans the codebase and fails if a var is used anywhere without being
  cataloged here.

Secrets are marked ``secret=True`` and their values are never included in
validation messages or logs.
"""

import os
import re
from dataclasses import dataclass

from core.logging_setup import get_logger

logger = get_logger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str  # bool | int | float | str | path | hhmm | csv
    default: str
    description: str
    secret: bool = False


def _s(name: str, kind: str, default: str, description: str, secret: bool = False) -> tuple[str, Setting]:
    return name, Setting(name, kind, default, description, secret)


CATALOG: dict[str, Setting] = dict(
    [
        # ── Core runtime ────────────────────────────────────────────────────
        _s("NOVA_HOST", "str", "127.0.0.1", "Bind address for the backend."),
        _s("NOVA_PORT", "int", "8008", "Backend port."),
        _s("NOVA_VERSION", "str", "0.1.0", "Reported version string."),
        _s("NOVA_REPO_ROOT", "path", "", "Override repo root autodetection."),
        _s("NOVA_MEMORY_DIR", "path", "memory_data", "Runtime memory storage directory."),
        _s("NOVA_PROJECTS_DIR", "path", "projects", "Directory for Nova-built projects."),
        # ── Logging ─────────────────────────────────────────────────────────
        _s("NOVA_LOG_LEVEL", "str", "INFO", "Application log level."),
        _s("NOVA_LOG_FORMAT", "str", "", "Log format override (console|json; empty = auto)."),
        _s("NOVA_NOISY_LOG_LEVEL", "str", "WARNING", "Level for chatty third-party loggers."),
        _s("NOVA_LLAMA_LOG_LEVEL", "str", "ERROR", "llama.cpp native log level."),
        _s("NOVA_LLM_PERF_LOG", "bool", "0", "Log per-call LLM performance stats."),
        # ── Model / LLM ─────────────────────────────────────────────────────
        _s("NOVA_MODEL_PATH", "path", "", "Explicit GGUF path (empty = newest in model dir)."),
        _s("NOVA_MODEL_DIR", "path", "", "Model directory override (default: model/)."),
        _s("NOVA_MMPROJ_PATH", "path", "", "Explicit mmproj GGUF for vision (empty = auto-detect)."),
        _s("NOVA_CHAT_FORMAT", "str", "", "llama-cpp chat format override."),
        _s("NOVA_CONTEXT_TOKENS", "int", "8192", "Model context window."),
        _s("NOVA_MAX_TOKENS", "int", "1536", "Default reply token budget."),
        _s("NOVA_MAIN_GPU", "int", "0", "CUDA device index for the LLM."),
        _s("NOVA_LLM_ALLOW_THINKING", "bool", "", "Globally allow reasoning blocks in replies."),
        _s("NOVA_VISION_FORCE", "bool", "", "Force-enable vision even if detection is unsure."),
        # ── Voice ───────────────────────────────────────────────────────────
        _s("NOVA_VOICE_DIR", "path", "voices", "Reference-voice wav directory."),
        _s("NOVA_DEFAULT_VOICE", "str", "", "Default reference voice filename."),
        _s("NOVA_TTS_DEVICE", "str", "auto", "XTTS device (cuda|auto)."),
        _s("NOVA_TTS_PREWARM", "bool", "0", "Load + warm XTTS at startup."),
        _s("NOVA_TTS_WARMUP_TEXT", "str", "Hello there. I am ready to help.", "Prewarm utterance."),
        _s("NOVA_TTS_SPEED", "float", "1.0", "Base TTS speed multiplier."),
        _s("NOVA_TTS_MOOD_PACING", "bool", "1", "Subtle mood-based speech pacing (WS-H)."),
        _s("NOVA_TTS_CACHE_MAX", "int", "64", "Max cached TTS clips."),
        _s("NOVA_FFMPEG_PATH", "path", "", "Explicit ffmpeg path if not on PATH."),
        _s("NOVA_STT_MODEL", "str", "openai/whisper-base", "Transformers STT fallback model."),
        _s("NOVA_STT_MODEL_SIZE", "str", "base", "faster-whisper model size."),
        # ── Memory ──────────────────────────────────────────────────────────
        _s("NOVA_EMBED_MODEL", "str", "BAAI/bge-small-en-v1.5", "Embedding model for semantic memory."),
        _s("NOVA_EMBED_DEVICE", "str", "auto", "Embedding device (cuda|cpu|auto)."),
        _s("NOVA_INDEX_TURNS", "bool", "1", "Semantically index substantive conversation turns."),
        _s("NOVA_RECENT_CHAT_TURNS", "int", "20", "Recent turns kept in the reply prompt."),
        _s("NOVA_SUMMARY_EVERY_N", "int", "8", "Summarize the conversation every N turns."),
        _s("NOVA_FOLLOWUP_WINDOW", "int", "10", "Follow-up dedup window (turns)."),
        # ── Behavior / autonomy ─────────────────────────────────────────────
        _s("NOVA_AUTONOMY", "bool", "1", "Enable background autonomy workers."),
        _s("NOVA_AUTONOMY_TICK_S", "float", "5", "Autonomy supervisor tick seconds."),
        _s("NOVA_AGENT_TICK_S", "float", "1", "Goal supervisor tick seconds."),
        _s("NOVA_AGENT_MAX_STEPS", "int", "6", "Max tool steps per chat turn."),
        _s("NOVA_SELF_IMPROVE_INTERVAL_S", "float", "1800", "Self-improve cycle interval."),
        _s("NOVA_RESUME_BACKGROUND_TASKS", "bool", "0", "Resume queued autonomy tasks on boot."),
        _s("NOVA_ALLOW_SHELL", "bool", "1", "Expose the guarded shell.exec tool."),
        _s("NOVA_ALLOW_NETWORK_TOOLS", "bool", "1", "Expose network plugins to the agent."),
        _s("NOVA_STORY_TOKENS", "int", "1200", "Token budget for storytelling mode."),
        # ── Scheduling ──────────────────────────────────────────────────────
        _s("NOVA_REMINDER_POLL_S", "float", "30", "Reminder worker poll interval."),
        _s("NOVA_BRIEFING_TIME", "hhmm", "08:00", "Morning briefing time (empty disables)."),
        # ── Developer mode / security ───────────────────────────────────────
        _s("NOVA_DEV_MODE", "bool", "0", "Enable guarded self-editing."),
        _s("NOVA_API_TOKEN", "str", "", "Bearer token required on all endpoints when set.", secret=True),
        _s("NOVA_ADMIN_TOKEN", "str", "", "Extra token for maintenance endpoints.", secret=True),
        _s("NOVA_ALLOWED_ORIGINS", "csv", "", "CORS origins override (comma-separated)."),
        # ── Project builder ─────────────────────────────────────────────────
        _s("NOVA_PROJECT_FILE_TOKENS", "int", "3000", "Token budget per generated project file."),
        _s("NOVA_PROJECT_RUN_CHECK", "bool", "1", "Run generated projects as a build check."),
        _s("NOVA_PROJECT_LOGIC_TESTS", "bool", "1", "Generate + run logic tests for projects."),
        # ── Image/video generation service ──────────────────────────────────
        _s("NOVA_IMAGEGEN_PORT", "int", "8801", "Local imagegen service port."),
        _s("NOVA_IMAGEGEN_MODEL", "str", "stabilityai/sdxl-turbo", "Diffusion model id."),
        _s("NOVA_IMAGEGEN_CUDA_DEVICE", "str", "1", "Physical GPU pinned via CUDA_VISIBLE_DEVICES."),
        # ── Avatar (photo-relief pipeline) ──────────────────────────────────
        _s("NOVA_PORTRAIT_EMISSION", "float", "0.4", "Avatar hologram emission strength."),
        _s("NOVA_PORTRAIT_GRADE", "float", "1.0", "Avatar color grade."),
        _s("NOVA_PORTRAIT_EYE_V", "float", "0.565", "Avatar eye vertical anchor."),
        _s("NOVA_PORTRAIT_EYE_SPAN", "float", "0.205", "Avatar eye span."),
        _s("NOVA_PORTRAIT_MOUTH_V", "float", "0.395", "Avatar mouth vertical anchor."),
    ]
)

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


# ── Typed accessors (for new code; old call sites migrate opportunistically) ──

def get_str(name: str) -> str:
    spec = CATALOG[name]
    return os.getenv(name, spec.default).strip()


def get_bool(name: str) -> bool:
    spec = CATALOG[name]
    raw = os.getenv(name, spec.default).strip().lower()
    return raw in _TRUTHY


def get_int(name: str) -> int:
    spec = CATALOG[name]
    raw = os.getenv(name, spec.default).strip() or spec.default
    try:
        return int(raw)
    except ValueError:
        return int(spec.default or 0)


def get_float(name: str) -> float:
    spec = CATALOG[name]
    raw = os.getenv(name, spec.default).strip() or spec.default
    try:
        return float(raw)
    except ValueError:
        return float(spec.default or 0)


# ── Boot validation ──────────────────────────────────────────────────────────

def validate_environment(environ: dict[str, str] | None = None) -> list[str]:
    """Return human-readable warnings about the NOVA_* environment.

    Never raises and never includes secret values. Two classes of finding:
    unknown NOVA_* names (almost always a typo — the value is silently
    ignored otherwise, which is exactly the failure mode this catches) and
    set values that don't parse as the declared type.
    """
    env = environ if environ is not None else dict(os.environ)
    warnings: list[str] = []

    for name in sorted(env):
        if name.startswith("NOVA_") and name not in CATALOG:
            close = _closest(name)
            hint = f" (did you mean {close}?)" if close else ""
            warnings.append(f"Unknown variable {name} is set but Nova doesn't read it{hint}.")

    for name, spec in CATALOG.items():
        raw = env.get(name)
        if raw is None or raw.strip() == "":
            continue
        value = raw.strip()
        shown = "<hidden>" if spec.secret else value
        if spec.kind == "int":
            try:
                int(value)
            except ValueError:
                warnings.append(f"{name}={shown} is not an integer; default {spec.default!r} will be used by typed accessors.")
        elif spec.kind == "float":
            try:
                float(value)
            except ValueError:
                warnings.append(f"{name}={shown} is not a number; default {spec.default!r} will be used by typed accessors.")
        elif spec.kind == "bool":
            if value.lower() not in _TRUTHY | _FALSY:
                warnings.append(f"{name}={shown} is not a recognized boolean (use 1/0/true/false/yes/no/on/off).")
        elif spec.kind == "hhmm":
            if not _HHMM_RE.match(value):
                warnings.append(f"{name}={shown} is not HH:MM (24h).")

    return warnings


def _closest(name: str) -> str | None:
    """Typo hint via stdlib fuzzy matching (handles transpositions like
    NOVA_PROT → NOVA_PORT, which prefix matching misses)."""
    import difflib

    matches = difflib.get_close_matches(name, list(CATALOG), n=1, cutoff=0.75)
    return matches[0] if matches else None


def log_environment_validation() -> int:
    """Run validation and log each finding. Returns the number of warnings."""
    warnings = validate_environment()
    for w in warnings:
        logger.warning("config_warning", detail=w)
    if warnings:
        logger.warning("config_validation_summary", findings=len(warnings))
    return len(warnings)
