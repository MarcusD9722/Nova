from __future__ import annotations

"""Where MCP meets Nova's governance.

This is the only place MCP tools become callable, and it deliberately makes them
callable *as ordinary Nova tools*: registered on the existing ToolRouter, gated
by the existing PermissionBroker, classified UNTRUSTED_EXTERNAL, captured as
artifacts. There is no MCP-specific execution path, because a second path is
exactly how a security model rots.

Order of operations for every MCP call, none of it optional:

    permission check  -> execute -> sanitise result -> classify trust
                      -> capture artifact -> return

If a future change makes an MCP call that skips any of those, that is a bug no
matter how much faster it is.
"""

import json
import os
import time
from typing import Any, Callable

from core.event_bus import BUS
from core.logging_setup import get_logger
from core.mcp.registry import CapabilityRegistry, capability_id
from core.mcp.sanitize import frame_for_prompt, looks_like_injection, sanitize_text
from core.mcp.session import McpError, McpSession, ServerConfig
from memory.artifacts import TRUST_UNTRUSTED

logger = get_logger(__name__)

#: A tool result is text that goes into a prompt. Cap it for the same reason
#: descriptions are capped: an enormous result is an attack on the context
#: budget even when it is honest.
MAX_RESULT_CHARS = int(os.getenv("NOVA_MCP_MAX_RESULT_CHARS", "8000"))

CAPABILITY_SEARCH_TOOL = "capability.search"


