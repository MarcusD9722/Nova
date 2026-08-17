from __future__ import annotations

"""Computer control — observe + propose-only, permission-gated (Phase 8).

Deliberately conservative. Every action routes through the PermissionBroker, and
even an *allowed* action does not actually touch the machine unless BOTH:
  1. `NOVA_COMPUTER_CONTROL=1` is set (off by default), and
  2. a real platform adapter is installed.
With neither, actions are approved-but-not-executed (an honest dry run) — there
is no armed autonomous actuator here. Observation is read-tier but still returns
an honest "no adapter" rather than pretending to see the screen.

The adapter interface (both methods optional):
    observe(what: str) -> dict
    execute(kind: str, target: str, details: dict) -> dict
Adapters are the ONLY place real OS input would live; none ships by default.
"""

import os
from typing import Any

from core.logging_setup import get_logger
from core.permissions import HUMAN_DECISION_TIMEOUT_S, PermissionBroker

logger = get_logger(__name__)

# Map a requested action kind to the permission capability it costs.
ACTION_CAPABILITY = {
    "click": "computer.click",
    "double_click": "computer.click",
    "type": "computer.type",
    "scroll": "computer.scroll",
    "key": "computer.type",
    "write_clipboard": "computer.write_clipboard",
    "launch_app": "computer.launch_app",
    "close_app": "computer.close_app",
    "file_write": "computer.file_write",
    "system_setting": "computer.system_setting",
    "delete": "computer.delete",
}


def execution_enabled() -> bool:
    return os.getenv("NOVA_COMPUTER_CONTROL", "0").strip().lower() in {"1", "true", "yes", "on"}


class ComputerControl:
    def __init__(self, broker: PermissionBroker, *, adapter: Any | None = None) -> None:
        self._broker = broker
        self._adapter = adapter

    @property
    def available(self) -> bool:
        """True only when actions could actually execute (flag + adapter)."""
        return execution_enabled() and self._adapter is not None

    async def observe(self, what: str = "windows") -> dict[str, Any]:
        """Read-tier observation. Honest when no adapter can actually look."""
        decision = await self._broker.request("computer.observe", details={"what": what})
        if decision["decision"] != "allowed":
            return {"ok": False, "status": decision["decision"], **decision}
        if self._adapter is None or not hasattr(self._adapter, "observe"):
            return {"ok": True, "available": False,
                    "note": "No computer-observation adapter is installed — Nova can't enumerate windows/apps here."}
        try:
            return {"ok": True, "available": True, "observation": self._adapter.observe(what)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    async def act(self, kind: str, *, target: str = "", details: dict[str, Any] | None = None,
                  wait_for_confirm: bool = True,
                  confirm_timeout_s: float = HUMAN_DECISION_TIMEOUT_S) -> dict[str, Any]:
        """Propose an action. It runs ONLY if permission clears AND execution is
        enabled AND an adapter exists — otherwise it's an honest dry run."""
        details = dict(details or {})
        details["target"] = target
        capability = ACTION_CAPABILITY.get(kind)
        if capability is None:
            return {"ok": False, "status": "unknown_action",
                    "note": f"Unknown action '{kind}' — refusing rather than guessing an intent."}

        decision = await self._broker.request(capability, details=details)
        d = decision["decision"]

        if d == "denied":
            return {"ok": False, "status": "denied", "reason": decision.get("reason")}

        if d == "needs_confirmation":
            if not wait_for_confirm:
                return {"ok": True, "status": "needs_confirmation", "request_id": decision["request_id"],
                        "note": decision.get("note")}
            approved = await self._broker.await_decision(decision["request_id"], timeout_s=confirm_timeout_s)
            if not approved:
                return {"ok": False, "status": "not_approved",
                        "note": "Marcus didn't approve the action (declined or timed out) — nothing was done."}

        # Cleared to run (allowed outright, or approved). Now the execution gate.
        if not self.available:
            reason = "NOVA_COMPUTER_CONTROL is off" if not execution_enabled() else "no platform adapter installed"
            return {"ok": True, "status": "approved_not_executed", "action": {"kind": kind, **details},
                    "note": f"Permitted, but not executed ({reason}). This is a dry run — no input was synthesized."}
        try:
            result = self._adapter.execute(kind, target, details)
            return {"ok": True, "status": "executed", "action": {"kind": kind, **details}, "result": result}
        except Exception as e:  # noqa: BLE001
            logger.debug("computer_action_failed", error=str(e)[:160])
            return {"ok": False, "status": "execution_error", "error": str(e)[:200]}
