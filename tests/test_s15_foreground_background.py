"""Stage 15 — foreground and background, held apart by barriers not by luck.

Every interleaving here is deterministic. A background build is paused INSIDE
its own model call -- a real production point, reached by wrapping the async
`chat` -- so the test knows exactly where the background work is standing while
the foreground turn runs. No sleeps, no scheduling hope.

LIVENESS IS ASSERTED ALONGSIDE EVERY ABSENCE. "B did not finish" is also what a
B that never started looks like, so each case proves B was genuinely mid-flight
(the barrier was reached, `is_building` is true) and, after release, that B
really does finish and publish its own events.

Attribution is always read from the authoritative payload or row -- project,
goal, task, generation, revision, conversation -- never from the assistant's
prose.

  I18  background failure cannot become foreground success
  I19  foreground success cannot conceal background failure
  I20  failures stay associated with their real project
  I28  project A never modifies project B
  I30  event payloads identify their true origin

Run:  venv\\Scripts\\python.exe tests\\test_s15_foreground_background.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import COMPLETE, FAILED, INCONCLUSIVE, PASSED  # noqa: E402
from s15_bus import Recorder  # noqa: E402

check = Checks()


#: A token that appears ONLY in the background instructions, so the barrier
#: catches that build and nothing else.
#:
#: Keying the barrier on the project SLUG deadlocks the test, and finding out
#: why was worth the time: the tool decider's prompt carries a Context blob
#: listing EVERY project, so the foreground turn's prompt contains the
#: background project's name too, and the foreground turn parks on the barrier
#: meant for the build. The same Context blob produced a false
#: cross-conversation-leak reading earlier in this stage.
TOKEN = "ZZBARRIERZZ"


class Barrier:
    """Pause the model call for one piece of work, deterministically.

    Wraps `llm.chat`. When a prompt carries the token, the call parks until
    released -- so the background build stands at a known point in its own
    production path while the foreground turn runs.
    """

    def __init__(self, nova, token: str = TOKEN):
        self.token = token
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self._llm = nova.llm
        self._real = nova.llm.chat

        async def gated(messages, **kw):
            text = "\n".join(str(m.get("content", "")) for m in messages or [])
            if token in text:
                self.calls += 1
                self.reached.set()
                await self.release.wait()
            return await self._real(messages, **kw)

        nova.llm.chat = gated

    def let_go(self) -> None:
        self.release.set()

    def restore(self) -> None:
        self.release.set()
        self._llm.chat = self._real


def script_improve(nova) -> None:
    """Let an improve run to its end, so releasing the barrier proves a path.

    Without this the default reply cannot produce a plan, `_improve` raises
    `no improvement plan produced` and publishes `project.error` -- which is
    also what a build that never resumed would look like. Scripting the two
    generation steps makes the released build reach the announcer, so the
    terminal `project.state_changed` is real evidence that B finished.
    """
    nova.llm.when(lambda t: '"changes"' in t,
                  json.dumps({"changes": [{"path": "main.py",
                                           "what": "print more"}],
                              "summary": "print more"}),
                  label="improve-plan")
    nova.llm.when(lambda t: "Return the COMPLETE improved file" in t,
                  # >= 40 characters: `_looks_like_failed_generation`
                  # rejects anything shorter as a truncated response, and
                  # a rejected file makes the improve publish `project.error`.
                  "```python\ndef main():\n    print('x')\n    print('y')\n\nmain()\n```",
                  label="improve-file")


def seed(nova, slug: str) -> Path:
    p = nova.projects_dir / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {slug}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


async def settle(nova, slug: str) -> None:
    pb = nova.runtime._project_builder
    for _ in range(200):
        if not pb.is_building(slug):
            return
        await asyncio.sleep(0.02)


async def contract(nova, slug: str, verdict: str | None = None):
    svc = nova.runtime.completion
    rev = await svc.record_request(slug=slug,
                                   request_text="a tool that adds numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds numbers", "origin_quote": "adds numbers",
         "verify_kind": "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    if verdict is not None:
        ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
        await svc.record_verdict(context=ctx, verdict=verdict,
                                 error="broke" if verdict == FAILED else "")
    return rev, ids


# ── 1 & 2: a foreground turn beside a paused background build ──────────────

async def test_foreground_runs_while_background_is_held():
    check.section("I28 A's turn cannot touch B, and B cannot answer for A")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="alpha", confidence=0.99)
        conv_a = str(uuid4())

        script_improve(nova)
        bar = Barrier(nova)
        try:
            with Recorder() as rec:
                started = await pb.improve(slug="bravo",
                                           instructions=f"make bravo nicer {TOKEN}")
                check(started.get("started") is True,
                      f"B's background work started ({started})")
                await asyncio.wait_for(bar.reached.wait(), timeout=20)
                check(pb.is_building("bravo") and bar.calls >= 1,
                      f"and it is genuinely mid-flight, held at the barrier "
                      f"inside its own model call (calls={bar.calls})")
                check(not pb.is_building("alpha"),
                      "while A is not building anything")

                # MEASURED, AND IT SHAPES THIS WHOLE SUITE: a chat turn cannot
                # run here. Nova has ONE model context and calls through it are
                # serialised, so a build parked INSIDE its model call holds the
                # only slot and a foreground turn blocks until it is released.
                # That is the single-GPU design, not a defect -- but it means
                # "foreground turn concurrent with a parked model call" is not
                # a reachable state, and a test that appeared to do it would be
                # testing something else. So the isolation below is asserted
                # from authoritative reads, which need no model.
                before_b = await nova.runtime.completion.evaluate(slug="bravo")
                v_a = await nova.runtime.completion.evaluate(slug="alpha")
                said_a = await pb.status_text("alpha")
                after_b = await nova.runtime.completion.evaluate(slug="bravo")

                check(v_a.state == COMPLETE,
                      f"A's completion is readable while B is parked "
                      f"({v_a.state})")
                check("bravo" not in said_a,
                      f"and A's status says nothing about B ({said_a[:70]!r})")

                check(before_b.state == after_b.state,
                      f"B's completion state did not move during A's turn "
                      f"({before_b.state} -> {after_b.state})")
                check(not rec.for_project("project.completed", "bravo"),
                      "and B announced no completion while parked")
                # LIVENESS for that absence: B's own start event IS here.
                check(rec.for_project("project.started", "bravo"),
                      f"the recorder saw B start, so the absence is real "
                      f"({rec.kinds()})")

                # Now release B and prove the path is live end to end.
                bar.let_go()
                await settle(nova, "bravo")
                check(rec.for_project("project.state_changed", "bravo"),
                      f"released, B finishes and announces for ITSELF "
                      f"({[e.data.get('project') for e in rec.of('project.state_changed')]})")
                for e in rec.of("project.state_changed"):
                    check(str(e.data.get("project")) in ("alpha", "bravo"),
                          f"every announcement names a real project "
                          f"({e.data.get('project')!r})")
                check(not [e for e in rec.for_project("project.state_changed",
                                                      "alpha")
                           if str(e.data.get("mode") or "")],
                      "and none of B's finishing announced anything for A")
        finally:
            bar.restore()
            await settle(nova, "bravo")


# ── 3: a correction to A while B executes ──────────────────────────────────

async def test_a_correction_to_a_does_not_reach_b():
    check.section("I28 A's revision moves; B's generation does not")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        seed(nova, "alpha")
        seed(nova, "bravo")
        rev_a, _ = await contract(nova, "alpha", PASSED)
        goal_b = await nova.memory.create_goal(project_name="bravo",
                                               title="bravo work",
                                               objective="bravo work")
        gen_b_before = [g for g in await nova.memory.list_goals(
            project_name="bravo")][0]["generation"]

        bar = Barrier(nova)
        try:
            await pb.improve(slug="bravo", instructions=f"make bravo nicer {TOKEN}")
            await asyncio.wait_for(bar.reached.wait(), timeout=20)

            # A is corrected while B is standing at the barrier.
            rev_a2 = await nova.runtime.completion.record_request(
                slug="alpha", request_text="a tool that adds and subtracts")
            check(rev_a2 == rev_a + 1,
                  f"A's requirement revision advanced ({rev_a} -> {rev_a2})")

            gen_b_after = [g for g in await nova.memory.list_goals(
                project_name="bravo")][0]["generation"]
            check(gen_b_after == gen_b_before,
                  f"B's generation is untouched ({gen_b_before} -> "
                  f"{gen_b_after})")
            # NOT a leak, and worth stating plainly because the first version
            # of this test asserted the opposite: `_improve` records a NEW
            # requirement revision for the improved project BEFORE touching a
            # file (Stage 14 -- "the requirements changed"). So B has a
            # requirement here. What matters is WHOSE it is.
            rev_b = await nova.memory.current_requirement(project_name="bravo")
            check(rev_b is not None
                  and str(rev_b["project_name"]) == "bravo",
                  f"B's own requirement belongs to B "
                  f"({(rev_b or {}).get('project_name')})")
            check(TOKEN in str((rev_b or {}).get("request_text", "")),
                  f"and carries B's instructions, not A's correction "
                  f"({str((rev_b or {}).get('request_text'))[:60]!r})")
            check("adds and subtracts" not in str(
                      (rev_b or {}).get("request_text", "")),
                  "A's new request text did not land on B")

            v_a = await nova.runtime.completion.evaluate(slug="alpha")
            check(v_a.state != COMPLETE,
                  f"A's old evidence no longer satisfies A ({v_a.state})")
        finally:
            bar.restore()
            await settle(nova, "bravo")


# ── 4 & 5: opposite outcomes, side by side ─────────────────────────────────

async def test_b_failure_while_a_succeeds():
    check.section("I18/I19 two projects, two outcomes, no bleed")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await contract(nova, "bravo", FAILED)

        g_a = await nova.memory.create_goal(project_name="alpha",
                                            title="alpha work",
                                            objective="alpha work")
        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        await nova.memory.enqueue_goal_task(goal_id=g_a, project_name="alpha",
                                            tool_name="demo.a")
        await nova.memory.enqueue_goal_task(goal_id=g_b, project_name="bravo",
                                            tool_name="demo.b")
        # Claim and finish them with opposite outcomes.
        for _ in range(2):
            c = await nova.memory.claim_next_goal_task()
            ok = str(c["project_name"]) == "alpha"
            await nova.memory.complete_goal_task(
                task_id=str(c["task_id"]), status="done" if ok else "failed",
                result={"ok": ok}, error="" if ok else "b broke",
                expected_generation=int(c["generation"]))

        rows_a = await nova.memory.list_goal_tasks(goal_id=str(g_a))
        rows_b = await nova.memory.list_goal_tasks(goal_id=str(g_b))
        check(rows_a[0]["status"] == "done" and rows_b[0]["status"] == "failed",
              f"opposite work outcomes ({rows_a[0]['status']}, "
              f"{rows_b[0]['status']})")
        check(str(rows_b[0]["project_name"]) == "bravo"
              and "b broke" in str(rows_b[0]["last_error"]),
              "B's failure is filed against B, with B's error")

        va = await nova.runtime.completion.evaluate(slug="alpha")
        vb = await nova.runtime.completion.evaluate(slug="bravo")
        check(va.state == COMPLETE and vb.state != COMPLETE,
              f"and the completion axes stay opposite too ({va.state}, "
              f"{vb.state})")

        said_a = await nova.runtime._project_builder.status_text("alpha")
        said_b = await nova.runtime._project_builder.status_text("bravo")
        check("b broke" not in said_a and "bravo" not in said_a,
              f"A's status says nothing about B ({said_a[:70]!r})")
        check("complete" not in said_b.split(".")[0].lower(),
              f"and B's does not inherit A's completion ({said_b[:70]!r})")


# ── 6 & 7: cancellation and permission, aimed at one project ───────────────

async def test_cancelling_a_leaves_b_running():
    check.section("I21 A's cancel fences A's work and nothing else")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        g_a = await nova.memory.create_goal(project_name="alpha",
                                            title="alpha work",
                                            objective="alpha work")
        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        t_a = await nova.memory.enqueue_goal_task(goal_id=g_a,
                                                  project_name="alpha",
                                                  tool_name="demo.a")
        t_b = await nova.memory.enqueue_goal_task(goal_id=g_b,
                                                  project_name="bravo",
                                                  tool_name="demo.b")
        gen_b = [g for g in await nova.memory.list_goals(project_name="bravo")][0]["generation"]

        await nova.memory.cancel_goal(goal_id=g_a)

        rows_b = await nova.memory.list_goal_tasks(goal_id=str(g_b))
        g_b_row = [g for g in await nova.memory.list_goals(project_name="bravo")][0]
        check(int(g_b_row["generation"]) == int(gen_b),
              f"B's generation is unchanged ({gen_b} -> {g_b_row['generation']})")
        check(str(g_b_row["status"]) == "active",
              f"and B is still active ({g_b_row['status']})")
        check(rows_b[0]["status"] == "queued",
              f"with its work still queued ({rows_b[0]['status']})")

        # LIVENESS: B's queued work is genuinely claimable; A's is not.
        claimed = await nova.memory.claim_next_goal_task()
        check(claimed is not None and str(claimed["project_name"]) == "bravo",
              f"B's task is claimable, A's is fenced "
              f"({(claimed or {}).get('project_name')})")
        check(str(claimed["task_id"]) == str(t_b),
              "and it is exactly B's task")


async def test_a_permission_names_one_project_while_the_other_runs():
    check.section("I34 the prompt targets A; B cannot consume it")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        seed(nova, "alpha")
        seed(nova, "bravo")

        def decide(prompt: str) -> str:
            return ('{"action":"tool","tool":"project.delete",'
                    '"args":{"name":"alpha"}}')

        nova.llm.when("agent brain for Nova", decide, label="del")
        nova.llm.rules.insert(0, nova.llm.rules.pop())
        broker = nova.runtime._permission_broker
        real = broker.await_decision

        async def fast(rid, *, timeout_s=6.0):
            return await real(rid, timeout_s=6.0)
        broker.await_decision = fast

        # B's background work here is a CLAIMED GOAL TASK, not a build: a
        # parked build holds the single model slot and the foreground turn
        # below could never run. This still gives a genuinely in-flight piece
        # of B work -- a row in `running`, owned by B's generation -- while A
        # takes the foreground.
        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        await nova.memory.enqueue_goal_task(goal_id=g_b, project_name="bravo",
                                            tool_name="demo.b")
        in_flight = await nova.memory.claim_next_goal_task()
        check(in_flight is not None
              and str(in_flight["project_name"]) == "bravo",
              f"B has work genuinely in flight "
              f"({(in_flight or {}).get('project_name')})")

        try:
            seen: dict = {}

            async def answer_when_asked():
                for _ in range(400):
                    await asyncio.sleep(0.02)
                    rows = broker.pending()
                    if rows:
                        seen.update(rows[0])
                        broker.resolve(str(rows[0]["request_id"]), False)
                        return

            with Recorder() as rec:
                await asyncio.gather(
                    nova.brain.chat("Delete the project alpha",
                                    conversation_id=str(uuid4())),
                    answer_when_asked())
                asked = rec.of("permission.requested")
                targets = rec.targets_of("permission.requested")

            check(len(asked) == 1, f"exactly one request was raised ({len(asked)})")
            check(targets == ["alpha"],
                  f"aimed at A, while B's work is in flight ({targets})")
            check(str(seen.get("capability")) == "project.delete",
                  f"and the pending row says what it would do "
                  f"({seen.get('capability')!r})")
            check((nova.projects_dir / "alpha" / "PROJECT.md").exists(),
                  "A survives, because the answer was no")
            check((nova.projects_dir / "bravo" / "PROJECT.md").exists(),
                  "and B was never a candidate for deletion")
            rows_b = await nova.memory.list_goal_tasks(goal_id=str(g_b))
            check(rows_b and rows_b[0]["status"] == "running",
                  f"B's task is still running, untouched by A's prompt "
                  f"({rows_b[0]['status'] if rows_b else None})")
            # LIVENESS: B's in-flight task can still be completed normally.
            applied = await nova.memory.complete_goal_task(
                task_id=str(in_flight["task_id"]), status="done",
                result={"ok": True},
                expected_generation=int(in_flight["generation"]))
            check(applied == "applied",
                  f"and completes on its own generation afterwards ({applied})")
        finally:
            pass


async def test_two_projects_hold_different_generations_at_once():
    check.section("I27 A moves to a new generation; B stays on its own")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        g_a = await nova.memory.create_goal(project_name="alpha",
                                            title="alpha work",
                                            objective="alpha work")
        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        t_a = await nova.memory.enqueue_goal_task(goal_id=g_a,
                                                  project_name="alpha",
                                                  tool_name="demo.a")
        claimed = await nova.memory.claim_next_goal_task()
        gen_a0 = int(claimed["generation"])

        await nova.memory.cancel_goal(goal_id=g_a)     # A -> generation N+1
        rows = {str(g["project_name"]): int(g["generation"])
                for g in await nova.memory.list_goals(limit=10)}
        check(rows["alpha"] == gen_a0 + 1 and rows["bravo"] == gen_a0,
              f"the two projects now sit on different generations ({rows})")

        # A's in-flight worker returns success against the OLD generation.
        outcome = await nova.memory.complete_goal_task(
            task_id=str(t_a), status="done", result={"ok": True},
            expected_generation=gen_a0)
        check(outcome == "superseded",
              f"A's stale result is superseded ({outcome})")

        # LIVENESS: the same call shape applied to B's CURRENT generation works.
        t_b = await nova.memory.enqueue_goal_task(goal_id=g_b,
                                                  project_name="bravo",
                                                  tool_name="demo.b")
        c_b = await nova.memory.claim_next_goal_task()
        applied = await nova.memory.complete_goal_task(
            task_id=str(c_b["task_id"]), status="done", result={"ok": True},
            expected_generation=int(c_b["generation"]))
        check(applied == "applied",
              f"while B's current-generation completion applies normally "
              f"({applied})")
        check(str(c_b["project_name"]) == "bravo",
              f"and it really was B's task ({c_b['project_name']!r})")


# ── 8: a real foreground TURN, beside a genuinely running background task ──

async def test_foreground_turn_while_bs_task_is_active():
    check.section("I28/I30 A's turn runs while B's task is genuinely running")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="alpha", confidence=0.99)

        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        await nova.memory.enqueue_goal_task(goal_id=g_b, project_name="bravo",
                                            tool_name="demo.b")
        running = await nova.memory.claim_next_goal_task()
        # `claim_next_goal_task` returns the claim, not the row: the status it
        # wrote lives in storage, so that is where it gets read from.
        claimed_rows = await nova.memory.list_goal_tasks(goal_id=str(g_b))
        check(running is not None
              and str(running["project_name"]) == "bravo"
              and claimed_rows
              and str(claimed_rows[0]["status"]) == "running",
              f"B's task is running before A says a word "
              f"({(running or {}).get('project_name')}, "
              f"{claimed_rows[0]['status'] if claimed_rows else None})")

        conv_a = uuid4()
        before = await nova.runtime.completion.evaluate(slug="bravo")
        with Recorder() as rec:
            res = await nova.brain.chat("Is alpha done?",
                                        conversation_id=str(conv_a))
        after = await nova.runtime.completion.evaluate(slug="bravo")

        check(str(res.conversation_id) == str(conv_a),
              f"the turn is filed under A's conversation "
              f"({res.conversation_id} vs {conv_a})")
        rows = await nova.memory.list_goal_tasks(goal_id=str(g_b))
        check(rows and str(rows[0]["status"]) == "running",
              f"B's task is untouched by A's turn "
              f"({rows[0]['status'] if rows else None})")
        check(int(rows[0]["generation"]) == int(running["generation"]),
              f"on B's own generation ({rows[0]['generation']})")
        check(before.state == after.state,
              f"and B's completion state did not move ({before.state})")
        strays = [e for e in rec.events
                  if str(e.type).startswith("project.")
                  and str((e.data or {}).get("project")) == "bravo"]
        check(not strays,
              f"A's turn published nothing about B ({[e.type for e in strays]})")

        # LIVENESS for all of that absence: B's task really can finish, on its
        # own generation, right after.
        applied = await nova.memory.complete_goal_task(
            task_id=str(running["task_id"]), status="done", result={"ok": True},
            expected_generation=int(running["generation"]))
        check(applied == "applied",
              f"B's work completes normally afterwards ({applied})")


# ── 9: B's completion LANDS while A's turn is in flight ────────────────────

async def test_b_completes_while_as_turn_is_in_flight():
    check.section("I30 B finishing mid-turn is filed to B, not to A's turn")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="alpha", confidence=0.99)
        g_b = await nova.memory.create_goal(project_name="bravo",
                                            title="bravo work",
                                            objective="bravo work")
        await nova.memory.enqueue_goal_task(goal_id=g_b, project_name="bravo",
                                            tool_name="demo.b")
        claimed = await nova.memory.claim_next_goal_task()

        conv_a = uuid4()
        v_a_before = await nova.runtime.completion.evaluate(slug="alpha")
        bar = Barrier(nova)
        try:
            with Recorder() as rec:
                # A's turn parks INSIDE its own model call: the token rides in
                # A's message, so the interleaving point is exact.
                turn = asyncio.create_task(
                    nova.brain.chat(f"Is alpha done? {TOKEN}",
                                    conversation_id=str(conv_a)))
                await asyncio.wait_for(bar.reached.wait(), timeout=20)
                check(bar.calls >= 1 and not turn.done(),
                      f"A's turn is held mid-flight (calls={bar.calls}, "
                      f"done={turn.done()})")

                # B finishes NOW -- both axes -- while A is standing still.
                applied = await nova.memory.complete_goal_task(
                    task_id=str(claimed["task_id"]), status="done",
                    result={"ok": True},
                    expected_generation=int(claimed["generation"]))
                await nova.memory.update_goal_status(goal_id=g_b,
                                                     status="completed")
                await contract(nova, "bravo", PASSED)
                check(applied == "applied",
                      f"B's work landed while A was parked ({applied})")

                bar.let_go()
                res = await asyncio.wait_for(turn, timeout=60)

            v_b = await nova.runtime.completion.evaluate(slug="bravo")
            v_a_after = await nova.runtime.completion.evaluate(slug="alpha")
            check(v_b.state == COMPLETE,
                  f"B is complete on its own evidence ({v_b.state})")
            check(v_a_after.state == v_a_before.state,
                  f"A's completion is exactly where it was "
                  f"({v_a_before.state} -> {v_a_after.state})")
            check(str(res.conversation_id) == str(conv_a),
                  f"A's turn is still A's ({res.conversation_id})")

            rows_b = await nova.memory.list_goal_tasks(goal_id=str(g_b))
            check(rows_b and str(rows_b[0]["status"]) == "done"
                  and str(rows_b[0]["project_name"]) == "bravo",
                  f"the durable row says B, done "
                  f"({rows_b[0]['project_name'] if rows_b else None}, "
                  f"{rows_b[0]['status'] if rows_b else None})")
            rows_a = await nova.memory.list_goal_tasks(project_name="alpha")
            check(not rows_a,
                  f"and nothing was filed against A ({len(rows_a)} rows)")
            misfiled = [e for e in rec.events
                        if str(e.type).startswith("project.")
                        and str((e.data or {}).get("project")) == "alpha"]
            check(not misfiled,
                  f"no event blamed A for B's finishing "
                  f"({[e.type for e in misfiled]})")
        finally:
            bar.restore()


# ── 10: A cannot be decided while B completes ──────────────────────────────

async def test_a_inconclusive_while_b_completes():
    check.section("I19 an undecided A stays undecided while B reaches COMPLETE")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        svc = nova.runtime.completion

        # "UNKNOWN" in the brief is INCONCLUSIVE here: the vocabulary Nova
        # actually records for "a check ran and could not decide". It is
        # deliberately NOT in SATISFYING.
        rev_a = await svc.record_request(slug="alpha",
                                         request_text="a tool that adds numbers")
        ids_a = await svc.set_criteria(slug="alpha", revision=rev_a, criteria=[
            {"text": "adds numbers", "origin_quote": "adds numbers",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="alpha", revision=rev_a)
        ctx = await svc.begin_check(slug="alpha", criterion_id=ids_a[0])
        await svc.record_verdict(context=ctx, verdict=INCONCLUSIVE,
                                 error="the check could not decide")
        await contract(nova, "bravo", PASSED)

        v_a = await svc.evaluate(slug="alpha")
        v_b = await svc.evaluate(slug="bravo")
        check(v_a.state != COMPLETE,
              f"A is not complete on an undecided check ({v_a.state})")
        check(v_b.state == COMPLETE,
              f"while B, next to it, is ({v_b.state})")
        check(str(v_a.state) != str(v_b.state),
              f"the two states are genuinely different "
              f"({v_a.state} vs {v_b.state})")

        said_a = await nova.runtime._project_builder.status_text("alpha")
        said_b = await nova.runtime._project_builder.status_text("bravo")
        check("complete" not in said_a.split(".")[0],
              f"A is not described as complete ({said_a[:70]!r})")
        check("bravo" not in said_a and "alpha" not in said_b,
              f"and neither borrows the other's name "
              f"({said_a[:40]!r} / {said_b[:40]!r})")

        # LIVENESS: the very same criterion reaches COMPLETE once a check
        # DOES decide -- so the undecided state above was the reason, not a
        # broken fixture.
        ctx2 = await svc.begin_check(slug="alpha", criterion_id=ids_a[0])
        await svc.record_verdict(context=ctx2, verdict=PASSED)
        v_a2 = await svc.evaluate(slug="alpha")
        check(v_a2.state == COMPLETE,
              f"a decided check moves A to complete ({v_a.state} -> "
              f"{v_a2.state})")


# ── 11: B's permission is RESOLVED while A holds the foreground ────────────

async def test_permission_for_b_resolves_while_a_is_foreground():
    check.section("I34 a decision lands on B's request, not on A's turn")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="alpha", confidence=0.99)
        broker = nova.runtime._permission_broker
        conv_a = uuid4()

        bar = Barrier(nova)
        try:
            with Recorder() as rec:
                raised = await broker.request(
                    "project.delete", details={"project": "bravo",
                                               "name": "bravo"})
                rid = str(raised.get("request_id") or "")
                check(raised.get("decision") == "needs_confirmation" and rid,
                      f"B's request is pending a human ({raised.get('decision')})")
                waiter = asyncio.create_task(
                    broker.await_decision(rid, timeout_s=60))

                turn = asyncio.create_task(
                    nova.brain.chat(f"Is alpha done? {TOKEN}",
                                    conversation_id=str(conv_a)))
                await asyncio.wait_for(bar.reached.wait(), timeout=20)
                check(not turn.done(),
                      "A's turn is parked in the foreground")

                pend = broker.pending()
                check(len(pend) == 1
                      and str(pend[0]["request_id"]) == rid,
                      f"exactly B's request is outstanding ({pend})")
                check(str((pend[0].get("details") or {}).get("project"))
                      == "bravo",
                      f"and it names B ({(pend[0].get('details') or {})})")

                # The decision lands on B while A is standing still.
                check(broker.resolve(rid, True) is True,
                      "the decision is accepted")
                approved = await asyncio.wait_for(waiter, timeout=20)
                check(approved is True,
                      f"B's waiter is released with the human's answer "
                      f"({approved})")
                check(broker.settled_as(rid) == "approved",
                      f"and the audit says how ({broker.settled_as(rid)})")

                bar.let_go()
                res = await asyncio.wait_for(turn, timeout=60)

            check(str(res.conversation_id) == str(conv_a),
                  f"A's turn finished as A's ({res.conversation_id})")
            check(broker.pending() == [],
                  f"nothing is left pending ({broker.pending()})")
            targets = rec.targets_of("permission.requested")
            check(targets == ["bravo"],
                  f"one request, aimed at B, throughout ({targets})")
            check((nova.projects_dir / "alpha" / "PROJECT.md").exists(),
                  "and approving B's request touched nothing of A's")
        finally:
            bar.restore()

async def main() -> None:
    await test_foreground_runs_while_background_is_held()
    await test_a_correction_to_a_does_not_reach_b()
    await test_b_failure_while_a_succeeds()
    await test_cancelling_a_leaves_b_running()
    await test_a_permission_names_one_project_while_the_other_runs()
    await test_two_projects_hold_different_generations_at_once()
    await test_foreground_turn_while_bs_task_is_active()
    await test_b_completes_while_as_turn_is_in_flight()
    await test_a_inconclusive_while_b_completes()
    await test_permission_for_b_resolves_while_a_is_foreground()
    check.finish()


if __name__ == "__main__":
    run(main)