class McpManager:
    """Owns sessions, the registry, and the bridge into Nova's tool layer."""

    def __init__(
        self,
        *,
        permission_broker: Any | None = None,
        artifact_store: Any | None = None,
        session_factory: Callable[[ServerConfig], McpSession] | None = None,
    ) -> None:
        self.registry = CapabilityRegistry()
        self._sessions: dict[str, McpSession] = {}
        self._configs: dict[str, ServerConfig] = {}
        self._permissions = permission_broker
        self._artifacts = artifact_store
        self._session_factory = session_factory or (lambda cfg: McpSession(cfg))
        self.calls = 0
        self.failures = 0
        self.denied = 0

    # -- lifecycle ------------------------------------------------------------

    async def add_server(self, cfg: ServerConfig) -> bool:
        """Connect and discover. Never raises — a bad server is a degraded
        capability, not a broken assistant."""
        if not cfg.enabled:
            return False
        self._configs[cfg.server_id] = cfg
        session = self._session_factory(cfg)
        self._sessions[cfg.server_id] = session

        if not await session.connect():
            BUS.publish("mcp.server_failed",
                        {"server": cfg.server_id, "error": (session.last_error or "")[:200]})
            return False
        return await self.refresh(cfg.server_id)

    async def refresh(self, server_id: str) -> bool:
        """Re-discover one server's tools. Called on connect and whenever a
        server signals its tool list changed."""
        session = self._sessions.get(server_id)
        if session is None or not session.is_alive():
            return False
        try:
            tools = await session.list_tools()
        except McpError as e:
            logger.warning("mcp_discovery_failed", server=server_id, error=str(e)[:200])
            self.registry.drop_server(server_id)
            BUS.publish("mcp.discovery_failed", {"server": server_id, "error": str(e)[:200]})
            return False
        delta = self.registry.replace_server(server_id, tools)
        BUS.publish("mcp.discovered", {"server": server_id, **delta})
        logger.info("mcp_discovered", server=server_id, **delta)
        return True

    async def remove_server(self, server_id: str) -> None:
        session = self._sessions.pop(server_id, None)
        self._configs.pop(server_id, None)
        self.registry.drop_server(server_id)
        if session is not None:
            await session.close()

    async def close(self) -> None:
        for server_id in list(self._sessions):
            await self.remove_server(server_id)

    async def _ensure_alive(self, server_id: str) -> bool:
        """Reconnect a dropped server on demand, and re-discover after — its
        tools may have changed while it was gone."""
        session = self._sessions.get(server_id)
        if session is None:
            return False
        if session.is_alive():
            return True
        cfg = self._configs.get(server_id)
        if cfg is None:
            return False
        BUS.publish("mcp.reconnecting", {"server": server_id})
        fresh = self._session_factory(cfg)
        self._sessions[server_id] = fresh
        if not await fresh.connect():
            self.registry.drop_server(server_id)
            return False
        await self.refresh(server_id)
        return True

    # -- execution ------------------------------------------------------------

    async def call(self, cap_id: str, args: dict[str, Any], *,
                   conversation_id: str = "", turn_id: str = "") -> dict[str, Any]:
        """Invoke one MCP capability, fully governed."""
        cap = self.registry.get(cap_id)
        if cap is None:
            # Could be a stale id from an earlier turn whose server has since
            # dropped the tool. Say so plainly rather than failing obscurely.
            return {"ok": False, "error": f"unknown MCP capability {cap_id!r} "
                                          "(the server may have removed it)"}

        # 1. Permission, BEFORE anything reaches the server.
        decision = await self._check_permission(cap)
        if decision is not None:
            self.denied += 1
            BUS.publish("mcp.denied", {"capability": cap_id, "reason": decision})
            return {"ok": False, "error": decision, "denied": True}

        if not await self._ensure_alive(cap.server_id):
            self.failures += 1
            return {"ok": False, "error": f"MCP server {cap.server_id!r} is unavailable"}

        session = self._sessions[cap.server_id]
        started = time.perf_counter()
        try:
            raw = await session.call_tool(cap.remote_name, args)
        except McpError as e:
            self.failures += 1
            BUS.publish("mcp.call_failed", {"capability": cap_id, "error": str(e)[:200]})
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        elapsed_ms = (time.perf_counter() - started) * 1000
        self.calls += 1

        text, flagged, is_error = self._normalise_result(raw)
        artifact_id = self._capture(cap, args, text, conversation_id, turn_id, flagged)

        BUS.publish("mcp.called", {"capability": cap_id, "ms": round(elapsed_ms, 1),
                                   "chars": len(text), "flagged": flagged})
        return {
            "ok": not is_error,
            # Framed so the model reads it as quoted external data. The context
            # firewall gets the same signal via trust; this is the belt to that
            # pair of braces, since the text lands in a prompt either way.
            "result": frame_for_prompt(cap.server_id, text) if text else "",
            "raw_chars": len(text),
            "trust": TRUST_UNTRUSTED,
            "server": cap.server_id,
            "capability": cap_id,
            "artifact_id": artifact_id,
            "injection_flagged": flagged,
            "elapsed_ms": round(elapsed_ms, 1),
        }

    async def _check_permission(self, cap) -> str | None:
        """None = allowed. A string = the refusal, already user-readable."""
        if self._permissions is None:
            # No broker wired (tests, headless). Fail CLOSED for anything a
            # server did not explicitly mark read-only — an unrecognised remote
            # action must never auto-run just because governance is absent.
            if cap.destructive or not cap.read_only:
                return (f"permission required for {cap.cap_id} and no permission "
                        "broker is available")
            return None
        try:
            verdict = await self._permissions.request(
                cap.permission_capability,
                details={"server": cap.server_id, "tool": cap.remote_name,
                         "destructive": cap.destructive, "read_only": cap.read_only},
            )
        except Exception as e:  # noqa: BLE001
            return f"permission check failed for {cap.cap_id}: {type(e).__name__}: {e}"
        if isinstance(verdict, dict) and verdict.get("allowed"):
            return None
        reason = ""
        if isinstance(verdict, dict):
            reason = str(verdict.get("reason") or verdict.get("decision") or "")
        return f"permission denied for {cap.cap_id}" + (f": {reason}" if reason else "")

    def _normalise_result(self, raw: dict) -> tuple[str, bool, bool]:
        """MCP results are a content-block list. Flatten to bounded text."""
        is_error = bool(raw.get("isError"))
        content = raw.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for block in content[:64]:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    parts.append(str(block.get("text") or ""))
                elif kind in {"image", "audio"}:
                    parts.append(f"[{kind} content omitted]")
                elif kind == "resource":
                    res = block.get("resource") or {}
                    parts.append(str(res.get("text") or f"[resource {res.get('uri', '')}]"))
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False)[:MAX_RESULT_CHARS])

        text = "\n".join(p for p in parts if p).strip()
        flagged = looks_like_injection(text)
        text = sanitize_text(text, limit=MAX_RESULT_CHARS)
        return text, flagged, is_error

    def _capture(self, cap, args, text, conversation_id, turn_id, flagged) -> str | None:
        """Record the call as an artifact with full provenance."""
        if self._artifacts is None or not conversation_id:
            return None
        try:
            from memory.artifacts import FRESH_SESSION, Artifact
            import uuid

            art = Artifact(
                artifact_id=uuid.uuid4().hex,
                conversation_id=str(conversation_id),
                turn_id=str(turn_id),
                artifact_type="mcp_result",
                summary=f"{cap.remote_name} via {cap.server_id}",
                payload={"text": text[:2000]},
                source_tool=cap.cap_id,
                trust=TRUST_UNTRUSTED,
                freshness=FRESH_SESSION,
                provenance={"server": cap.server_id, "tool": cap.remote_name,
                            "args": {k: str(v)[:200] for k, v in (args or {}).items()},
                            "schema_hash": cap.schema_hash, "at": time.time(),
                            "injection_flagged": flagged},
            )
            self._artifacts.add(art)
            return art.artifact_id
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp_artifact_capture_failed", error=str(e)[:200])
            return None

    # -- Nova tool-layer bridge ----------------------------------------------

    def register_with_router(self, router: Any, *, context: Callable[[], tuple[str, str]] | None = None) -> int:
        """Expose every known capability as an ordinary ToolRouter tool.

        Going through the router rather than around it is what gives MCP calls
        Nova's timeout, retry policy, failure taxonomy and audit events for
        free — and what stops MCP becoming a second execution path.
        """
        registered = 0
        for cap in self.registry.all():
            router.register(cap.cap_id, self._make_tool(cap.cap_id, context),
                            cap.selector_line())
            registered += 1

        router.register(
            CAPABILITY_SEARCH_TOOL, self._make_search_tool(),
            "Search Nova's full capability registry (including MCP servers) when the "
            "tools offered do not cover what is needed. args: {query}.")
        return registered

    def _make_tool(self, cap_id: str, context):
        async def _run(args: dict[str, Any]) -> Any:
            conv, turn = context() if context else ("", "")
            return await self.call(cap_id, args or {}, conversation_id=conv, turn_id=turn)
        return _run

    def _make_search_tool(self):
        async def _run(args: dict[str, Any]) -> Any:
            query = str((args or {}).get("query") or "").strip()
            hits = self.registry.search(query)
            return {
                "query": query,
                "count": len(hits),
                # Metadata only. Schemas are hydrated after selection, so the
                # escape hatch cannot itself become a schema flood.
                "results": [{"capability": c.cap_id, "server": c.server_id,
                             "description": c.description} for c in hits],
            }
        return _run

    # -- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "servers": [s.status() for s in self._sessions.values()],
            "registry": self.registry.stats(),
            "calls": self.calls,
            "failures": self.failures,
            "denied": self.denied,
        }


def load_server_configs(raw: str | None = None) -> list[ServerConfig]:
    """Parse NOVA_MCP_SERVERS (JSON object of server-id -> config).

    Malformed configuration disables MCP loudly rather than half-starting it.
    """
    text = (raw if raw is not None else os.getenv("NOVA_MCP_SERVERS", "")).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("mcp_config_invalid", error=str(e)[:200])
        return []
    if not isinstance(data, dict):
        logger.warning("mcp_config_invalid", error="expected an object of server-id -> config")
        return []
    out = []
    for server_id, cfg in data.items():
        if isinstance(cfg, dict) and cfg.get("command"):
            out.append(ServerConfig.from_dict(server_id, cfg))
    return out
