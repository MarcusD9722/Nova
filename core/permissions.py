from __future__ import annotations

"""Permission tiers, broker, and audit log (Goal — Phase 8 foundation).

This is the gate EVERY actuator must pass before Nova touches anything beyond
reading. It ships first, on purpose: computer control and learned-skill
execution route through here, so there is one place that decides whether an
action is auto-allowed, needs Marcus's explicit confirmation, or is denied — and
one append-only audit trail of every request and outcome.

Design:
- Four tiers by escalating risk: READ < STANDARD < ADMIN < CRITICAL.
- A policy MODE maps each tier to a decision. Default is `guarded`
  (conservative): only READ is silent; STANDARD/ADMIN need confirmation;
  CRITICAL is denied outright. `locked` denies everything but READ; `trusted`
  auto-allows through STANDARD. Nothing auto-allows ADMIN or CRITICAL except by
  explicit human confirmation, ever.
- `evaluate` is a pure function (fully tested). The async `PermissionBroker`
  handles the confirmation handshake (a pending future the UI resolves) and
  writes the audit trail.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.event_bus import BUS, clip
from core.logging_setup import get_logger

logger = get_logger(__name__)

# Tiers (escalating risk).
READ, STANDARD, ADMIN, CRITICAL = 0, 1, 2, 3
TIER_NAMES = {READ: "read", STANDARD: "standard", ADMIN: "admin", CRITICAL: "critical"}
TIER_BY_NAME = {v: k for k, v in TIER_NAMES.items()}

# What each known capability costs. Anything unknown defaults to ADMIN (safe:
# an unrecognized action is treated as needing confirmation, never auto-run).
CAPABILITY_TIERS: dict[str, int] = {
    "computer.observe": READ,          # list windows/apps, read state
    "computer.read_clipboard": READ,
    "computer.click": STANDARD,
    "computer.type": STANDARD,
    "computer.scroll": STANDARD,
    "computer.write_clipboard": STANDARD,
    "computer.launch_app": ADMIN,
    "computer.close_app": ADMIN,
    "computer.file_write": ADMIN,
    "computer.system_setting": CRITICAL,
    "computer.delete": CRITICAL,
    "skill.run": STANDARD,             # a learned workflow (its steps re-checked individually)
    # Projects. Deleting is RECOVERABLE (the folder moves to projects/.trash/),
    # so it needs confirmation but is allowed in the default 'guarded' mode.
    # Purging erases bytes for good, so it sits at CRITICAL — denied by default,
    # and requires deliberately raising NOVA_PERMISSION_MODE to allow it.
    "project.restore": STANDARD,
    "project.delete": ADMIN,
    "project.purge": CRITICAL,
}

DECISION_ALLOW, DECISION_CONFIRM, DECISION_DENY = "allow", "confirm", "deny"

#: How long a pending request waits for a human. THE authoritative number for the
#: approval window: `core/tool_router.py` derives a permission-gated tool's
#: execution budget from it rather than hard-coding a second one, so a tool can
#: never be abandoned before the window it advertised to Marcus has closed.
HUMAN_DECISION_TIMEOUT_S = 120.0

# Per-mode: the HIGHEST tier that is auto-allowed, and the highest that may be
# confirmed. Above the confirm ceiling → denied.
_MODES = {
    "locked":  {"auto_max": READ,     "confirm_max": READ},      # only reading
    "guarded": {"auto_max": READ,     "confirm_max": ADMIN},     # default
    "trusted": {"auto_max": STANDARD, "confirm_max": CRITICAL},
}
DEFAULT_MODE = "guarded"


def tier_of(capability: str) -> int:
    return CAPABILITY_TIERS.get(capability, ADMIN)


def normalize_mode(mode: str | None) -> str:
    m = (mode or "").strip().lower()
    return m if m in _MODES else DEFAULT_MODE


def evaluate(capability: str, *, mode: str = DEFAULT_MODE) -> str:
    """Pure decision for a capability under a policy mode: allow / confirm / deny."""
    m = _MODES[normalize_mode(mode)]
    tier = tier_of(capability)
    if tier <= m["auto_max"]:
        return DECISION_ALLOW
    if tier <= m["confirm_max"]:
        return DECISION_CONFIRM
    return DECISION_DENY


class PermissionBroker:
    """Async gate + audit trail. `request()` returns an immediate decision or a
    pending request the UI resolves via `resolve()`; every outcome is audited."""

    def __init__(self, *, mode: str = DEFAULT_MODE, audit_path: Path | None = None) -> None:
        self._mode = normalize_mode(mode)
        self._audit_path = audit_path
        self._pending: dict[str, asyncio.Future] = {}
        self._recent: list[dict[str, Any]] = []
        #: request_id -> how it finally settled ("approved", "rejected",
        #: "timeout", "abandoned", "cancelled"). Kept so a LATE answer can be
        #: audited as what it actually was instead of as a fresh approval. Ids
        #: only, never details, and bounded.
        self._settled: dict[str, str] = {}
        self._recover_from_audit()

    #: How far back a new process reads its own trail. The file is append-only
    #: and grows for the life of the machine; the requests a person could still
    #: click on are at the end of it.
    _RECOVER_TAIL = 2000

    #: Outcomes that END a request. `late_approval_ignored`, `unknown_request`
    #: and friends are entries ABOUT a late answer, not endings, so reading them
    #: as one would overwrite the real ending with a footnote to it.
    _ENDINGS = frozenset({"approved", "rejected", "timeout", "cancelled",
                          "abandoned", "already_settled", "interrupted_by_restart"})

    def _recover_from_audit(self) -> None:
        """Learn from the durable trail how earlier lives' requests ended.

        Everything about a permission request except the audit line is in
        memory: the pending future, the answer, how it finished. A restart
        erases all of it, and the old broker then had exactly one thing to say
        about any id from before — "no request with this id was ever pending".
        For a request that WAS pending, recorded on disk one line up, that is
        not a gap; it is a false statement in a security log. It also left
        `settled_as` empty, so a user who had DECLINED something was told only
        that their click did nothing, never that their refusal still stood.

        So a new broker reads the tail of its own trail and rebuilds two things:

          * how each request ENDED, so a late click is audited as the late
            answer to a decided request, and `settled_as` can still say which
            decision it was;
          * which requests ended NOWHERE — the ones the process was still
            holding when it died. Those get one terminal entry of their own,
            because a request left `pending` for ever reads as still waiting,
            and "still waiting" is the one thing it certainly is not.

        Nothing here can make an action runnable. `_pending` stays empty: no
        future is restored, so `resolve()` cannot approve any of these and
        `pending()` has nothing to offer. This is only about what is TRUE
        afterwards. It assumes one Nova per memory directory — the same
        assumption startup already makes when it closes out in-flight work.
        """
        path = self._audit_path
        if path is None:
            return
        try:
            if not path.exists():
                return
            lines = path.read_text(encoding="utf-8").splitlines()[-self._RECOVER_TAIL:]
        except OSError as e:  # noqa: BLE001
            logger.debug("permission_audit_read_failed", error=str(e)[:160])
            return

        rows: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A crash mid-write can tear the last line. One unreadable entry
                # is not a reason to abandon the rest of the history.
                continue
            if isinstance(row, dict):
                rows.append(row)

        # What `audit_log()` shows now spans lives, so the endpoint that exists
        # to show the trail stops disagreeing with the file it is written to.
        self._recent = rows[-500:]

        opened: dict[str, dict[str, Any]] = {}
        ended: dict[str, str] = {}
        for row in rows:
            rid = str(row.get("request_id") or "")
            if not rid:
                continue                      # allow/deny are decided, not tracked
            outcome = str(row.get("outcome") or "")
            if outcome == "pending":
                opened[rid] = row
                ended.pop(rid, None)
            elif outcome in self._ENDINGS:
                ended[rid] = outcome
        for rid, outcome in ended.items():
            self._settle(rid, outcome)
        for rid, row in opened.items():
            if rid in ended:
                continue
            self._settle(rid, "interrupted_by_restart")
            self._audit({"outcome": "interrupted_by_restart", "request_id": rid,
                         "capability": str(row.get("capability") or ""),
                         "reason": "Nova stopped before this was answered; "
                                   "nothing was executed, and it can no longer "
                                   "be approved"})

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = normalize_mode(mode)
        BUS.publish("permission.mode_changed", {"mode": self._mode})

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _audit(self, entry: dict[str, Any]) -> None:
        entry = {"ts": self._now(), **entry}
        self._recent.append(entry)
        self._recent = self._recent[-500:]
        BUS.publish("permission.audit", {k: clip(str(v), 160) for k, v in entry.items()})
        if self._audit_path is not None:
            try:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self._audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:  # noqa: BLE001
                logger.debug("permission_audit_write_failed", error=str(e)[:160])

    async def request(self, capability: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask to perform a capability. Returns {decision, request_id?}:
        - 'allowed'  → proceed now (audited).
        - 'needs_confirmation' → a pending request_id; await_decision() or the UI
          resolves it. Denied by default if never confirmed.
        - 'denied'   → not permitted under the current mode."""
        decision = evaluate(capability, mode=self._mode)
        tier = tier_of(capability)
        base = {"capability": capability, "tier": TIER_NAMES[tier], "details": details or {}}

        if decision == DECISION_ALLOW:
            self._audit({**base, "outcome": "allowed"})
            return {"decision": "allowed"}
        if decision == DECISION_DENY:
            self._audit({**base, "outcome": "denied", "reason": f"tier '{TIER_NAMES[tier]}' not permitted in '{self._mode}' mode"})
            return {"decision": "denied", "reason": f"'{capability}' requires a higher permission mode than '{self._mode}'."}

        request_id = uuid4().hex[:12]
        self._pending[request_id] = asyncio.get_event_loop().create_future()
        self._audit({**base, "outcome": "pending", "request_id": request_id})
        BUS.publish("permission.requested", {"request_id": request_id, "capability": capability,
                                             "tier": TIER_NAMES[tier], "details": details or {}})
        return {"decision": "needs_confirmation", "request_id": request_id,
                "note": f"'{capability}' needs your explicit approval before it runs."}

    def _settle(self, request_id: str, how: str) -> None:
        self._settled[str(request_id)] = how
        if len(self._settled) > 500:
            for rid in list(self._settled)[: len(self._settled) - 500]:
                self._settled.pop(rid, None)

    def _close_out(self, request_id: str, outcome: str, reason: str) -> bool:
        """Drop a pending request that will never run, and say so once.

        Removing it from `_pending` is the security-relevant half, and it is the
        half that actually protects anything: a request left pending is still
        listed by `pending()` and still resolvable, so a click minutes later would
        return True for an action whose caller is long gone.

        `permission.expired` is published for whatever consumes the bus. Be precise
        about what that buys today: as of this commit NOTHING in frontend/src
        consumes `permission.requested` or `permission.expired` — there is no
        approval UI to withdraw a button from. The event is emitted so a future
        one can be correct; the correctness here does not depend on it, because
        `resolve()` refuses a settled request regardless of what any client shows.
        """
        fut = self._pending.pop(str(request_id), None)
        if fut is None:
            return False
        if not fut.done():
            # DENY the waiter, do not cancel it. Cancelling the future made
            # `await_decision` raise CancelledError into a tool that was waiting
            # perfectly normally, which propagated through `ToolRouter.execute`
            # (CancelledError is a BaseException, so the router never catches it)
            # and killed the turn instead of returning "not approved".
            #
            # Withdrawing a request means it will not be approved, and that is
            # exactly what False says. The caller gets a clean result.
            fut.set_result(False)
        self._settle(request_id, outcome)
        self._audit({"outcome": outcome, "request_id": str(request_id), "reason": reason})
        BUS.publish("permission.expired", {"request_id": str(request_id), "outcome": outcome})
        return True

    def cancel(self, request_id: str, *, reason: str = "the caller stopped waiting") -> bool:
        """Withdraw a pending request whose caller no longer wants it."""
        return self._close_out(request_id, "cancelled", reason)

    def resolve(self, request_id: str, approved: bool, *, by: str = "user") -> bool:
        """The human answers a pending request. Returns False if unknown/settled.

        The audit entry describes WHAT HAPPENED, not what was clicked. The old
        version audited "approved" before checking whether the request was still
        live, so a timed-out request that Marcus then approved left an audit trail
        saying `approved` for an action that never ran — the log implied a
        deletion that did not happen, and would equally have hidden one that did.
        """
        rid = str(request_id)
        answer = "approved" if approved else "rejected"
        # The ignored-answer outcome is a NOUN ("late_approval_ignored"), distinct
        # from the successful verb ("approved"), so no substring match can confuse
        # the two when reading the audit trail.
        ignored = "late_approval_ignored" if approved else "late_rejection_ignored"
        fut = self._pending.pop(rid, None)

        if fut is None:
            prior = self._settled.get(rid)
            if prior:
                self._audit({"outcome": ignored, "request_id": rid,
                             "by": by, "answered": answer, "settled_as": prior,
                             "reason": f"the request had already ended as '{prior}'; "
                                       f"this answer arrived too late and NOTHING was executed"})
            else:
                self._audit({"outcome": "unknown_request", "request_id": rid,
                             "by": by, "answered": answer,
                             "reason": "no request with this id was ever pending"})
            return False

        if fut.done():
            self._settle(rid, "already_settled")
            self._audit({"outcome": ignored, "request_id": rid,
                         "by": by, "answered": answer, "settled_as": "already_settled",
                         "reason": "the request was already settled; this answer changed nothing"})
            return False

        self._settle(rid, answer)
        self._audit({"outcome": answer, "request_id": rid, "by": by})
        fut.set_result(bool(approved))
        return True

    async def await_decision(self, request_id: str, *,
                             timeout_s: float = HUMAN_DECISION_TIMEOUT_S) -> bool:
        """Wait for the human to resolve a pending request; False on timeout."""
        rid = str(request_id)
        fut = self._pending.get(rid)
        if fut is None:
            return False
        try:
            return bool(await asyncio.wait_for(fut, timeout=timeout_s))
        except (TimeoutError, asyncio.TimeoutError):
            self._close_out(rid, "timeout",
                            f"no answer within {timeout_s:g}s; treated as DENIED")
            return False
        except asyncio.CancelledError:
            # The tool above us was cancelled (its own timeout fired, the turn was
            # abandoned, shutdown). Cleaning up here is what stops an orphaned
            # request from being approvable later — and cancellation is re-raised
            # rather than swallowed, because reporting False would tell the caller
            # "denied" when nobody decided anything.
            self._close_out(rid, "abandoned",
                            "the caller was cancelled while waiting; nothing was executed")
            raise

    def settled_as(self, request_id: str) -> str:
        """How a request ended: approved / rejected / timeout / cancelled / ...

        The caller needs this to say what happened. "Not approved" covers a
        refusal and a silence, and those are different sentences to a person:
        one is an answer they gave, the other is one they never saw. The broker
        already records the difference; without this it simply had no way out.
        """
        return str(self._settled.get(str(request_id)) or "")

    def pending(self) -> list[dict[str, Any]]:
        return [{"request_id": rid} for rid in self._pending]

    def audit_log(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return list(reversed(self._recent[-int(limit):]))
