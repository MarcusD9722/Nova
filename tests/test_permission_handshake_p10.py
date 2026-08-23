"""P10 pre-flight: the permission/tool timeout handshake (brief 2E cases A-H).

The bug this file exists for was CONFIRMED against live code, not theorised:

  `project.delete` asks `PermissionBroker` for approval and waits up to 120s for
  Marcus to click. The agent loop called it with `timeout_s=25.0`. At 25s the
  router cancelled the tool and returned `ok=False, error=""` — an empty string,
  because `asyncio.TimeoutError` carries no message. The approval request stayed
  in `_pending`, so the UI kept showing a live Approve button for another 95s.
  Clicking it audited `approved` and returned False. Nothing was deleted, and the
  audit log recorded an approval for an action that never happened.

TIMING IS SCALED, not real. Waiting 120s in a test would be absurd, so the three
numbers that matter keep their RATIO and shrink by 100x:

  OLD_BOUNDARY  0.25s   stands in for the 25s ToolRouter timeout that used to cut
                        the handshake — the boundary approval must now survive
  WINDOW        1.20s   stands in for the 120s broker approval window
  BUDGET        1.40s   stands in for the tool's real 140s budget

Case B then approves at 0.5s: AFTER the old boundary, BEFORE the window closes.
Under the old contract that click was impossible to honour. The unscaled
production constants are asserted separately in CASE A.

Nothing here touches Marcus's real projects/ directory — cases B/C/D/H build a
throwaway project tree in a temp dir.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.permissions import (  # noqa: E402
    ADMIN,
    DEFAULT_MODE,
    HUMAN_DECISION_TIMEOUT_S,
    PermissionBroker,
    evaluate,
    tier_of,
)
from core.tool_router import (  # noqa: E402
    DEFAULT_TOOL_TIMEOUT_S,
    PERMISSION_EXECUTION_ALLOWANCE_S,
    PERMISSION_TOOL_TIMEOUT_S,
    ToolCall,
    ToolRouter,
)

#: Scaled stand-ins (see the module docstring). 100x smaller, same ratio.
OLD_BOUNDARY = 0.25
WINDOW = 1.20
BUDGET = 1.40
APPROVE_AT = 0.50   # after OLD_BOUNDARY, before WINDOW

REPO = Path(__file__).resolve().parents[1]
PASS, FAIL = [], []


def _declared_timeouts(path: Path) -> dict[str, str | None]:
    """tool name -> the source text of its `timeout_s=` argument, or None.

    Every `*.register("name", ...)` call in the file, read from the AST so the
    answer does not depend on formatting, argument order, or comments.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register" and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        kw = next((k.value for k in node.keywords if k.arg == "timeout_s"), None)
        out[first.value] = None if kw is None else ast.unparse(kw)
    return out


def code_only(path: Path) -> str:
    """Source with comments and docstrings removed.

    Static assertions that grep raw source keep matching the PROSE that explains
    the fix rather than the code that implements it — this file's own first run
    failed because a comment in agent.py mentions `project.delete` while
    explaining why the loop must not special-case it. Tokenizing is the fix.
    """
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    prev_type = tokenize.INDENT
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            # A string that is the whole statement is a docstring, not code.
            if tok.type == tokenize.STRING and prev_type in (
                    tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
                continue
            out.append(tok.string)
            if tok.type not in (tokenize.NL, tokenize.NEWLINE):
                prev_type = tok.type
            else:
                prev_type = tok.type
    except tokenize.TokenError:
        return src   # never let a tokenizer hiccup silently weaken the check
    return "\n".join(out)


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def outcomes(broker: PermissionBroker) -> list[str]:
    return [e.get("outcome", "") for e in reversed(broker.audit_log(limit=200))]


def count_outcome(broker: PermissionBroker, outcome: str) -> int:
    return sum(1 for o in outcomes(broker) if o == outcome)


async def _never(_args: dict) -> dict:
    await asyncio.sleep(3600)
    return {"ok": True}


async def _quick(_args: dict) -> dict:
    await asyncio.sleep(0.2)
    return {"ok": True, "slept": 0.2}


class Fixture:
    """A real ProjectManager + real PermissionBroker + real ToolRouter on temp dirs.

    The gate below is the same shape as `core/runtime.py::_gate` — request, honour
    an outright allow/deny, otherwise wait on the broker — so these cases exercise
    the production handshake rather than a mock of it.
    """

    def __init__(self, td: str, *, window: float = WINDOW) -> None:
        from core.project_manager import ProjectManager

        self.root = Path(td)
        self.projects = self.root / "projects"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.pm = ProjectManager(repo_root=self.root, projects_dir=self.projects)
        self.broker = PermissionBroker(mode=DEFAULT_MODE, audit_path=self.root / "audit.jsonl")
        self.router = ToolRouter({})
        self.window = window
        self.deletes: list[str] = []
        self.restores: list[str] = []
        self.router.register("project.delete", self._delete, timeout_s=BUDGET)
        self.router.register("project.restore", self._restore, timeout_s=BUDGET)

    def make_project(self, name: str) -> Path:
        """A REAL project. This fixture drives the production
        ProjectManager.delete_project, which takes the same view of a
        project as every read surface: a bare mkdir is a directory, not a
        project. ensure_workspace is the production creation path.
        """
        d = self.pm.ensure_workspace(name)
        (d / "main.py").write_text("print('hi')\n", encoding="utf-8")
        return d

    async def _gate(self, capability: str, details: dict) -> dict | None:
        d = await self.broker.request(capability, details=details)
        if d["decision"] == "allowed":
            return None
        if d["decision"] == "denied":
            return {"ok": False, "status": "denied", "reason": d.get("reason")}
        if not await self.broker.await_decision(d["request_id"], timeout_s=self.window):
            return {"ok": False, "status": "not_approved",
                    "note": "You didn't approve it (declined or timed out) — nothing was touched."}
        return None

    async def _delete(self, args: dict) -> dict:
        name = str(args.get("name") or "")
        blocked = await self._gate("project.delete", {"project": name, "recoverable": True})
        if blocked:
            return blocked
        try:
            res = await asyncio.to_thread(self.pm.delete_project, name)
        except Exception as e:  # noqa: BLE001
            # A failure HERE is an execution failure, not a permission failure.
            return {"ok": False, "status": "execution_error", "error": str(e)[:200]}
        self.deletes.append(name)
        return {"ok": True, "status": "deleted", **res}

    async def _restore(self, args: dict) -> dict:
        entry = str(args.get("entry") or "")
        blocked = await self._gate("project.restore", {"entry": entry})
        if blocked:
            return blocked
        try:
            res = await asyncio.to_thread(self.pm.restore_project, entry)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "execution_error", "error": str(e)[:200]}
        self.restores.append(entry)
        return {"ok": True, "status": "restored", **res}

    async def click_after(self, delay: float, approved: bool) -> str:
        """Stand in for Marcus clicking Approve/Deny `delay` seconds from now."""
        await asyncio.sleep(delay)
        pend = self.broker.pending()
        if not pend:
            return ""
        rid = pend[0]["request_id"]
        self.broker.resolve(rid, approved, by="marcus")
        return rid


