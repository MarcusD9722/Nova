from __future__ import annotations

"""Computer control — observe + propose-only, permission-gated (Phase 8).

Deliberately conservative. Every action routes through the PermissionBroker, and
even an *allowed* action does not actually touch the machine unless BOTH:
  1. `NOVA_COMPUTER_CONTROL=1` is set (off by default), and
  2. a real platform adapter is installed.
With neither, actions are approved-but-not-executed (an honest dry run) — there
is no armed autonomous actuator here.

A Windows adapter now exists (`core/computer_windows.py`), so the flag above is
the switch it always claimed to be rather than a no-op. The DEFAULT is
unchanged: the flag is off, so a permitted action is still a dry run.

Observation is gated separately, by `NOVA_COMPUTER_OBSERVE`, and also defaults
off. It has to be: `computer.observe` is READ tier and therefore auto-allowed in
every policy mode including `locked`, so without its own gate the act of
installing an adapter would have turned silent window enumeration on. Window
titles name documents, messages and accounts — looking is a separate decision
from acting, not a lesser one.

The adapter interface (both methods optional):
    observe(what: str) -> dict
    execute(kind: str, target: str, details: dict) -> dict
Either may raise `AdapterRefusal` to decline on its own account; that is
reported distinctly from a crash and from a permission denial.
Adapters are the ONLY place real OS input lives.
"""

import os
import sys
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


_TRUTHY = {"1", "true", "yes", "on"}


class AdapterRefusal(RuntimeError):
    """An adapter declined — not a crash, and not a permission decision.

    Raised for something the adapter will not do at all (a CRITICAL action it
    deliberately omits) or will not do here (input aimed at a protected window,
    a failed focus assertion, an off-screen coordinate). Kept in this module
    rather than in an adapter because it is part of the adapter CONTRACT, and
    because core/computer_windows.py cannot be imported on a non-Windows
    machine, so nothing neutral may depend on it.
    """


def execution_enabled() -> bool:
    return os.getenv("NOVA_COMPUTER_CONTROL", "0").strip().lower() in _TRUTHY


def observation_enabled() -> bool:
    """Whether Nova may actually look at the windows on this machine.

    Separate from `execution_enabled` on purpose — see the module docstring.
    Reading what is on screen and acting on it are different permissions, and
    the read side is the one no policy mode would otherwise stop.
    """
    return os.getenv("NOVA_COMPUTER_OBSERVE", "0").strip().lower() in _TRUTHY


def default_adapter(platform: str | None = None) -> Any | None:
    """The platform adapter for this machine, or None when none fits.

    Imported lazily and per-platform: `core.computer_windows` pulls in
    `ctypes.wintypes`, which raises outright on Linux and macOS, so a
    module-level import here would break every non-Windows boot.
    """
    plat = platform if platform is not None else sys.platform
    if plat != "win32":
        return None
    try:
        from core.computer_windows import WindowsAdapter

        return WindowsAdapter()
    except Exception as exc:  # noqa: BLE001
        # A machine that cannot bind user32 is a machine with no actuator, not
        # a machine that fails to boot. Loud, because it is surprising.
        logger.warning("computer_adapter_unavailable", platform=plat,
                       error=str(exc)[:160])
        return None


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
        if not observation_enabled():
            return {"ok": True, "available": False, "status": "observation_disabled",
                    "note": ("An observation adapter is installed, but NOVA_COMPUTER_OBSERVE "
                             "is off. Window titles name documents, messages and accounts, so "
                             "looking at the screen is opt-in separately from acting on it.")}
        try:
            return {"ok": True, "available": True, "observation": self._adapter.observe(what)}
        except AdapterRefusal as e:
            return {"ok": False, "status": "refused_by_adapter", "note": str(e)[:300]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    async def act(self, kind: str, *, target: str = "", details: dict[str, Any] | None = None,
                  wait_for_confirm: bool = True,
                  confirm_timeout_s: float = HUMAN_DECISION_TIMEOUT_S,
                  dry_run: bool = False) -> dict[str, Any]:
        """Propose an action. It runs ONLY if permission clears AND execution is
        enabled AND an adapter exists — otherwise it's an honest dry run.

        `dry_run=True` forces the execution gate closed for THIS call however
        the flags are set, for a caller that wants the permission decision
        without the effect. `skill.run` is the one that needs it: its steps are
        free-text descriptions, not keystroke payloads, so executing them as
        typed input would dump prose into whatever window had focus.
        """
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
        if dry_run or not self.available:
            if dry_run:
                reason = "the caller asked for an evaluation, not an action"
            elif not execution_enabled():
                reason = "NOVA_COMPUTER_CONTROL is off"
            else:
                reason = "no platform adapter installed"
            return {"ok": True, "status": "approved_not_executed", "action": {"kind": kind, **details},
                    "note": f"Permitted, but not executed ({reason}). This is a dry run — no input was synthesized."}
        try:
            result = self._adapter.execute(kind, target, details)
            return {"ok": True, "status": "executed", "action": {"kind": kind, **details}, "result": result}
        except AdapterRefusal as e:
            # The adapter declined. Reported apart from execution_error because
            # "I will not do this" and "I tried and it broke" are different
            # answers, and collapsing them would hide a working safety guard
            # behind what looks like a bug.
            logger.info("computer_action_refused", kind=kind, note=str(e)[:160])
            return {"ok": False, "status": "refused_by_adapter",
                    "action": {"kind": kind, **details}, "note": str(e)[:300]}
        except Exception as e:  # noqa: BLE001
            logger.warning("computer_action_failed", kind=kind, error=str(e)[:160])
            return {"ok": False, "status": "execution_error", "error": str(e)[:200]}
