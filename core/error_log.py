from __future__ import annotations

"""Durable rolling log of Nova's own runtime errors.

Fed by the self-improvement worker (which subscribes to the event bus), this is
what lets Nova notice a recurring problem in her own operation and file a fix
proposal for Marcus. Kept small, bounded, and scrubbed of secrets.

That last part used to be assumed rather than done: the docstring claimed
payloads were "free of secrets (only event payloads, already clipped by the
bus)", but clipping is not redaction. A real entry on this machine read

    Client error '404 Not Found' for url
    'https://api.openweathermap.org/data/2.5/weather?q=...&appid=52e5e1...'

— the live OpenWeather key, in plaintext. It matters beyond the file itself,
because the self-improve loop feeds recurring error text to an LLM to diagnose
it, and roles can be routed to a remote provider. `_redact_secrets` now runs on
every message and context value before anything is stored.
"""

import asyncio
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

# Event types that represent a real problem worth logging.
_ERROR_HINTS = ("error", "failed", "failure", "exception", "traceback")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Credentials as they actually appear in error text: query-string parameters
# (the OpenWeather/Maps shape), JSON-ish key/value pairs, and Authorization
# headers. Kept deliberately broad on the NAME and total on the VALUE.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|appid|app_id|access[_-]?token|auth[_-]?token|"
               r"token|secret|password|passwd|pwd|client[_-]?secret)"
               r"(\s*[=:]\s*|=)([^&\s'\"},]+)"),
    re.compile(r"(?i)\b(bearer|bot)\s+([A-Za-z0-9._\-]{12,})"),
    re.compile(r"\b(sk-[A-Za-z0-9._\-]{8,})"),          # OpenAI-style keys
    re.compile(r"\b(AIza[A-Za-z0-9._\-]{20,})"),        # Google API keys
)


def _redact_secrets(text: str) -> str:
    """Strip credentials out of error text before it is stored or shown."""
    out = str(text or "")
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    for pattern in _SECRET_PATTERNS[2:]:
        out = pattern.sub("[REDACTED]", out)
    return out


def is_error_event(event_type: str, data: dict[str, Any] | None = None) -> bool:
    t = (event_type or "").lower()
    # "Not configured" is a state, not a defect — never a self-correction
    # candidate (it carries an `error` field, so exclude it before the
    # generic checks below).
    if t == "tool.not_configured":
        return False
    if t == "system.warning":
        return True
    if t.endswith(".error"):
        return True
    if any(h in t for h in _ERROR_HINTS):
        return True
    # Some events carry an explicit error field even if the type is neutral.
    if data and (data.get("error") or data.get("failed")):
        return True
    return False


def error_message(event_type: str, data: dict[str, Any] | None = None) -> str:
    data = data or {}
    for key in ("error", "message", "reason", "detail", "impact"):
        v = data.get(key)
        if v:
            return str(v)
    return event_type


class ErrorLog:
    """Bounded, persisted list of recent error events + recurrence counts."""

    def __init__(self, path: Path, max_entries: int = 400) -> None:
        self._path = Path(path)
        self._max = int(max_entries)
        self._entries: deque[dict[str, Any]] = deque(maxlen=self._max)
        self._lock = asyncio.Lock()
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for e in data[-self._max :]:
                    self._entries.append(e)
        except Exception as e:  # noqa: BLE001
            logger.debug("error_log_load_failed", error=str(e)[:200])

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(list(self._entries), ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.debug("error_log_persist_failed", error=str(e)[:200])

    # ── signature (for recurrence grouping) ──────────────────────────────────

    @staticmethod
    def signature(component: str, message: str) -> str:
        """Normalize an error into a stable key: drop digits, hex, and paths so
        the same failure with different specifics groups together."""
        m = (message or "").strip().splitlines()[0] if message else ""
        m = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+", "<path>", m)
        m = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "<n>", m)
        m = re.sub(r"\s+", " ", m).strip().lower()[:160]
        return f"{(component or '').lower()}::{m}"

    # ── api ──────────────────────────────────────────────────────────────────

    async def record(self, component: str, message: str, context: dict[str, Any] | None = None) -> None:
        # Redact BEFORE clipping and before the signature is computed, so a
        # rotated key can't produce a different signature for the same fault.
        safe_message = _redact_secrets(message)[:1000]
        entry = {
            "ts": _now_iso(),
            "component": str(component or "")[:80],
            "message": safe_message,
            "signature": self.signature(component, safe_message),
            "context": {k: _redact_secrets(str(v))[:200] for k, v in (context or {}).items()},
        }
        async with self._lock:
            self._entries.append(entry)
            await asyncio.to_thread(self._persist)

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._entries)[-int(limit):][::-1]

    async def recurring(self, min_count: int = 2, limit: int = 10) -> list[dict[str, Any]]:
        """Signatures seen >= min_count times, most frequent first, with a
        representative message and the latest timestamp."""
        async with self._lock:
            groups: dict[str, dict[str, Any]] = {}
            for e in self._entries:
                sig = e.get("signature", "")
                g = groups.setdefault(sig, {"signature": sig, "count": 0, "message": e.get("message", ""),
                                            "component": e.get("component", ""), "last_ts": e.get("ts", "")})
                g["count"] += 1
                g["message"] = e.get("message", g["message"])  # keep latest concrete message
                g["last_ts"] = e.get("ts", g["last_ts"])
        out = [g for g in groups.values() if g["count"] >= min_count]
        out.sort(key=lambda g: g["count"], reverse=True)
        return out[:limit]
