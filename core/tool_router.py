from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.event_bus import BUS, clip
from core.logging_setup import get_logger
from core.permissions import HUMAN_DECISION_TIMEOUT_S


logger = get_logger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    name: str
    ok: bool
    result: Any | None
    error: str | None


AsyncTool = Callable[[dict[str, Any]], Awaitable[Any]]


#: Default execution budget for an ordinary tool — a web call, a lookup, a file
#: edit. Callers may still pass their own, but a tool that needs longer declares
#: it at REGISTRATION rather than relying on every call site to remember.
DEFAULT_TOOL_TIMEOUT_S = 25.0

#: Room for a permission-gated tool to actually do its work AFTER the human says
#: yes: move a folder, write the trash manifest, return a summary.
PERMISSION_EXECUTION_ALLOWANCE_S = 20.0

#: A tool that blocks on a human clicking Approve needs the broker's whole
#: window PLUS that allowance. Derived from the broker's own constant rather than
#: hard-coded, so the two can never drift back apart — which is exactly the
#: defect this exists to prevent:
#:
#:   the tool was abandoned at 25s and reported ok=False with an EMPTY error;
#:   the human then clicked Approve inside the advertised 120s window;
#:   `resolve()` returned False, nothing ran, and the audit log recorded
#:   "approved" for an action that never happened.
PERMISSION_TOOL_TIMEOUT_S = HUMAN_DECISION_TIMEOUT_S + PERMISSION_EXECUTION_ALLOWANCE_S


class ToolRouter:
    def __init__(self, tools: dict[str, AsyncTool], descriptions: dict[str, str] | None = None):
        self._tools = dict(tools)
        self._descriptions = dict(descriptions or {})
        #: name -> execution budget. ONE authoritative timeout per tool, owned by
        #: the router, so there is no second conflicting number at a call site.
        self._timeouts: dict[str, float] = {}

    def timeout_for(self, name: str, fallback: float | None = None) -> float:
        """The authoritative execution budget for `name`."""
        declared = self._timeouts.get(str(name))
        if declared is not None:
            return float(declared)
        return float(DEFAULT_TOOL_TIMEOUT_S if fallback is None else fallback)

    def set_timeout(self, name: str, timeout_s: float) -> None:
        self._timeouts[str(name)] = float(timeout_s)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def describe_tools(self) -> dict[str, str]:
        """name -> human/LLM-readable description (used for function calling)."""
        return {name: self._descriptions.get(name, "") for name in self.list_tools()}

    def register(self, name: str, fn: AsyncTool, description: str = "",
                 timeout_s: float | None = None) -> None:
        """Register an additional tool after construction (e.g. project builder).

        `timeout_s` declares this tool's authoritative budget. Pass it for any
        tool that waits on a human — the registration is the only place that
        knows, and it then overrides whatever generic value a call site supplies.
        """
        self._tools[str(name)] = fn
        if description:
            self._descriptions[str(name)] = description
        if timeout_s is not None:
            self.set_timeout(name, timeout_s)

    async def execute(self, call: ToolCall, timeout_s: float | None = None, retries: int = 1) -> ToolResult:
        if call.name not in self._tools:
            BUS.publish("tool.error", {"tool": call.name, "error": "unknown_tool"})
            return ToolResult(name=call.name, ok=False, result=None, error=f"Unknown tool: {call.name}")

        # A timeout DECLARED for this tool wins over whatever a call site passed.
        # Call sites are generic loops that do not know a tool waits on a human;
        # the registration does. Without this precedence a supervisor's blanket
        # 30s would keep amputating the 120s approval handshake.
        budget = self.timeout_for(call.name, timeout_s)

        BUS.publish("tool.started", {"tool": call.name})
        last_err: Exception | None = None
        last_text = ""
        attempts = retries + 1
        for attempt in range(attempts):
            try:
                coro = self._tools[call.name](call.args)
                result = await asyncio.wait_for(coro, timeout=budget)
                BUS.publish("tool.result", {"tool": call.name, "ok": True, "summary": clip(result, 200)})
                return ToolResult(name=call.name, ok=True, result=result, error=None)
            except Exception as e:  # noqa: BLE001
                last_err = e
                last_text = self._error_text(e, call.name, budget, attempt, attempts)
                logger.warning("tool_failed", tool=call.name, attempt=attempt, error=last_text)
                # Back off only if another attempt is actually coming. The sleep
                # used to run after the LAST failure too, adding 0.2s of dead wait
                # to every failing tool call — including the retries=0 path the
                # agent loop uses, where there is nothing to back off from.
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.2 * (attempt + 1))

        # A "not configured" state (missing API key, OAuth never run) is not a
        # code defect: publish it as tool.not_configured so the self-improve
        # loop never files a bogus code-fix proposal for it (observed live:
        # an auto-"fix" proposed for google_oauth_setup.py because the honest
        # "account not connected" error recurred). Matched by class name to
        # keep core/ free of a plugins/ import.
        if type(last_err).__name__ == "PluginConfigError":
            BUS.publish("tool.not_configured", {"tool": call.name, "error": clip(last_text, 200)})
        else:
            BUS.publish("tool.error", {"tool": call.name, "error": clip(last_text, 200) or "tool_failed"})
        return ToolResult(name=call.name, ok=False, result=None, error=last_text or "tool_failed")

    @staticmethod
    def _error_text(exc: BaseException, name: str, budget: float,
                    attempt: int, attempts: int) -> str:
        """A failure explanation that is NEVER empty.

        `asyncio.TimeoutError` carries no message, so `str(exc)` on the most
        common tool failure was the empty string. That propagated all the way to
        the caller as `ok=False, error=""` — a failure the model was then asked to
        explain to Marcus with nothing to explain it from, and which looked
        identical to success-with-no-output in the logs. Every branch below
        produces text, and the timeout branch names the budget it exceeded so the
        number is diagnosable instead of mysterious.
        """
        of_n = f" (attempt {attempt + 1} of {attempts})" if attempts > 1 else ""
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return (f"'{name}' timed out after {budget:g}s{of_n}. It was cancelled "
                    f"mid-flight, so any work it had started may be incomplete.")
        if isinstance(exc, asyncio.CancelledError):
            return f"'{name}' was cancelled before it finished{of_n}."
        detail = str(exc).strip()
        if detail:
            return f"{type(exc).__name__}: {detail}"
        return f"{type(exc).__name__} raised by '{name}' with no message{of_n}."