# ── CASE A — ordinary tool timeout, and the one authoritative budget ──────────
def case_a() -> None:
    print("\nCASE A  ordinary tool timeout + a single authoritative budget")

    check("A1 the permission budget exceeds the approval window",
          PERMISSION_TOOL_TIMEOUT_S > HUMAN_DECISION_TIMEOUT_S,
          f"tool budget {PERMISSION_TOOL_TIMEOUT_S:g}s > approval window "
          f"{HUMAN_DECISION_TIMEOUT_S:g}s — the handshake can complete")
    check("A2 it is DERIVED from the broker's window, so the two cannot drift",
          PERMISSION_TOOL_TIMEOUT_S == HUMAN_DECISION_TIMEOUT_S + PERMISSION_EXECUTION_ALLOWANCE_S,
          f"{HUMAN_DECISION_TIMEOUT_S:g} + {PERMISSION_EXECUTION_ALLOWANCE_S:g} "
          f"= {PERMISSION_TOOL_TIMEOUT_S:g} (brief asked for ~130-150s)")
    check("A3 ordinary tools keep an ordinary default",
          DEFAULT_TOOL_TIMEOUT_S < HUMAN_DECISION_TIMEOUT_S,
          f"the generic default is still {DEFAULT_TOOL_TIMEOUT_S:g}s — no tool was "
          f"blanket-raised to 120s+")

    router = ToolRouter({})
    router.register("slow.human", _never, timeout_s=PERMISSION_TOOL_TIMEOUT_S)
    router.register("fast.thing", _never)
    check("A4 a declared timeout wins over a call site's value",
          router.timeout_for("slow.human", 25.0) == PERMISSION_TOOL_TIMEOUT_S,
          f"call site said 25.0, router says {router.timeout_for('slow.human', 25.0):g}")
    check("A5 an undeclared tool still honours the call site",
          router.timeout_for("fast.thing", 15.0) == 15.0)
    check("A6 and falls back to the default when nobody says",
          router.timeout_for("fast.thing") == DEFAULT_TOOL_TIMEOUT_S)

    async def run() -> None:
        r = ToolRouter({})

        # An ordinary tool exceeding its ordinary timeout: fails AT that timeout,
        # with a non-empty error. This is the brief's CASE A proper.
        r.register("ordinary.hangs", _never)
        t0 = time.perf_counter()
        res = await r.execute(ToolCall(name="ordinary.hangs", args={}),
                              timeout_s=0.3, retries=0)
        dt = time.perf_counter() - t0
        check("A7 an ordinary tool fails AT its own timeout, with no dead wait after",
              res.ok is False and 0.28 <= dt < 0.45,
              f"failed after {dt:.3f}s (budget 0.3s, tolerance 0.28-0.45; the "
              f"trailing back-off sleep used to add 0.2s to every single failure, "
              f"which would land at ~0.5s)")
        check("A8 the error is NOT empty and names the budget",
              bool((res.error or "").strip()) and "timed out" in (res.error or "").lower()
              and "0.3" in (res.error or ""), f"error={res.error!r}")

        # execute() must OBEY the declared budget in both directions.
        r.register("declared.short", _never, timeout_s=0.05)
        res2 = await r.execute(ToolCall(name="declared.short", args={}),
                               timeout_s=300.0, retries=0)
        check("A9 execute() times out at the DECLARED 0.05s, not the passed 300s",
              res2.ok is False and "0.05" in (res2.error or ""), f"error={res2.error!r}")

        r.register("declared.long", _quick, timeout_s=30.0)
        res3 = await r.execute(ToolCall(name="declared.long", args={}),
                               timeout_s=0.05, retries=0)
        check("A10 a declared-long tool is NOT cut short by a 0.05s call site",
              res3.ok is True and res3.result == {"ok": True, "slept": 0.2},
              f"ok={res3.ok} error={res3.error!r} — the project.delete bug in miniature")

        async def _blank(_a: dict) -> dict:
            raise ValueError("")   # an exception carrying no message at all

        r.register("blank", _blank)
        res4 = await r.execute(ToolCall(name="blank", args={}), retries=0)
        check("A11 even a message-less exception produces text",
              bool((res4.error or "").strip()) and "ValueError" in (res4.error or ""),
              f"error={res4.error!r}")

        # PluginConfigError semantics are preserved (brief 2D).
        class PluginConfigError(RuntimeError):
            pass

        async def _unconfigured(_a: dict) -> dict:
            raise PluginConfigError("gmail account not connected")

        from core.event_bus import BUS
        q = BUS.subscribe()
        r.register("plugin.unconfigured", _unconfigured)
        res5 = await r.execute(ToolCall(name="plugin.unconfigured", args={}), retries=0)
        await asyncio.sleep(0.05)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait().type)
        BUS.unsubscribe(q)
        check("A12 a not-configured plugin still publishes tool.not_configured",
              "tool.not_configured" in seen and "tool.error" not in seen
              and res5.ok is False and "not connected" in (res5.error or ""),
              f"events={seen} error={res5.error!r}")

    asyncio.run(run())


