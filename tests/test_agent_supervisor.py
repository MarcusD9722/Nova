"""Full-audit coverage for core/agent_supervisor.py — the last uncovered module.

This drives multi-session goals: a `__decide__` step asks the model for the
next move, tool steps execute it, and the cycle repeats under a step budget.
It runs unattended, so every failure here is invisible by construction.

Real MemoryUnifier and ToolRouter; the model is scripted per case.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, ScriptedLLM

from core.agent_supervisor import AgentSupervisor, SupervisorConfig
from core.tool_router import ToolRouter
from memory.unifier import MemoryUnifier

check = Checks()


async def build(tmp: Path, reply: str, tools: dict | None = None):
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()
    llm = ScriptedLLM()
    llm.default_reply = reply

    async def _ok(_args):
        return {"result": "fine"}

    async def _explodes(_args):
        raise RuntimeError("tool blew up")

    router = ToolRouter(tools if tools is not None else {"demo.ok": _ok, "demo.bad": _explodes},
                        {"demo.ok": "A tool that works", "demo.bad": "A tool that fails"})
    sup = AgentSupervisor(
        memory=mem, llm=llm, router=router,
        tool_descriptions={"demo.ok": "works", "demo.bad": "fails"},
        cfg=SupervisorConfig(tick_seconds=0.05, max_retries=2, max_steps_per_goal=6),
    )
    return mem, sup, llm


async def seed_goal(mem, title="Tidy the garage"):
    gid = await mem.create_goal(project_name="temp", title=title, objective=title,
                                success_criteria="it is done")
    await mem.enqueue_goal_task(goal_id=gid, project_name="temp", tool_name="__decide__", args={})
    return gid


async def drain(sup, seconds=3.0):
    sup.start()
    await asyncio.sleep(seconds)
    await sup.stop()


async def test_final_decision(tmp: Path) -> None:
    check.section("A 'final' decision completes the goal")
    mem, sup, _ = await build(tmp / "g1", '{"type":"final","message":"All tidy."}')
    gid = await seed_goal(mem)
    await drain(sup, 1.5)

    goals = await mem.list_goals(project_name="temp")
    g = next((x for x in goals if x["goal_id"] == str(gid)), None)
    check(g is not None and g["status"] == "completed", f"the goal is marked completed ({g and g['status']})")
    tasks = await mem.list_goal_tasks(goal_id=str(gid))
    check(all(t["status"] in ("done", "failed") for t in tasks), "no task is left claimed")


async def test_question_pauses(tmp: Path) -> None:
    check.section("A 'question' decision pauses and asks")
    mem, sup, _ = await build(tmp / "g2", '{"type":"question","message":"Which shelf first?"}')
    gid = await seed_goal(mem)
    await drain(sup, 1.5)

    goals = await mem.list_goals(project_name="temp")
    g = next((x for x in goals if x["goal_id"] == str(gid)), None)
    check(g is not None and g["status"] == "paused", f"the goal pauses for input ({g and g['status']})")


async def test_tool_chain(tmp: Path) -> None:
    check.section("A 'tool' decision enqueues the call AND the next decision")
    mem, sup, _ = await build(tmp / "g3", '{"type":"tool","name":"demo.ok","args":{"x":1}}')
    gid = await seed_goal(mem)
    await drain(sup, 2.0)

    tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
    names = [t["tool_name"] for t in tasks]
    check("demo.ok" in names, f"the chosen tool was enqueued and run ({set(names)})")
    executed = [t for t in tasks if t["tool_name"] == "demo.ok" and t["status"] == "done"]
    check(bool(executed), "the tool task completed")

    # The step budget is what stops an endless decide->tool->decide loop.
    check(len(tasks) <= 20, f"the step budget bounds the loop ({len(tasks)} tasks)")
    goals = await mem.list_goals(project_name="temp")
    g = next((x for x in goals if x["goal_id"] == str(gid)), None)
    check(g["status"] in ("paused", "active"), f"a never-finishing goal ends paused, not spinning ({g['status']})")


async def test_bad_decisions_do_not_orphan(tmp: Path) -> None:
    """Regression: an unparseable or unknown decision raised inside the loop's
    try, was caught by the outer handler, and the CLAIMED task was never
    completed — a goal stalled forever with nothing reporting it."""
    check.section("A malformed decision must not orphan the claimed task")

    for label, reply in (
        ("unparseable JSON", "I think we should probably start with the shelves."),
        ("unknown type", '{"type":"teleport","message":"nope"}'),
        ("tool with no name", '{"type":"tool","name":"","args":{}}'),
    ):
        mem, sup, _ = await build(tmp / f"g4-{label[:6]}", reply)
        gid = await seed_goal(mem)
        await drain(sup, 2.0)
        tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        stuck = [t for t in tasks if t["status"] not in ("done", "failed")]
        check(not stuck, f"{label}: no task left claimed/running ({len(stuck)} stuck)")
        check(any(t["status"] == "failed" for t in tasks) or
              any((t.get("last_error") or "") for t in tasks),
              f"{label}: the failure is recorded honestly")


async def test_failing_tool_is_not_retried_then_failed(tmp: Path) -> None:
    """A failing tool is recorded honestly, and an UNSAFE one is not re-run.

    Two regressions in one place. Originally the retry/backoff branch was guarded
    by `except`, so it was dead code and a failing tool was recorded as 'done'.
    This test then asserted the opposite extreme — that a failing tool IS retried
    — which became wrong once retry safety landed: `demo.bad` is not declared
    retry-safe, so re-running it could repeat a side effect. The honest contract
    is: run once, record the failure, do not requeue.
    """
    check.section("An unsafe failing tool is NOT retried, and is marked failed")
    mem, sup, _ = await build(tmp / "g5", '{"type":"tool","name":"demo.bad","args":{}}')
    sup._cfg = SupervisorConfig(tick_seconds=0.05, max_retries=1, max_steps_per_goal=6)
    gid = await seed_goal(mem)
    await drain(sup, 10.0)

    tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
    bad = [t for t in tasks if t["tool_name"] == "demo.bad"]
    check(bool(bad), "the failing tool task exists")
    check(all(t["status"] != "done" for t in bad),
          f"a tool that failed is NEVER recorded as done ({[t['status'] for t in bad]})")
    check(all(int(t.get("attempts") or 0) == 0 for t in bad),
          f"an UNSAFE tool is not requeued ({[t.get('attempts') for t in bad]})")
    check(any("not retried" in (t.get("last_error") or "") for t in bad),
          f"and the record says why ({[(t.get('last_error') or '')[:50] for t in bad]})")


async def test_retry_safe_tool_is_still_retried(tmp: Path) -> None:
    """The capability is aimed, not removed: a READ-ONLY tool still retries."""
    check.section("A declared retry-safe tool is still retried")

    attempts = {"n": 0}

    async def flaky_read(_args):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient blip")
        return {"result": "eventually fine"}

    mem, sup, _ = await build(tmp / "g5b",
                              '{"type":"tool","name":"demo.read","args":{}}',
                              tools={"demo.read": flaky_read})
    sup._router.set_retry_safe("demo.read", True)
    sup._cfg = SupervisorConfig(tick_seconds=0.05, max_retries=2, max_steps_per_goal=6)
    gid = await seed_goal(mem)
    await drain(sup, 12.0)

    check(attempts["n"] >= 2,
          f"the read-only tool was retried ({attempts['n']} invocations)")
    tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
    read = [t for t in tasks if t["tool_name"] == "demo.read"]
    check(any(t["status"] == "done" for t in read),
          f"and eventually succeeded ({[t['status'] for t in read]})")


async def test_step_budget(tmp: Path) -> None:
    check.section("The per-goal step budget pauses runaway goals")
    mem, sup, llm = await build(tmp / "g6", '{"type":"tool","name":"demo.ok","args":{}}')
    gid = await seed_goal(mem)
    await drain(sup, 4.0)

    goals = await mem.list_goals(project_name="temp")
    g = next((x for x in goals if x["goal_id"] == str(gid)), None)
    tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=100)
    executed = sum(1 for t in tasks if t["status"] in ("done", "failed"))
    check(g["status"] == "paused", f"the goal is paused rather than looping forever ({g['status']})")
    check(executed <= sup._cfg.max_steps_per_goal + 4,
          f"execution stopped near the budget ({executed} steps, budget {sup._cfg.max_steps_per_goal})")
    check(not [t for t in tasks if t["status"] not in ("done", "failed")],
          "nothing is left claimed after the pause")


async def test_lifecycle(tmp: Path) -> None:
    check.section("Supervisor lifecycle")
    mem, sup, _ = await build(tmp / "g7", '{"type":"final","message":"done"}')
    sup.start()
    first = sup._task
    sup.start()
    check(sup._task is first, "start() is idempotent — no duplicate loop task")
    await sup.stop()
    check(sup._task.done(), "stop() ends the loop task")
    await sup.stop()
    check(True, "stop() twice does not raise")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_final_decision(tmp)
        await test_question_pauses(tmp)
        await test_tool_chain(tmp)
        await test_bad_decisions_do_not_orphan(tmp)
        await test_failing_tool_is_not_retried_then_failed(tmp)
        await test_retry_safe_tool_is_still_retried(tmp)
        await test_step_budget(tmp)
        await test_lifecycle(tmp)
    check.finish()


asyncio.run(main())
