"""Phase 8: permission tiers, policy modes, and the broker gate + audit.

The gate every actuator passes. Pure evaluate() is exhaustively checked across
the tier×mode matrix; the broker's allow/confirm/deny handshake and audit trail
are checked end-to-end.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.permissions import (
    ADMIN,
    CRITICAL,
    PermissionBroker,
    READ,
    STANDARD,
    evaluate,
    tier_of,
)

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── tier classification ──
    check(tier_of("computer.observe") == READ, "observe is READ")
    check(tier_of("computer.click") == STANDARD, "click is STANDARD")
    check(tier_of("computer.launch_app") == ADMIN, "launch is ADMIN")
    check(tier_of("computer.system_setting") == CRITICAL, "system setting is CRITICAL")
    check(tier_of("something.unknown") == ADMIN, "unknown capability defaults to ADMIN (safe)")

    # ── evaluate across the tier × mode matrix ──
    check(evaluate("computer.observe", mode="locked") == "allow", "READ auto-allowed even when locked")
    check(evaluate("computer.click", mode="locked") == "deny", "STANDARD denied when locked")
    check(evaluate("computer.click", mode="guarded") == "confirm", "STANDARD needs confirm when guarded")
    check(evaluate("computer.click", mode="trusted") == "allow", "STANDARD auto-allowed when trusted")
    check(evaluate("computer.launch_app", mode="guarded") == "confirm", "ADMIN needs confirm when guarded")
    check(evaluate("computer.launch_app", mode="trusted") == "confirm", "ADMIN NEVER auto-allowed (even trusted)")
    check(evaluate("computer.system_setting", mode="guarded") == "deny", "CRITICAL denied by default (guarded)")
    check(evaluate("computer.system_setting", mode="trusted") == "confirm", "CRITICAL at most confirm (never auto)")
    check(evaluate("weird.unknown", mode="guarded") == "confirm", "unknown capability -> confirm (never silent)")

    # ── broker: immediate decisions + audit ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        audit = Path(td) / "audit.jsonl"
        broker = PermissionBroker(mode="guarded", audit_path=audit)

        allowed = await broker.request("computer.observe")
        check(allowed["decision"] == "allowed", "READ request is allowed immediately")

        denied = await broker.request("computer.system_setting")
        check(denied["decision"] == "denied", "CRITICAL request is denied in guarded mode")

        # ── confirmation handshake ──
        pend = await broker.request("computer.click", details={"x": 10, "y": 20})
        check(pend["decision"] == "needs_confirmation" and pend.get("request_id"), "STANDARD request pends for confirmation")
        rid = pend["request_id"]

        async def _approver():
            await asyncio.sleep(0.02)
            return broker.resolve(rid, True)

        approver = asyncio.create_task(_approver())
        decision = await broker.await_decision(rid, timeout_s=2.0)
        await approver
        check(decision is True, "an approved pending request resolves True")

        # ── timeout when nobody answers ──
        pend2 = await broker.request("computer.type", details={"text": "hi"})
        timed = await broker.await_decision(pend2["request_id"], timeout_s=0.05)
        check(timed is False, "an unanswered request times out to False (deny by default)")

        # ── audit trail persisted ──
        entries = broker.audit_log()
        outcomes = [e["outcome"] for e in entries]
        check("allowed" in outcomes and "denied" in outcomes and "approved" in outcomes and "timeout" in outcomes,
              "audit log records every outcome kind")
        check(audit.exists() and audit.read_text(encoding="utf-8").strip(), "audit is persisted to disk (durable trail)")

        # ── mode change loosens/tightens live ──
        broker.set_mode("trusted")
        check((await broker.request("computer.click"))["decision"] == "allowed", "raising mode to trusted auto-allows STANDARD")
        broker.set_mode("locked")
        check((await broker.request("computer.click"))["decision"] == "denied", "locking mode denies STANDARD")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