# ── CASE A' — the live registrations really declare it ────────────────────────
def case_a_wiring() -> None:
    print("\nCASE A'  the live registrations, read from runtime.py source")
    # Comments are stripped: prose ABOUT the fix must never satisfy a check FOR it.
    src = code_only(REPO / "core" / "runtime.py")

    # Read the registrations from the AST, not by slicing text. A brace-counting
    # substring search over this file was already wrong once (tokenized source has
    # no `register("x"` spelling left in it), and it would silently start matching
    # a NEIGHBOURING register() call the moment the argument order changed.
    declared = _declared_timeouts(REPO / "core" / "runtime.py")
    for tool in ("project.delete", "project.restore", "project.purge"):
        check(f"A' {tool} declares the permission budget",
              declared.get(tool) == "PERMISSION_TOOL_TIMEOUT_S",
              f"timeout_s={declared.get(tool)!r} — a permission-blocking tool must "
              f"declare it, or the generic call-site value cancels the handshake")
    check("A' an ordinary tool is left undeclared",
          "project.trash" in declared and declared["project.trash"] is None,
          f"project.trash={declared.get('project.trash')!r} — non-blocking tools "
          f"must NOT be raised to 140s")

    agent = code_only(REPO / "core" / "orchestrator" / "agent.py")
    check("A' the agent loop hard-codes no tool timeout at all",
          "25.0" not in agent and "timeout_s" not in agent,
          "agent.py must let the router decide, or project.delete breaks again")
    check("A' the gate no longer restates the 120s window",
          "120.0" not in src,
          "one number, one owner — the only 120.0 left is HUMAN_DECISION_TIMEOUT_S "
          "in core/permissions.py")
    check("A' no special-cased tool names in the agent loop",
          "project.delete" not in agent,
          "2A forbids hardcoding tool names in Agent.run — only comments may name it")

    # And the tokenizer really is doing the work: the raw text DOES contain the
    # name, inside the comment that explains the rule.
    raw_agent = (REPO / "core" / "orchestrator" / "agent.py").read_text(encoding="utf-8")
    check("A' (the comment-stripping is load-bearing, not decorative)",
          "project.delete" in raw_agent and "project.delete" not in agent,
          "raw source mentions it in prose; code_only() correctly does not")


