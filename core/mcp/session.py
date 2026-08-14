from __future__ import annotations

"""One MCP server connection: stdio JSON-RPC, bounded and disposable.

Local stdio first, deliberately. It is the transport every MCP server supports,
it needs no network, and it keeps "Nova can use MCP" from implying "Nova needs
internet". `Transport` is the seam where an HTTP/SSE transport slots in later
without the registry or the tool layer noticing.

Every failure mode here is assumed to happen, because a server is somebody
else's process: it can be missing, crash on launch, hang, return malformed
JSON, return a 40 MB result, advertise a protocol Nova does not speak, or
disappear mid-conversation. None of that may destabilise Nova — a broken server
degrades to "that capability is unavailable", which the tool layer already knows
how to say honestly.
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

#: MCP revisions Nova knows how to speak. Sent as preferred; a server answering
#: with something else is recorded and refused rather than guessed at.
SUPPORTED_PROTOCOL = "2024-11-05"

#: A single JSON-RPC frame. A server that sends more than this is either broken
#: or hostile; either way Nova will not buffer it.
MAX_FRAME_BYTES = int(os.getenv("NOVA_MCP_MAX_FRAME", str(4 * 1024 * 1024)))

DEFAULT_STARTUP_TIMEOUT = float(os.getenv("NOVA_MCP_STARTUP_TIMEOUT", "20"))
DEFAULT_CALL_TIMEOUT = float(os.getenv("NOVA_MCP_CALL_TIMEOUT", "30"))


class McpError(RuntimeError):
    """An MCP server failed. Carries the reason, never a guess."""


@dataclass
class ServerConfig:
    server_id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT

    @classmethod
    def from_dict(cls, server_id: str, raw: dict[str, Any]) -> "ServerConfig":
        return cls(
            server_id=str(server_id),
            command=str(raw.get("command") or ""),
            args=[str(a) for a in (raw.get("args") or [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=(str(raw["cwd"]) if raw.get("cwd") else None),
            enabled=bool(raw.get("enabled", True)),
            startup_timeout_s=float(raw.get("startup_timeout_s", DEFAULT_STARTUP_TIMEOUT)),
            call_timeout_s=float(raw.get("call_timeout_s", DEFAULT_CALL_TIMEOUT)),
        )


class Transport:
    """Minimal duplex JSON-RPC channel. Implemented by stdio; an HTTP/SSE
    transport can satisfy the same three methods."""

    async def start(self) -> None: ...
    async def send(self, payload: dict) -> None: ...
    async def receive(self, timeout: float) -> dict | None: ...
    async def close(self) -> None: ...
    def is_alive(self) -> bool: ...


class StdioTransport(Transport):
    """Newline-delimited JSON over a child process's stdin/stdout."""

    def __init__(self, cfg: ServerConfig) -> None:
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        exe = shutil.which(self._cfg.command) or self._cfg.command
        env = {**os.environ, **self._cfg.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *self._cfg.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr is captured, not inherited: a chatty server must not
                # scribble over Nova's own console output.
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cfg.cwd, env=env,
                # asyncio's StreamReader defaults to a 64 KB line limit, and MCP
                # frames are newline-delimited JSON — so a perfectly legitimate
                # large result would blow up readline() and kill the connection
                # rather than being read and capped. Raise the reader limit to
                # Nova's own frame cap so oversized frames are REJECTED by the
                # explicit check below, with a clear reason, instead of
                # surfacing as an opaque stream error.
                limit=MAX_FRAME_BYTES,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as e:
            raise McpError(f"cannot start MCP server {self._cfg.server_id!r}: "
                           f"{type(e).__name__}: {e}") from e

    async def send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("transport not started")
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def receive(self, timeout: float) -> dict | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            raise McpError(f"MCP server {self._cfg.server_id!r} timed out after {timeout:.0f}s")
        if not raw:
            return None            # clean EOF: the server exited
        if len(raw) > MAX_FRAME_BYTES:
            raise McpError(f"MCP server {self._cfg.server_id!r} sent an oversized frame "
                           f"({len(raw)} bytes > {MAX_FRAME_BYTES})")
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise McpError(f"MCP server {self._cfg.server_id!r} sent malformed JSON: {e}") from e
        if not isinstance(msg, dict):
            raise McpError(f"MCP server {self._cfg.server_id!r} sent a non-object frame")
        return msg

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


class McpSession:
    """A connected MCP server: handshake, tool listing, tool calls.

    Sequential by design. MCP servers are frequently single-threaded scripts,
    and one in-flight request per server is both safe and enough — Nova calls
    at most one tool at a time per server anyway.
    """

    def __init__(self, cfg: ServerConfig, transport: Transport | None = None) -> None:
        self.cfg = cfg
        self._transport = transport or StdioTransport(cfg)
        self._next_id = 0
        self._lock = asyncio.Lock()
        self.state = "disconnected"     # disconnected | ready | failed
        self.last_error: str | None = None
        self.server_info: dict[str, Any] = {}
        self.protocol: str | None = None

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> bool:
        """Start and handshake. Never raises; returns readiness."""
        async with self._lock:
            try:
                await self._transport.start()
                result = await self._request("initialize", {
                    "protocolVersion": SUPPORTED_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "nova", "version": "3.0"},
                }, timeout=self.cfg.startup_timeout_s)

                self.protocol = str(result.get("protocolVersion") or "")
                self.server_info = dict(result.get("serverInfo") or {})
                if self.protocol and self.protocol != SUPPORTED_PROTOCOL:
                    # Recorded, not silently accepted. Guessing at an unknown
                    # revision is how a client starts misreading payloads.
                    logger.warning("mcp_protocol_mismatch", server=self.cfg.server_id,
                                   got=self.protocol, expected=SUPPORTED_PROTOCOL)
                    self.state = "failed"
                    self.last_error = (f"unsupported MCP protocol {self.protocol!r} "
                                       f"(Nova speaks {SUPPORTED_PROTOCOL})")
                    await self._transport.close()
                    return False

                await self._notify("notifications/initialized", {})
                self.state = "ready"
                self.last_error = None
                return True
            except Exception as e:  # noqa: BLE001
                self.state = "failed"
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("mcp_connect_failed", server=self.cfg.server_id,
                               error=self.last_error[:200])
                try:
                    await self._transport.close()
                except Exception:  # noqa: BLE001
                    pass
                return False

    async def close(self) -> None:
        self.state = "disconnected"
        await self._transport.close()

    def is_alive(self) -> bool:
        return self.state == "ready" and self._transport.is_alive()

    # -- protocol -------------------------------------------------------------

    async def _request(self, method: str, params: dict, *, timeout: float) -> dict:
        self._next_id += 1
        req_id = self._next_id
        await self._transport.send({"jsonrpc": "2.0", "id": req_id,
                                    "method": method, "params": params})
        # Skip notifications and any response that is not ours; a server is
        # allowed to emit log notifications between request and reply.
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise McpError(f"MCP {method} timed out after {timeout:.0f}s")
            msg = await self._transport.receive(remaining)
            if msg is None:
                raise McpError(f"MCP server {self.cfg.server_id!r} closed the connection")
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                err = msg["error"] or {}
                raise McpError(f"MCP {method} failed: {err.get('code')} "
                               f"{str(err.get('message'))[:200]}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {"result": result}

    async def _notify(self, method: str, params: dict) -> None:
        await self._transport.send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- operations -----------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        """Raw tool descriptors. Sanitising happens in the registry, so this
        stays a faithful record of what the server actually said."""
        async with self._lock:
            if not self.is_alive():
                raise McpError(f"MCP server {self.cfg.server_id!r} is not connected")
            out = await self._request("tools/list", {}, timeout=self.cfg.call_timeout_s)
        tools = out.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict, *,
                        timeout: float | None = None) -> dict:
        async with self._lock:
            if not self.is_alive():
                raise McpError(f"MCP server {self.cfg.server_id!r} is not connected")
            return await self._request(
                "tools/call", {"name": name, "arguments": arguments or {}},
                timeout=timeout or self.cfg.call_timeout_s)

    def status(self) -> dict[str, Any]:
        return {
            "server_id": self.cfg.server_id,
            "state": self.state,
            "alive": self._transport.is_alive(),
            "protocol": self.protocol,
            "server_info": self.server_info,
            "last_error": self.last_error,
        }
