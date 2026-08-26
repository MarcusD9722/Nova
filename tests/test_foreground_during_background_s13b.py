"""Talking to Nova while it works must be safe for both (Stage 13B, journey 6).

INVARIANTS UNDER TEST

  I26  A foreground turn and background work never share a model call.
  I27  Talking to Nova does not disturb work already running.
  I28  Background work does not answer, delay or contaminate the foreground.
  I29  Every observation is attributed to the operation that produced it.

WHY THIS ONE IS EASY TO GET WRONG

The dangerous property here is not "did both finish" — it is CONCURRENCY, and
concurrency is exactly where a test can invent a defect that is not there.
Earlier in this stage I produced a phantom privacy leak by slicing a shared
global prompt list under concurrency: the "leaked" prompt was the other
participant's own system prompt, sitting where I assumed mine would be. The
measurement was wrong, not the product.

So nothing here is attributed by position, arrival order or timing:

  * the model-call overlap is read from `ScriptedLLM.max_concurrent`, a counter
    the harness increments and decrements around each call. It is a fact about
    the calls themselves, not an inference from when they were seen.
  * the background side is read from the authoritative `tasks` row by task_id.
  * the foreground side is read from the HTTP response to the request that
    asked, not from a shared log.

A shared llama.cpp context cannot survive two concurrent calls; the harness
records the high-water mark for precisely that reason.

Run:  venv\\Scripts\\python.exe tests\\test_foreground_during_background_s13b.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()

GAME = "flappy-bird"
CALC = "quickcalc"


async def _task_rows(mem, goal_id):
    return await mem.list_goal_tasks(goal_id=str(goal_id), limit=50)


async def main() -> None:
    async with boot(default_reply="Noted.") as nova:
        from core.agent_supervisor import AgentSupervisor, SupervisorConfig
        from core.tool_router import ToolRouter

        mem = nova.memory
        llm = nova.llm

        check.section("journey 6: a chat turn arrives while a goal is running")

        entered, release = asyncio.Event(), asyncio.Event()
        ran: list[str] = []

        async def slow_tool(_a):
            ran.append("demo.slow")
            entered.set()
            await release.wait()
            return {"ok": True, "wrote": "game.js"}

        decisions = {"n": 0}

        def decide(_prompt: str) -> str:
            decisions["n"] += 1
            if decisions["n"] == 1:
                return '{"type":"tool","name":"demo.slow","args":{}}'
            return '{"type":"final","message":"the pause menu is in"}'

        # The supervisor's decisions are scripted; every other model call in the
        # process (the chat turn below) falls through to default_reply.
        llm.when(lambda t: "goal" in t.lower() or "decide" in t.lower(),
                 decide, label="decide")

        sup = AgentSupervisor(
            memory=mem, llm=llm, router=ToolRouter({"demo.slow": slow_tool}, {}),
            tool_descriptions={"demo.slow": "writes a file"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                 max_steps_per_goal=6))

        goal_id = await mem.create_goal(project_name=GAME,
                                        title="add a pause menu",
                                        objective="pause menu",
                                        success_criteria="it pauses")
        await mem.enqueue_goal_task(goal_id=goal_id, project_name=GAME,
                                    tool_name="__decide__", args={})

        concurrent_before = llm.max_concurrent
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=60)
            check(ran == ["demo.slow"],
                  f"the background tool is mid-flight ({ran})")

            # The user says something, about a DIFFERENT project, right now.
            r = await nova.http.post(
                "/chat", json={"message": "how is quickcalc coming along?"})
            check(r.status_code == 200,
                  f"the chat turn is answered while work runs ({r.status_code})")
            body = r.json() if r.status_code == 200 else {}
            reply = str(body.get("assistant") or "")
            check(reply.strip() != "",
                  f"and the answer is a real one ({reply[:60]!r})")
            check(bool(body.get("conversation_id")),
                  "with its own conversation id, so the turn is attributable")

            # The background work is still exactly where it was: talking to
            # Nova did not finish it, fail it, or move it on.
            rows = await _task_rows(mem, goal_id)
            running = [t for t in rows if str(t.get("status")) == "running"]
            check(len(running) == 1,
                  f"the background step is still running ({[(t.get('tool_name'), t.get('status')) for t in rows]})")
            check(str(running[0].get("project_name")) == GAME,
                  f"and still belongs to its own project "
                  f"({running[0].get('project_name')!r})")

            release.set()
            for _ in range(400):
                await asyncio.sleep(0.05)
                row = await mem.get_goal(goal_id=goal_id)
                if str((row or {}).get("status")) != "active":
                    break
        finally:
            release.set()
            await sup.stop()

        check.section("what each side ended up with")

        goal = await mem.get_goal(goal_id=goal_id) or {}
        rows = await _task_rows(mem, goal_id)
        statuses = [(str(t.get("tool_name")), str(t.get("status"))) for t in rows]
        check(str(goal.get("status")) == "completed",
              f"the goal finished on its own terms ({goal.get('status')!r})")
        check(all(s == "done" for _n, s in statuses),
              f"every step of it is done ({statuses})")
        check(all(str(t.get("project_name")) == GAME for t in rows),
              f"all of it attributed to {GAME} "
              f"({sorted({str(t.get('project_name')) for t in rows})})")

        # The chat turn asked about the OTHER project. It must not have created
        # work, and the goal above must not have been re-attributed to it.
        other = [t for t in await mem.list_goal_tasks(limit=100)
                 if str(t.get("project_name")) == CALC]
        check(not other,
              f"the chat turn created no work of its own ({len(other)})")

        check.section("the model was never called twice at once")

        # THE property. A shared llama.cpp context cannot survive it, and this
        # is a counter on the calls, not an inference from ordering.
        check(llm.max_concurrent <= 1,
              f"never two model calls at once (high-water mark "
              f"{llm.max_concurrent}, was {concurrent_before} before)")
        check(decisions["n"] >= 2,
              f"the background really did call the model ({decisions['n']} decisions)")
        check(len(llm.prompts) > decisions["n"],
              f"and so did the foreground ({len(llm.prompts)} prompts total)")

        check.finish()


if __name__ == "__main__":
    run(main)