# ── CASE B — approval AFTER the old boundary, BEFORE the window closes ────────
def case_b() -> None:
    print("\nCASE B  approval inside the advertised window (the original defect)")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            fx.make_project("toy")

            t0 = time.perf_counter()
            # The call site passes the OLD boundary. Under the old contract this
            # cancelled the tool at 0.25s and the click below could never land.
            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            clicker = asyncio.create_task(fx.click_after(APPROVE_AT, True))
            res = await task
            rid = await clicker
            dt = time.perf_counter() - t0

            check("B1 the router was STILL WAITING past the old boundary",
                  dt > OLD_BOUNDARY and bool(rid),
                  f"resolved at {dt:.2f}s, old boundary was {OLD_BOUNDARY:g}s")
            check("B2 the approval resolved a live request", bool(rid))
            check("B3 the tool result is a success",
                  res.ok is True and (res.result or {}).get("status") == "deleted",
                  f"ok={res.ok} error={res.error!r} result={res.result}")
            check("B4 delete executed EXACTLY once", fx.deletes == ["toy"], f"{fx.deletes}")
            check("B5 the source folder is gone", not (fx.projects / "toy").exists())
            check("B6 a trash entry exists",
                  bool((res.result or {}).get("moved_to_trash"))
                  and any(p.name == "main.py" for p in (fx.projects / ".trash").rglob("*")),
                  f"moved_to_trash={(res.result or {}).get('moved_to_trash')!r}")
            check("B7 the audit contains exactly one legitimate 'approved'",
                  count_outcome(fx.broker, "approved") == 1, str(outcomes(fx.broker)))
            check("B8 no pending request remains", fx.broker.pending() == [])

            lines = [json.loads(x) for x in
                     (fx.root / "audit.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
            check("B9 the audit is durable on disk",
                  any(e.get("outcome") == "approved" for e in lines), f"{len(lines)} entries")

    asyncio.run(run())


# ── CASE C — no approval: the broker's own timeout, not the tool's ───────────
def case_c() -> None:
    print("\nCASE C  no approval at all")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            # A 0.6s window, twice the 0.25s call-site boundary, so "the broker
            # closed it, not the router" has real margin rather than resting on a
            # float comparison at the boundary (an equality-tight version of this
            # assertion flaked once at 0.2999s).
            window = 0.6
            fx = Fixture(td, window=window)
            fx.make_project("toy")

            t0 = time.perf_counter()
            res = await fx.router.execute(ToolCall(name="project.delete", args={"name": "toy"}),
                                          timeout_s=OLD_BOUNDARY, retries=0)
            dt = time.perf_counter() - t0

            # A refusal is a FAILURE now, not a success carrying a sad
            # payload: the router reports `ok=False` for any tool that returns a
            # top-level `ok: False`. Both halves matter — the caller must be able
            # to see that it did not happen AND why, so the structured payload is
            # still asserted alongside.
            check("C1 the tool returns a clean not_approved",
                  res.ok is False and (res.result or {}).get("status") == "not_approved",
                  f"ok={res.ok} result={res.result}")
            check("C2 the OUTER tool did not preempt the broker",
                  dt > OLD_BOUNDARY * 1.5 and dt < BUDGET
                  and res.error == (res.result or {}).get("note"),
                  f"returned after {dt:.3f}s with error={res.error!r}; the broker's "
                  f"{window:g}s window closed first, well past the {OLD_BOUNDARY:g}s "
                  f"the call site asked for, and the message is the TOOL's own "
                  f"refusal rather than the router's timeout")
            check("C3 nothing was deleted", fx.deletes == [] and (fx.projects / "toy").exists())
            check("C4 audited as a timeout", count_outcome(fx.broker, "timeout") == 1,
                  str(outcomes(fx.broker)))
            check("C5 no 'approved' anywhere in the audit",
                  count_outcome(fx.broker, "approved") == 0, str(outcomes(fx.broker)))
            check("C6 no pending request remains", fx.broker.pending() == [])

    asyncio.run(run())


# ── CASE D — explicit denial ──────────────────────────────────────────────────
def case_d() -> None:
    print("\nCASE D  explicit denial")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            fx.make_project("toy")

            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            rid = await fx.click_after(APPROVE_AT, False)
            res = await task

            check("D1 the tool reports not_approved",
                  res.ok is False and (res.result or {}).get("status") == "not_approved",
                  f"ok={res.ok} result={res.result}")
            check("D2 nothing executed", fx.deletes == [])
            check("D3 the project is untouched", (fx.projects / "toy" / "main.py").exists())
            check("D4 audited as rejected, exactly once",
                  count_outcome(fx.broker, "rejected") == 1 and bool(rid),
                  str(outcomes(fx.broker)))
            check("D5 never audited as approved",
                  count_outcome(fx.broker, "approved") == 0, str(outcomes(fx.broker)))
            check("D6 no pending request remains", fx.broker.pending() == [])

            # CRITICAL is denied outright: it never even becomes pending, so no
            # timeout policy could ever rescue it.
            purge = await fx.broker.request("project.purge", details={"entry": "ALL"})
            check("D7 purge is denied without becoming pending",
                  purge["decision"] == "denied" and fx.broker.pending() == [],
                  f"decision={purge['decision']!r}")
            check("D8 delete is ADMIN and needs confirmation in the default mode",
                  tier_of("project.delete") == ADMIN
                  and evaluate("project.delete", mode=DEFAULT_MODE) == "confirm")

    asyncio.run(run())


# ── CASE E — caller cancellation while permission is pending ─────────────────
def case_e() -> None:
    print("\nCASE E  the caller/turn is cancelled while waiting")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            fx.make_project("toy")

            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=BUDGET, retries=0))
            await asyncio.sleep(0.15)
            pend = fx.broker.pending()
            check("E1 a request is pending before cancellation", len(pend) == 1)
            rid = pend[0]["request_id"]

            task.cancel()                       # the turn is abandoned
            try:
                await task
            except asyncio.CancelledError:
                propagated = True
            else:
                propagated = False
            await asyncio.sleep(0.05)

            check("E2 cancellation propagates instead of a fake 'denied'", propagated)
            check("E3 nothing executed", fx.deletes == [] and (fx.projects / "toy").exists())
            check("E4 the pending request was REMOVED, not orphaned",
                  fx.broker.pending() == [], f"pending={fx.broker.pending()}")
            check("E5 audited as abandoned", count_outcome(fx.broker, "abandoned") == 1,
                  str(outcomes(fx.broker)))

            late = fx.broker.resolve(rid, True, by="marcus")
            check("E6 a later Approve returns False", late is False)
            entry = fx.broker.audit_log(limit=1)[0]
            check("E7 the late approval is NOT audited as a successful approval",
                  entry["outcome"] != "approved"
                  and count_outcome(fx.broker, "approved") == 0,
                  f"outcome={entry['outcome']!r}")
            check("E8 it is named a late approval that was ignored",
                  entry["outcome"] == "late_approval_ignored"
                  and entry.get("settled_as") == "abandoned",
                  f"outcome={entry['outcome']!r} settled_as={entry.get('settled_as')!r}")
            check("E9 still nothing executed after the late click", fx.deletes == [])

            # An explicit withdrawal by the caller is audited distinctly.
            d = await fx.broker.request("project.delete", details={"project": "x"})
            fx.broker.cancel(d["request_id"], reason="the caller gave up")
            check("E10 an explicit cancel is audited as cancelled",
                  fx.broker.audit_log(limit=1)[0]["outcome"] == "cancelled"
                  and fx.broker.pending() == [])

            # E11 tests `await_decision` DIRECTLY, with no asyncio.wait_for in
            # between. Through the router, `wait_for` re-raises CancelledError on
            # its own, which masks whether the broker swallowed it — a mutation
            # that replaced the re-raise with `return False` passed every other
            # check here. Swallowing it would report "denied" to a caller when
            # nobody decided anything, so the distinction needs its own test.
            d2 = await fx.broker.request("project.delete", details={"project": "z"})
            bare = asyncio.create_task(fx.broker.await_decision(d2["request_id"], timeout_s=30.0))
            await asyncio.sleep(0.05)
            bare.cancel()
            try:
                returned = await bare
            except asyncio.CancelledError:
                raised, returned = True, None
            else:
                raised = False
            check("E11 await_decision re-raises cancellation, never returns a verdict",
                  raised is True,
                  "" if raised else f"returned {returned!r} instead of raising — a "
                  f"swallowed cancellation reads to the caller as a human denial")

    asyncio.run(run())


