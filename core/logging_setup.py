from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import orjson
import structlog


_SECRET_ENV_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


class _RedactSecretsProcessor:
    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in event_dict.items():
            if _SECRET_ENV_NAME_RE.search(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = value
        return redacted


def _orjson_dumps(obj: Any, *, default: Any | None = None, **_: Any) -> str:
    options = orjson.OPT_NON_STR_KEYS | orjson.OPT_SORT_KEYS
    if default is None:
        return orjson.dumps(obj, option=options).decode("utf-8")
    return orjson.dumps(obj, default=default, option=options).decode("utf-8")


def _join(items: Any) -> str:
    if not items:
        return "-"
    if isinstance(items, (list, tuple, set)):
        return ", ".join(str(x) for x in items)
    return str(items)


def _startup_bullets_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Render key boot events as clean bullet lines in console output.

    This processor is intentionally narrow: it only rewrites known boot events.
    Everything else passes through unchanged.
    """
    event = event_dict.get("event")
    if not event:
        return event_dict

    # These are emitted during backend startup; keep them human-friendly.
    if event == "memory_backends_detected":
        event_dict["event"] = f"• Memory backends detected: {_join(event_dict.get('backends'))}"
        return {"event": event_dict["event"]}

    if event == "memory_backends_initialized":
        event_dict["event"] = f"• Memory backends initialized: {_join(event_dict.get('backends'))}"
        return {"event": event_dict["event"]}

    if event in ("llm_loaded", "model_loaded"):
        model = event_dict.get("model")
        model_name = Path(str(model)).name if model else "unknown"
        n_ctx = event_dict.get("n_ctx")
        suffix = f" (ctx={n_ctx})" if n_ctx else ""
        event_dict["event"] = f"• Model loaded: {model_name}{suffix}"
        return {"event": event_dict["event"]}

    if event == "plugins_detected":
        event_dict["event"] = f"• Plugins detected: {_join(event_dict.get('plugins'))}"
        return {"event": event_dict["event"]}

    if event == "plugins_loaded":
        tools = event_dict.get("tools") or event_dict.get("tools_count")
        if isinstance(tools, int):
            event_dict["event"] = f"• Plugins loaded: {tools} tools"
        else:
            event_dict["event"] = f"• Plugins loaded: {_join(tools)}"
        return {"event": event_dict["event"]}

    if event == "startup_complete":
        gpu = (event_dict.get("gpu_status") or {}).get("status") or "unknown"
        event_dict["event"] = f"• Startup complete: {gpu}"
        return {"event": event_dict["event"]}

    # Leave everything else alone.
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    """Configure Nova logging.

    Defaults to a clean, human-friendly console format. Set NOVA_LOG_FORMAT=json
    to revert to structured JSON logs (useful for log aggregation).
    """
    level = (level or "INFO").upper()
    py_level = getattr(logging, level, logging.INFO)

    logging.basicConfig(level=py_level, format="%(message)s")

    log_format = (os.getenv("NOVA_LOG_FORMAT") or "human").strip().lower()
    if log_format not in {"human", "json"}:
        log_format = "human"

    processors: list[Callable[[Any, str, dict[str, Any]], Any]] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _RedactSecretsProcessor(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "human":
        # Convert key startup events into bullet lines, then render everything to console.
        processors.append(_startup_bullets_processor)
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer(serializer=_orjson_dumps))

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(py_level),
        cache_logger_on_first_use=True,
    )

    # Avoid noisy third-party loggers
    noisy_level = (os.getenv("NOVA_NOISY_LOG_LEVEL", "WARNING") or "WARNING").upper()
    noisy_py_level = getattr(logging, noisy_level, logging.WARNING)

    for noisy in [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "starlette",
        "httpcore",
        "httpx",
        "chromadb",
        "posthog",
        "multipart",
    ]:
        logging.getLogger(noisy).setLevel(noisy_py_level)

    # llama.cpp logging (best-effort; some builds still print a few lines)
    os.environ.setdefault("LLAMA_LOG_LEVEL", os.getenv("NOVA_LLAMA_LOG_LEVEL", "ERROR"))


def get_logger(name: str):
    return structlog.get_logger(name)
