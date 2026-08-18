"""Permission lifecycle: every ordering permutation, with side effects COUNTED.

The permission bug that started this campaign was an ordering bug — an approval
that arrived after the tool had been cancelled still wrote "approved" to the audit.
Ordering bugs are not found by testing the happy path, so this file enumerates the
orderings and counts the side effect every time.

THE INVARIANT THAT MATTERS: the guarded side effect runs 0 or 1 times. Never twice,
never after a refusal, never after a cancellation. Everything else here supports
that claim.

Run:  venv\\Scripts\\python.exe tests\\test_permission_state_machine_p10.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.permissions import DEFAULT_MODE, PermissionBroker  # noqa: E402
from core.tool_router import (  # noqa: E402
    PERMISSION_TOOL_TIMEOUT_S, ToolCall, ToolRouter,
)

check = Checks()


class Gated:
    """A permission-gated side effect that COUNTS its own invocations."""

    def __init__(self, *, window: float = 1.0, fail: bool = False):
        self.broker = PermissionBroker(mode=DEFAULT_MODE)
        self.router = ToolRouter({})
        self.calls: list[str] = []
        self.window = window
        self.fail = fail
        self.router.register("project.delete", self._tool,
                             timeout_s=PERMISSION_TOOL_TIMEOUT_S)

    async def _tool(self, args: dict) -> dict:
        d = await self.broker.request("project.delete", details={"p": "x"})
        if d["decision"] == "denied":
            return {"ok": False, "status": "denied"}
        if d["decision"] == "needs_confirmation":
            if not await self.broker.await_decision(d["request_id"],
                                                    timeout_s=self.window):
                return {"ok": False, "status": "not_approved"}
        # THE side effect.
        self.calls.append(str(args.get("name") or "x"))
        if self.fail:
            raise RuntimeError("downstream execution failed after approval")
        return {"ok": True, "status": "deleted"}

    def run(self, name="x", timeout_s=None):
        return asyncio.create_task(self.router.execute(
            ToolCall(name="project.delete", args={"name": name}),
            timeout_s=timeout_s, retries=0))

    def outcomes(self):
        return [e.get("outcome", "") for e in reversed(self.broker.audit_log(limit=200))]

    def count(self, outcome):
        return sum(1 for o in self.outcomes() if o == outcome)

    async def pending_id(self, tries=40):
        for _ in range(tries):
            p = self.broker.pending()
            if p:
                return p[0]["request_id"]
            await asyncio.sleep(0.02)
        return ""


async def _case(label, body, *, expect_calls, expect_approved=None,
                expect_status=None):
    g = Gated()
    res = await body(g)
    check(len(g.calls) == expect_calls,
          f"{label}: side effect ran {len(g.calls)}x, expected {expect_calls}")
    check(len(g.calls) <= 1, f"{label}: NEVER more than once ({len(g.calls)})")
    if expect_approved is not None:
        check(g.count("approved") == expect_approved,
              f"{label}: {g.count('approved')} 'approved' audit entries, expected "
              f"{expect_approved} ({g.outcomes()})")
    if expect_status is not None and res is not None:
        got = (res.result or {}).get("status") if hasattr(res, "result") else None
        check(got == expect_status, f"{label}: status={got!r}, expected {expect_status!r}")
    check(g.broker.pending() == [], f"{label}: nothing left pending")
    return g


async def test_ordering_permutations():
    check.section("every approve/deny/cancel/timeout ordering, side effect counted")

    async def approve_normally(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.resolve(rid, True)
        return await t
    await _case("approve normally", approve_normally, expect_calls=1,
                expect_approved=1, expect_status="deleted")

    async def deny(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.resolve(rid, False)
        return await t
    await _case("deny", deny, expect_calls=0, expect_approved=0,
                expect_status="not_approved")

    async def timeout(g):
        g.window = 0.15
        t = g.run()
        return await t
    await _case("timeout", timeout, expect_calls=0, expect_approved=0,
                expect_status="not_approved")

    async def late_approve(g):
        g.window = 0.15
        t = g.run()
        res = await t
        rid = [e.get("request_id") for e in g.broker.audit_log(limit=20)
               if e.get("outcome") == "timeout"]
        ok = g.broker.resolve(rid[0], True) if rid else None
        check(ok is False, "late approve returns False")
        await asyncio.sleep(0.05)
        return res
    await _case("late approve after timeout", late_approve, expect_calls=0,
                expect_approved=0)

    async def late_deny(g):
        g.window = 0.15
        t = g.run()
        res = await t
        rid = [e.get("request_id") for e in g.broker.audit_log(limit=20)
               if e.get("outcome") == "timeout"]
        if rid:
            g.broker.resolve(rid[0], False)
        return res
    await _case("late deny after timeout", late_deny, expect_calls=0,
                expect_approved=0)

    async def duplicate_approve(g):
        t = g.run()
        rid = await g.pending_id()
        first = g.broker.resolve(rid, True)
        second = g.broker.resolve(rid, True)
        check(first is True and second is False,
              "duplicate approve: first True, second False")
        return await t
    await _case("duplicate approve", duplicate_approve, expect_calls=1,
                expect_approved=1)

    async def duplicate_deny(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.resolve(rid, False)
        g.broker.resolve(rid, False)
        return await t
    await _case("duplicate deny", duplicate_deny, expect_calls=0,
                expect_approved=0)

    async def approve_then_deny(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.resolve(rid, True)
        g.broker.resolve(rid, False)   # too late to matter
        return await t
    await _case("approve then deny", approve_then_deny, expect_calls=1,
                expect_approved=1)

    async def deny_then_approve(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.resolve(rid, False)
        g.broker.resolve(rid, True)    # must NOT execute
        await asyncio.sleep(0.05)
        return await t
    await _case("deny then approve", deny_then_approve, expect_calls=0,
                expect_approved=0)

    async def cancel_then_approve(g):
        t = g.run()
        rid = await g.pending_id()
        g.broker.cancel(rid)
        # The tool must return a clean result, NOT raise. Withdrawing a request
        # used to cancel the pending future, which raised CancelledError into a
        # normally-waiting tool; CancelledError is a BaseException, so
        # ToolRouter.execute never caught it and the whole turn died instead of
        # reporting "not approved".
        raised = ""
        try:
            res = await t
        except BaseException as e:  # noqa: BLE001
            raised, res = type(e).__name__, None
        check(not raised, f"cancel does not blow up the waiting tool ({raised})")
        check(res is not None and (res.result or {}).get("status") == "not_approved",
              f"it returns a clean not_approved ({res.result if res else None})")
        check(g.broker.resolve(rid, True) is False,
              "approve after cancel returns False")
        await asyncio.sleep(0.05)
        return None
    await _case("cancel then approve", cancel_then_approve, expect_calls=0,
                expect_approved=0)

    async def outer_cancellation(g):
        t = g.run()
        await g.pending_id()
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        return None
    await _case("outer cancellation", outer_cancellation, expect_calls=0,
                expect_approved=0)

    async def wrong_request_id(g):
        t = g.run()
        rid = await g.pending_id()
        check(g.broker.resolve("not-a-real-id", True) is False,
              "an unknown request id resolves False")
        g.broker.resolve(rid, True)
        return await t
    await _case("wrong request id then correct", wrong_request_id, expect_calls=1,
                expect_approved=1)


async def test_execution_failure_is_not_a_permission_failure():
    check.section("approval and execution outcome stay distinct")

    g = Gated(fail=True)
    t = g.run()
    rid = await g.pending_id()
    g.broker.resolve(rid, True)
    res = await t

    check(g.count("approved") == 1,
          f"permission WAS approved ({g.outcomes()})")
    check(len(g.calls) == 1, "the side effect was attempted exactly once")
    check(res.ok is False and "execution failed" in (res.error or ""),
          f"but the tool result reports execution failure ({res.error!r})")
    check(not any(o in ("deleted", "executed") for o in g.outcomes()),
          "and the audit never claims the action succeeded")
    check(g.broker.pending() == [], "nothing left pending")


async def test_concurrent_requests_are_independent():
    check.section("simultaneous permission requests do not cross wires")

    g = Gated()
    ran: list[str] = []

    async def tool(args):
        d = await g.broker.request("project.delete", details={"p": args["name"]})
        if not await g.broker.await_decision(d["request_id"], timeout_s=2.0):
            return {"ok": False, "status": "not_approved"}
        ran.append(args["name"])
        return {"ok": True}

    g.router.register("project.delete", tool, timeout_s=PERMISSION_TOOL_TIMEOUT_S)

    t1 = g.run("alpha")
    t2 = g.run("beta")
    t3 = g.run("gamma")
    for _ in range(60):
        if len(g.broker.pending()) == 3:
            break
        await asyncio.sleep(0.02)
    pend = [p["request_id"] for p in g.broker.pending()]
    check(len(pend) == 3, f"three independent requests are pending ({len(pend)})")

    # Approve one, deny one, leave one to time out.
    g.broker.resolve(pend[0], True)
    g.broker.resolve(pend[1], False)
    r1, r2, r3 = await asyncio.gather(t1, t2, t3)

    check(len(ran) == 1,
          f"exactly ONE side effect ran across three requests ({ran})")
    check(g.count("approved") == 1 and g.count("rejected") == 1
          and g.count("timeout") == 1,
          f"one approved, one rejected, one timed out ({g.outcomes()})")
    check(g.broker.pending() == [], "and nothing is left pending")


async def test_budget_contract_holds():
    check.section("the advertised human window fits inside the tool budget")
    from core.permissions import HUMAN_DECISION_TIMEOUT_S

    check(PERMISSION_TOOL_TIMEOUT_S > HUMAN_DECISION_TIMEOUT_S,
          f"{PERMISSION_TOOL_TIMEOUT_S:g}s budget > {HUMAN_DECISION_TIMEOUT_S:g}s window")

    # An approval arriving after the OLD 25s-equivalent boundary still lands.
    g = Gated(window=1.2)
    t = g.run(timeout_s=0.25)          # the old, too-short call-site value
    rid = await g.pending_id()
    await asyncio.sleep(0.5)           # past the old boundary
    check(g.broker.pending() != [], "the request is still live past the old boundary")
    g.broker.resolve(rid, True)
    res = await t
    check(len(g.calls) == 1 and res.ok,
          f"and the approval executes ({len(g.calls)} call(s))")


async def main():
    await test_ordering_permutations()
    await test_execution_failure_is_not_a_permission_failure()
    await test_concurrent_requests_are_independent()
    await test_budget_contract_holds()
    check.finish()


if __name__ == "__main__":
    run(main)