# ── CASE F — a late approval after the broker's own timeout ──────────────────
def case_f() -> None:
    print("\nCASE F  approval arriving after the broker window closed")

    async def run() -> None:
        broker = PermissionBroker(mode=DEFAULT_MODE)
        d = await broker.request("project.delete", details={"project": "toy"})
        rid = d["request_id"]

        approved = await broker.await_decision(rid, timeout_s=0.1)
        check("F1 the window closes as denied", approved is False)
        check("F2 audited as a timeout", count_outcome(broker, "timeout") == 1)
        check("F3 nothing is left pending", broker.pending() == [])

        ok = broker.resolve(rid, True, by="marcus")
        entry = broker.audit_log(limit=1)[0]
        check("F4 the late approval returns False", ok is False)
        check("F5 it is NOT audited as 'approved'",
              entry["outcome"] != "approved" and count_outcome(broker, "approved") == 0,
              f"outcome={entry['outcome']!r} — the old code wrote 'approved' here for "
              f"an action that never ran")
        check("F6 it is named late_approval_ignored",
              entry["outcome"] == "late_approval_ignored", f"outcome={entry['outcome']!r}")
        check("F7 and records how the request had really ended",
              entry.get("settled_as") == "timeout", f"settled_as={entry.get('settled_as')!r}")

        d2 = await broker.request("project.delete", details={"project": "y"})
        await broker.await_decision(d2["request_id"], timeout_s=0.1)
        broker.resolve(d2["request_id"], False)
        check("F8 a late REJECTION is named too, not silently accepted",
              broker.audit_log(limit=1)[0]["outcome"] == "late_rejection_ignored",
              str(broker.audit_log(limit=1)[0]["outcome"]))

        check("F9 an id that never existed is audited as unknown_request",
              broker.resolve("never-existed", True) is False
              and broker.audit_log(limit=1)[0]["outcome"] == "unknown_request",
              str(broker.audit_log(limit=1)[0]["outcome"]))

    asyncio.run(run())


# ── CASE G — duplicate approval ──────────────────────────────────────────────
def case_g() -> None:
    print("\nCASE G  duplicate approval")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            fx.make_project("toy")

            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            await asyncio.sleep(0.15)
            rid = fx.broker.pending()[0]["request_id"]

            first = fx.broker.resolve(rid, True, by="marcus")
            second = fx.broker.resolve(rid, True, by="marcus")
            res = await task

            check("G1 the first approval applies", first is True)
            check("G2 the second returns False", second is False)
            check("G3 exactly ONE successful 'approved' audit event",
                  count_outcome(fx.broker, "approved") == 1, str(outcomes(fx.broker)))
            check("G4 the duplicate is audited as late, not as a second approval",
                  fx.broker.audit_log(limit=1)[0]["outcome"] == "late_approval_ignored")
            check("G5 the delete ran exactly once",
                  fx.deletes == ["toy"] and res.ok, f"{fx.deletes}")

            # G6 reaches resolve()'s DEFENSIVE branch: a future that is already
            # settled while still listed in _pending. Nothing in the current code
            # produces that state — every exit path pops the entry — so it is
            # constructed here deliberately. A mutation making this branch audit a
            # plain "approved" survived every other check, which is exactly what
            # untested defensive code does.
            d = await fx.broker.request("project.delete", details={"project": "w"})
            rid2 = d["request_id"]
            fx.broker._pending[rid2].set_result(True)          # noqa: SLF001
            before = count_outcome(fx.broker, "approved")
            ok = fx.broker.resolve(rid2, True, by="marcus")
            entry = fx.broker.audit_log(limit=1)[0]
            check("G6 an already-settled request is not re-approved",
                  ok is False and entry["outcome"] == "late_approval_ignored"
                  and count_outcome(fx.broker, "approved") == before,
                  f"ok={ok} outcome={entry['outcome']!r}")
            check("G7 and it is dropped from pending", fx.broker.pending() == [])

    asyncio.run(run())


# ── CASE H — execution fails AFTER a legitimate approval ─────────────────────
def case_h() -> None:
    print("\nCASE H  approval succeeds, execution fails — do not conflate them")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            # Deliberately do NOT create the project: the delete will raise
            # FileNotFoundError after permission has legitimately been granted.
            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "ghost"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            rid = await fx.click_after(APPROVE_AT, True)
            res = await task

            check("H1 permission was legitimately approved",
                  bool(rid) and count_outcome(fx.broker, "approved") == 1,
                  str(outcomes(fx.broker)))
            check("H2 the TOOL RESULT reports execution failure",
                  (res.result or {}).get("status") == "execution_error",
                  f"result={res.result}")
            check("H3 the failure is described, not empty",
                  bool(str((res.result or {}).get("error", "")).strip()),
                  f"error={(res.result or {}).get('error')!r}")
            check("H4 nothing was recorded as deleted", fx.deletes == [])
            check("H5 the audit does NOT claim the action succeeded",
                  not any(o in ("deleted", "executed") for o in outcomes(fx.broker)),
                  str(outcomes(fx.broker)))
            check("H6 no pending request remains", fx.broker.pending() == [])

    asyncio.run(run())


# ── 2F — the real delete/restore/deny contract on a temp tree ────────────────
def case_2f() -> None:
    print("\n2F  real project.delete / restore / deny against a TEMP projects dir")

    async def run() -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fx = Fixture(td)
            fx.make_project("toy")

            # delete, approved after the old boundary
            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            await fx.click_after(APPROVE_AT, True)
            res = await task
            entry = (res.result or {}).get("moved_to_trash", "")
            check("2F1 moved to .trash exactly once, source gone",
                  res.ok and fx.deletes == ["toy"] and bool(entry)
                  and not (fx.projects / "toy").exists(), f"entry={entry!r}")
            check("2F2 the trash entry really holds the bytes",
                  (fx.projects / ".trash" / entry / "main.py").exists())
            check("2F3 the response is truthful about recoverability",
                  "recover" in json.dumps(res.result).lower() or bool(entry),
                  f"result={res.result}")

            # restore, approved
            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.restore", args={"entry": entry}),
                timeout_s=OLD_BOUNDARY, retries=0))
            await fx.click_after(APPROVE_AT, True)
            res = await task
            check("2F4 restored exactly once, with contents",
                  res.ok and fx.restores == [entry]
                  and (fx.projects / "toy" / "main.py").read_text(encoding="utf-8") == "print('hi')\n",
                  f"restores={fx.restores} result={res.result}")

            # delete, denied
            task = asyncio.create_task(fx.router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}),
                timeout_s=OLD_BOUNDARY, retries=0))
            await fx.click_after(APPROVE_AT, False)
            res = await task
            check("2F5 a denied delete leaves the project in place",
                  (res.result or {}).get("status") == "not_approved"
                  and fx.deletes == ["toy"]          # still only the first, approved one
                  and (fx.projects / "toy" / "main.py").exists(),
                  f"deletes={fx.deletes}")
            check("2F6 the audit tells the whole story in order",
                  [o for o in outcomes(fx.broker) if o != "pending"]
                  == ["approved", "approved", "rejected"],
                  str(outcomes(fx.broker)))
            check("2F7 Marcus's real projects dir was never used",
                  str(fx.projects).startswith(str(fx.root))
                  and "Desktop" not in str(fx.projects), str(fx.projects))

    asyncio.run(run())


class _StubLLM:
    """Stands in for LLMRuntime.

    Not a convenience: the delete path must never reach the model, and a stub that
    RAISES proves it. Loading a real 9B model to test a folder move would also put
    this suite behind a GPU.
    """

    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def generate(self, *a, **k):
        raise AssertionError("the project.delete path must not call the model")

    async def chat(self, *a, **k):
        raise AssertionError("the project.delete path must not call the model")


def case_runtime() -> None:
    """The ACTUAL production RuntimeManager, its registered tools, its own broker.

    Everything above builds a gate with the same SHAPE as the production closure.
    That is not proof of the integration defect we fixed: the bug lived in the
    wiring between `Agent.run`'s timeout, the registration, and `_gate`. So this
    case constructs the real `RuntimeManager` on temp directories and drives the
    tools it registered itself.
    """
    print("\nRUNTIME  the real RuntimeManager, real registered project.delete")

    async def run() -> None:
        from core.event_bus import BUS
        from core.runtime import RuntimeManager
        from core.tooling import build_tool_router
        from memory.unifier import MemoryUnifier

        with TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            projects = root / "projects"
            (projects / "toy").mkdir(parents=True)
            # A REAL project: this exercises the production project.delete,
            # which takes the same view of a project as every read surface.
            (projects / "toy" / "PROJECT.md").write_text(
                "# toy\n", encoding="utf-8")
            (projects / "toy" / "main.py").write_text("print('hi')\n", encoding="utf-8")
            (projects / "toy" / "README.md").write_text("# toy\n", encoding="utf-8")
            # Counted, not assumed: a real project carries its identity
            # document too, and the claim R14 makes is that the tool REPORTS
            # what it moved.
            toy_files = sum(1 for f in (projects / "toy").rglob("*")
                            if f.is_file())
            mem_dir = root / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)

            mem = MemoryUnifier(mem_dir)
            await mem.initialize()
            router = build_tool_router(repo_root=root, projects_dir=projects, memory=mem)
            rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=mem,
                                llm=_StubLLM(), router=router, memory_dir=mem_dir)
            broker = rt.permission_broker

            check("R1 the real runtime registered project.delete and project.restore",
                  "project.delete" in rt._router.list_tools()
                  and "project.restore" in rt._router.list_tools())
            # Asserted on the LIVE router object, not on source text.
            check("R2 the LIVE router gives project.delete the permission budget",
                  rt._router.timeout_for("project.delete") == PERMISSION_TOOL_TIMEOUT_S,
                  f"{rt._router.timeout_for('project.delete'):g}s")
            check("R3 and it beats the agent loop's generic value",
                  rt._router.timeout_for("project.delete", DEFAULT_TOOL_TIMEOUT_S)
                  == PERMISSION_TOOL_TIMEOUT_S,
                  f"generic {DEFAULT_TOOL_TIMEOUT_S:g}s -> "
                  f"{rt._router.timeout_for('project.delete', DEFAULT_TOOL_TIMEOUT_S):g}s")
            check("R4 an ordinary registered tool is NOT raised to it",
                  rt._router.timeout_for("project.trash") == DEFAULT_TOOL_TIMEOUT_S,
                  f"project.trash = {rt._router.timeout_for('project.trash'):g}s")
            check("R5 the audit log lands under the temp memory dir",
                  str(broker._audit_path).startswith(str(root)),  # noqa: SLF001
                  str(broker._audit_path))                        # noqa: SLF001

            # ── DELETE, approved ────────────────────────────────────────────
            q = BUS.subscribe()
            task = asyncio.create_task(rt._router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}), retries=0))
            await asyncio.sleep(0.4)

            seen = []
            while not q.empty():
                seen.append(q.get_nowait())
            BUS.unsubscribe(q)
            requested = [e for e in seen if e.type == "permission.requested"]
            check("R6 permission.requested was published by the real broker",
                  len(requested) == 1, f"events={[e.type for e in seen]}")
            check("R7 naming the capability and its tier",
                  requested and requested[0].data.get("capability") == "project.delete"
                  and requested[0].data.get("tier") == "admin",
                  str(requested[0].data if requested else None))

            pend = broker.pending()
            check("R8 exactly one request is pending", len(pend) == 1, str(pend))
            rid = pend[0]["request_id"]
            check("R9 resolving it reports that it applied",
                  broker.resolve(rid, True, by="marcus") is True)

            res = await task
            entry = (res.result or {}).get("moved_to_trash", "")
            check("R10 the operation completed", res.ok is True and (res.error or "") == "",
                  f"ok={res.ok} error={res.error!r}")
            check("R11 the source directory is gone", not (projects / "toy").exists())
            trash_entries = sorted(pp.name for pp in (projects / ".trash").glob("*"))
            check("R12 exactly ONE trash entry exists",
                  trash_entries == [entry] and len(trash_entries) == 1,
                  f"entry={entry!r} trash={trash_entries}")
            check("R13 holding the real files",
                  (projects / ".trash" / entry / "main.py").exists()
                  and (projects / ".trash" / entry / "README.md").exists())
            check("R14 the tool result is truthful about what happened",
                  (res.result or {}).get("ok") is True
                  and (res.result or {}).get("files") == toy_files
                  and (res.result or {}).get("recoverable") is True
                  and entry in str((res.result or {}).get("note", "")),
                  f"result={res.result}")
            check("R15 no request is left pending", broker.pending() == [])
            check("R16 the audit records exactly one approval",
                  sum(1 for e in broker.audit_log(limit=50)
                      if e.get("outcome") == "approved") == 1,
                  str([e.get("outcome") for e in reversed(broker.audit_log(limit=50))]))

            # ── RESTORE, approved ───────────────────────────────────────────
            task = asyncio.create_task(rt._router.execute(
                ToolCall(name="project.restore", args={"entry": entry}), retries=0))
            await asyncio.sleep(0.4)
            pend = broker.pending()
            check("R17 restore also asks permission", len(pend) == 1, str(pend))
            broker.resolve(pend[0]["request_id"], True, by="marcus")
            res = await task
            check("R18 the content is restored exactly as it was",
                  res.ok
                  and (projects / "toy" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
                  and (projects / "toy" / "README.md").read_text(encoding="utf-8") == "# toy\n",
                  f"result={res.result}")
            check("R19 and the trash entry is consumed, not duplicated",
                  [pp.name for pp in (projects / ".trash").glob("*")] == [],
                  str([pp.name for pp in (projects / ".trash").glob("*")]))

            # ── DELETE, denied ──────────────────────────────────────────────
            task = asyncio.create_task(rt._router.execute(
                ToolCall(name="project.delete", args={"name": "toy"}), retries=0))
            await asyncio.sleep(0.4)
            broker.resolve(broker.pending()[0]["request_id"], False, by="marcus")
            res = await task
            check("R20 a denied delete leaves the project untouched",
                  (res.result or {}).get("status") == "not_approved"
                  and (projects / "toy" / "main.py").exists(),
                  f"result={res.result}")
            check("R21 audited as rejected, never as approved",
                  sum(1 for e in broker.audit_log(limit=50)
                      if e.get("outcome") == "rejected") == 1
                  and sum(1 for e in broker.audit_log(limit=50)
                          if e.get("outcome") == "approved") == 2,
                  str([e.get("outcome") for e in reversed(broker.audit_log(limit=50))]))
            check("R22 nothing pending at the end", broker.pending() == [])
            check("R23 Marcus's real projects directory was never involved",
                  str(projects).startswith(str(root)) and "Desktop" not in str(projects),
                  str(projects))

            # R24/R25 record a LIMIT, not an achievement. The backend emits
            # `permission.expired`; no frontend consumes it. Claiming "the UI
            # withdraws the button" would be unfounded, so the absence is pinned
            # here instead — if an approval UI ever lands, this check fails and
            # whoever adds it has to test the lifecycle properly (Stage-10 case 8).
            fe = REPO / "frontend" / "src"
            consumers = []
            if fe.exists():
                for f in list(fe.rglob("*.jsx")) + list(fe.rglob("*.js")):
                    txt = f.read_text(encoding="utf-8", errors="ignore")
                    if "permission.requested" in txt or "permission.expired" in txt:
                        consumers.append(f.name)
            check("R24 the broker emits permission.expired on abandonment",
                  "permission.expired" in
                  (REPO / "core" / "permissions.py").read_text(encoding="utf-8"))
            check("R25 NO frontend approval lifecycle exists yet — not claimed as tested",
                  consumers == [],
                  f"consumers found: {consumers} — if this fails, the frontend "
                  f"approval surface now exists and MUST be tested rather than assumed")

    asyncio.run(run())


if __name__ == "__main__":
    print("=" * 72)
    print("P10 PRE-FLIGHT — permission / tool timeout handshake")
    print(f"scaled timing: old boundary {OLD_BOUNDARY:g}s | window {WINDOW:g}s | "
          f"budget {BUDGET:g}s | approve at {APPROVE_AT:g}s")
    print("=" * 72)
    for fn in (case_a, case_a_wiring, case_b, case_c, case_d, case_e, case_f,
               case_g, case_h, case_2f, case_runtime):
        fn()
    print("\n" + "=" * 72)
    print(f"RESULT: {'ALL PASS' if not FAIL else 'FAILURES'}  "
          f"({len(PASS)} passed, {len(FAIL)} failed)")
    for f in FAIL:
        print(f"  FAILED: {f}")
    print("=" * 72)
    sys.exit(1 if FAIL else 0)
