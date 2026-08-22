"""Stages 7, 9, 10, 11 — the deterministic surfaces, without the model.

STAGE 7   API / serialization, over real HTTP
STAGE 9   concurrency and races, with barriers rather than sleeps
STAGE 10  tools and plugins, through the real ToolRouter
STAGE 11  fixed-seed sequence fuzzing over the four subsystems together

The defect this file exists for: a side-effecting tool that TIMED OUT was invoked
a second time. `ToolRouter.execute` defaults to `retries=1`, and both
`agent_supervisor` and `autonomy_supervisor` call arbitrary tools with that
default — so a `project.delete`, `code.write` or `shell.exec` that ran long was
simply run again. A timeout says the call did not FINISH. It says nothing about
whether the side effect landed.

Run:  venv\\Scripts\\python.exe tests\\test_deterministic_stages_p10c3.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.permissions import DEFAULT_MODE, PermissionBroker  # noqa: E402
from core.project_manager import ProjectManager  # noqa: E402
from core.tool_router import ToolCall, ToolRouter  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


# ── STAGE 10 ────────────────────────────────────────────────────────────────
async def test_side_effects_are_never_retried():
    check.section("Stage 10: a side effect is never re-invoked automatically")

    r = ToolRouter({})
    calls = {"n": 0}

    async def side_effecting(_args):
        calls["n"] += 1
        raise RuntimeError("failed AFTER doing the side effect")

    async def slow_side_effecting(_args):
        calls["n"] += 1
        await asyncio.sleep(5)

    r.register("side.effect", side_effecting)
    r.register("slow.effect", slow_side_effecting, timeout_s=0.1)

    calls["n"] = 0
    res = await r.execute(ToolCall(name="side.effect", args={}))   # DEFAULT retries=1
    check(calls["n"] == 1,
          f"a failing side effect runs ONCE under the default retries "
          f"({calls['n']}x)")
    check(res.ok is False and bool(res.error), "and reports the failure")

    calls["n"] = 0
    await r.execute(ToolCall(name="slow.effect", args={}))
    check(calls["n"] == 1,
          f"a TIMED-OUT side effect is not repeated ({calls['n']}x) — a timeout "
          f"does not mean the effect did not land")

    # A tool that declares itself read-only still gets its retry.
    attempts = {"n": 0}

    async def flaky_read(_args):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient network blip")
        return {"ok": True}

    r.register("read.only", flaky_read, retry_safe=True)
    res = await r.execute(ToolCall(name="read.only", args={}))
    check(attempts["n"] == 2 and res.ok,
          f"a DECLARED read-only tool still retries and recovers "
          f"({attempts['n']}x, ok={res.ok})")
    check(r.is_retry_safe("read.only") and not r.is_retry_safe("side.effect"),
          "retry safety is per-tool and defaults to NO")


async def test_tool_contract_edges():
    check.section("Stage 10: malformed calls, missing tools, odd returns")

    r = ToolRouter({})

    async def needs_key(args):
        return {"v": args["required"]}

    async def returns_none(_args):
        return None

    r.register("needs.key", needs_key)
    r.register("returns.none", returns_none)

    res = await r.execute(ToolCall(name="nope.missing", args={}), retries=0)
    check(res.ok is False and "Unknown tool" in (res.error or ""),
          f"an unknown tool is refused clearly ({res.error!r})")

    res = await r.execute(ToolCall(name="needs.key", args={}), retries=0)
    check(res.ok is False and "KeyError" in (res.error or ""),
          f"a missing argument surfaces as a real error ({res.error!r})")

    res = await r.execute(ToolCall(name="returns.none", args={}), retries=0)
    check(res.ok is True and res.result is None,
          "a tool returning None is not mistaken for a failure")

    # Cancellation leaves no partial claim of success.
    started = asyncio.Event()

    async def long_running(_args):
        started.set()
        await asyncio.sleep(30)

    r.register("long.running", long_running, timeout_s=30)
    task = asyncio.create_task(r.execute(ToolCall(name="long.running", args={}),
                                         retries=0))
    await started.wait()
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    check(cancelled, "cancelling a running tool propagates, not silently succeeds")


async def test_tool_chain_dependency():
    check.section("Stage 10: a failed prerequisite blocks the dependent step")

    r = ToolRouter({})
    ran: list[str] = []

    async def step_a(_args):
        ran.append("A")
        return {"value": 41}

    async def step_b(args):
        ran.append("B")
        if args.get("fail"):
            raise RuntimeError("B failed")
        return {"value": int(args["value"]) + 1}

    async def step_c(args):
        ran.append("C")
        return {"final": args["value"]}

    for n, f in (("step.a", step_a), ("step.b", step_b), ("step.c", step_c)):
        r.register(n, f)

    # Happy chain: B consumes A's ACTUAL result, C consumes B's.
    ran.clear()
    a = await r.execute(ToolCall(name="step.a", args={}), retries=0)
    b = await r.execute(ToolCall(name="step.b", args={"value": a.result["value"]}),
                        retries=0)
    c = await r.execute(ToolCall(name="step.c", args={"value": b.result["value"]}),
                        retries=0)
    check(ran == ["A", "B", "C"], f"the chain runs in order ({ran})")
    check(c.result["final"] == 42,
          f"and each step consumed the previous REAL result ({c.result})")

    # B fails -> C must not run. The caller is responsible, and this pins that a
    # failed result is unmistakably a failure to branch on.
    ran.clear()
    a = await r.execute(ToolCall(name="step.a", args={}), retries=0)
    b = await r.execute(ToolCall(name="step.b",
                                 args={"value": a.result["value"], "fail": True}),
                        retries=0)
    if b.ok:
        await r.execute(ToolCall(name="step.c", args={"value": 0}), retries=0)
    check(ran == ["A", "B"], f"C did NOT run after B failed ({ran})")
    check(b.ok is False and bool(b.error),
          "and B's failure is unambiguous enough to branch on")


# ── STAGE 9 ─────────────────────────────────────────────────────────────────
async def test_concurrency_races():
    check.section("Stage 9: races, driven by barriers rather than sleeps")

    # Two simultaneous creates of the same name -> one directory.
    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)
        barrier = asyncio.Barrier(2)

        async def create():
            await barrier.wait()
            return await asyncio.to_thread(pm.scaffold_project, "Race Project")

        results = await asyncio.gather(create(), create(), return_exceptions=True)
        dirs = [p.name for p in projects.iterdir() if p.is_dir()]
        check(dirs == ["race-project"],
              f"two simultaneous creates leave ONE directory ({dirs})")
        check(all(not isinstance(x, Exception) for x in results),
              f"and neither caller sees an error ({results})")

    # Concurrent writes to the SAME attribute: one winner, no corruption.
    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        barrier = asyncio.Barrier(8)

        async def write(i):
            await barrier.wait()
            await m.add_fact(entity="user", attribute="k", value=f"v{i}",
                             confidence=0.9)

        await asyncio.gather(*[write(i) for i in range(8)])
        got = await m.get_latest_fact(entity="user", attribute="k")
        check(got is not None and got.value in {f"v{i}" for i in range(8)},
              f"8 concurrent writes leave exactly one real value ({got.value!r})")

    # Concurrent writes to DISTINCT attributes: nothing is lost.
    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        barrier = asyncio.Barrier(10)

        async def write(i):
            await barrier.wait()
            await m.add_fact(entity="user", attribute=f"a{i}", value=f"v{i}",
                             confidence=0.9)

        await asyncio.gather(*[write(i) for i in range(10)])
        kept = [await m.get_latest_fact(entity="user", attribute=f"a{i}")
                for i in range(10)]
        check(all(k is not None for k in kept),
              f"10 concurrent distinct writes all survive "
              f"({sum(1 for k in kept if k)}/10)")

    # Permission resolution racing the waiter.
    broker = PermissionBroker(mode=DEFAULT_MODE)
    d = await broker.request("project.delete", details={"p": "x"})
    rid = d["request_id"]
    results: list[bool] = []

    async def waiter():
        results.append(await broker.await_decision(rid, timeout_s=2.0))

    async def resolver():
        await asyncio.sleep(0)
        results.append(broker.resolve(rid, True))

    await asyncio.gather(waiter(), resolver())
    check(results.count(True) == 2,
          f"a resolve racing its waiter settles exactly once ({results})")
    check(broker.pending() == [], "and nothing is left pending")


async def test_concurrency_matrix():
    """STAGE 9 — the deterministic half of the concurrency matrix.

    Barriers, not sleeps: every case below is a real simultaneous entry into the
    same critical section. The cases that need the model to interleave two live
    conversations are DEFERRED TO STAGE 13 and named at the end of this file.
    """
    check.section("Stage 9: two workers never claim the same task")

    # THE defect this section exists for. Both queues did SELECT-then-UPDATE
    # across two statements on separate connections, so two claimers simply
    # selected the same row and both ran it. Measured before the fix: 17 claims
    # over 10 queued background tasks, 19 over 10 goal tasks.
    with _tmp() as td:
        m = MemoryUnifier(Path(td), enable_chroma=False)
        await m.initialize()
        for i in range(10):
            await m.enqueue_task(title=f"t{i}", details="d", priority=3,
                                 project_name="temp", initiated_by_user=True)
        barrier = asyncio.Barrier(6)

        async def claimer():
            await barrier.wait()
            got = []
            for _ in range(5):
                t = await m.claim_next_task()
                if t:
                    got.append(str(t["task_id"]))
            return got

        claims = [x for r in await asyncio.gather(*[claimer() for _ in range(6)])
                  for x in r]
        check(len(claims) == len(set(claims)) == 10,
              f"6 claimers over 10 background tasks: {len(claims)} claims, "
              f"{len(set(claims))} distinct")
        check(len(await m.list_tasks(status="running", limit=50)) == 10,
              "and every task is running exactly once")

    with _tmp() as td:
        m = MemoryUnifier(Path(td), enable_chroma=False)
        await m.initialize()
        gid = await m.create_goal(project_name="temp", title="g", objective="o",
                                  success_criteria="c")
        for i in range(10):
            await m.enqueue_goal_task(goal_id=gid, project_name="temp",
                                      tool_name=f"demo.t{i}", args={})
        barrier = asyncio.Barrier(6)

        async def gclaimer():
            await barrier.wait()
            got = []
            for _ in range(5):
                t = await m.claim_next_goal_task()
                if t:
                    got.append(str(t["task_id"]))
            return got

        claims = [x for r in await asyncio.gather(*[gclaimer() for _ in range(6)])
                  for x in r]
        check(len(claims) == len(set(claims)) == 10,
              f"6 claimers over 10 goal tasks: {len(claims)} claims, "
              f"{len(set(claims))} distinct")

    check.section("Stage 9: the same destructive action, twice, at once")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)
        pm.scaffold_project("Duplicate Delete")
        (projects / "duplicate-delete" / "payload.txt").write_text(
            "the only copy", encoding="utf-8")
        barrier = asyncio.Barrier(2)

        async def delete():
            await barrier.wait()
            return await asyncio.to_thread(pm.delete_project, "duplicate-delete")

        results = await asyncio.gather(delete(), delete(), return_exceptions=True)
        entries = sorted(p.name for p in (projects / ".trash").iterdir())
        # NOTE: this is asserted on the OUTCOME, not on an exception. On this
        # filesystem the losing `os.rename` does not raise (verified directly
        # with two threads and nothing but the stdlib), so "one of them fails"
        # would be a claim about Windows, not about Nova.
        check(len(entries) == 1,
              f"two simultaneous deletes leave exactly ONE trash entry ({entries})")
        check(pm.list_projects() == [],
              f"the project is gone from live ({pm.list_projects()})")
        payload = (projects / ".trash" / entries[0] / "payload.txt")
        check(payload.is_file() and payload.read_text(encoding="utf-8") == "the only copy",
              "and the only copy of the data survived intact")
        answers = [r for r in results if not isinstance(r, Exception)]
        check(all(r["moved_to_trash"] == entries[0] for r in answers),
              f"every caller that succeeded names the entry that really exists "
              f"({[r['moved_to_trash'][-6:] for r in answers]})")
        check(all(isinstance(r, FileNotFoundError) for r in results
                  if isinstance(r, Exception)),
              f"and any caller that lost is told the project was already gone "
              f"({[type(r).__name__ for r in results if isinstance(r, Exception)]})")
        restored = pm.restore_project(entries[0])
        check(restored["restored"] == "duplicate-delete",
              f"the surviving entry restores cleanly ({restored['restored']})")

    check.section("Stage 9: foreground and worker on the same durable state")

    with _tmp() as td:
        m = MemoryUnifier(Path(td), enable_chroma=False)
        await m.initialize()
        for i in range(6):
            await m.enqueue_task(title=f"w{i}", details="d", priority=3,
                                 project_name="temp", initiated_by_user=True)
        barrier = asyncio.Barrier(2)

        async def foreground():
            """A conversation writing facts while background work runs."""
            await barrier.wait()
            for i in range(30):
                await m.add_fact(entity="user", attribute=f"live{i}",
                                 value=f"v{i}", confidence=0.9)

        async def worker():
            """A supervisor draining its queue at the same time."""
            await barrier.wait()
            done = 0
            while True:
                t = await m.claim_next_task()
                if not t:
                    break
                await m.add_fact(entity="worker", attribute=f"note{done}",
                                 value="w", confidence=0.5)
                await m.mark_task_done(task_id=str(t["task_id"]),
                                       result={"status": "ok"})
                done += 1
            return done

        _, drained = await asyncio.gather(foreground(), worker())
        kept = [await m.get_latest_fact(entity="user", attribute=f"live{i}")
                for i in range(30)]
        check(all(k is not None for k in kept),
              f"none of the foreground writes was lost "
              f"({sum(1 for k in kept if k)}/30)")
        check(drained == 6, f"and the worker drained its whole queue ({drained}/6)")
        check(not await m.list_tasks(status="running", limit=20),
              "with nothing left claimed")
        check(len(await m.list_tasks(status="done", limit=20)) == 6,
              "and every task recorded done exactly once")

    check.section("Stage 9: cancellation in the middle of a state change")

    with _tmp() as td:
        m = MemoryUnifier(Path(td), enable_chroma=False)
        await m.initialize()
        await m.enqueue_task(title="long one", details="d", priority=1,
                             project_name="temp", initiated_by_user=True)
        started = asyncio.Event()

        async def holder():
            """Claims a task, then is cancelled before it can finish."""
            t = await m.claim_next_task()
            started.set()
            await asyncio.sleep(30)
            return t

        task = asyncio.create_task(holder())
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        running = await m.list_tasks(status="running", limit=20)
        check(len(running) == 1,
              f"a cancelled holder leaves the task claimed, honestly ({len(running)})")
        # It must not be stranded forever: boot recovery is what releases it.
        recovered = await m.cancel_pending_background_work()
        check(recovered["autonomy_tasks"] == 1,
              f"and boot recovery releases it ({recovered})")
        check(not await m.list_tasks(status="running", limit=20),
              "so nothing is stranded across a restart")

        # The store itself is undamaged by the cancellation.
        await m.add_fact(entity="user", attribute="after_cancel", value="ok",
                         confidence=0.9)
        got = await m.get_latest_fact(entity="user", attribute="after_cancel")
        check(got is not None and got.value == "ok",
              "and memory still writes after a cancellation mid-flight")

    check.section("Stage 9: switching project while work is in flight")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)
        m = MemoryUnifier(root / "memory", enable_chroma=False)
        await m.initialize()
        for n in ("Alpha", "Beta"):
            pm.scaffold_project(n)
        barrier = asyncio.Barrier(2)

        async def writer():
            """Work that believes it is still on 'alpha'."""
            await barrier.wait()
            for i in range(10):
                await m.add_fact(entity="project:alpha", attribute=f"n{i}",
                                 value=str(i), confidence=0.9)
                await asyncio.sleep(0)

        async def switcher():
            """The user deletes alpha and moves to beta mid-flight."""
            await barrier.wait()
            await asyncio.to_thread(pm.delete_project, "alpha")

        await asyncio.gather(writer(), switcher())

        check(pm.list_projects() == ["beta"],
              f"the switch took effect ({pm.list_projects()})")
        check(not (projects / "alpha").exists(),
              "and the deleted project's directory is gone")
        # The stale pointer must not RESURRECT it: resolving a name is a lookup,
        # never a create.
        stale = pm.project_path("alpha")
        check(not stale.exists(),
              f"a stale 'alpha' reference does not recreate the directory "
              f"({stale.name})")
        check(pm.list_projects() == ["beta"],
              "and listing is unchanged by that lookup")
        # Facts written under the old project are still readable and are not
        # attributed to the new one.
        kept = await m.get_latest_fact(entity="project:alpha", attribute="n0")
        leaked = await m.get_latest_fact(entity="project:beta", attribute="n0")
        check(kept is not None, "work recorded against the old project survives")
        check(leaked is None,
              f"and none of it leaked into the new project ({leaked})")


# ── STAGE 11 ────────────────────────────────────────────────────────────────
async def test_seeded_sequence_fuzzing():
    """STAGE 11 — fixed seeds, mixed actions, invariants after EVERY step.

    Nothing here is random at run time: each seed replays exactly the same
    sequence on every machine, so a failure is a bug report with a reproduction
    attached. The point is the interleavings nobody writes by hand — seed 7 step
    38 found the same-second trash collision that has its own test below.
    """
    check.section("Stage 11: fixed-seed sequences over the four subsystems")

    #: How often each action actually DID something across every seed. A fuzz
    #: that reports "clean" without ever reaching its interesting branches
    #: proves nothing, so the run asserts its own coverage at the end.
    performed: dict[str, int] = {}

    SEEDS = [1, 7, 13, 42, 99, 2026, 31337]
    ACTIONS = ["create", "delete", "restore", "purge", "remember", "recall",
               "approve", "deny", "timeout", "tool_ok", "tool_fail",
               "tool_refuse", "tool_timeout", "chain", "list", "switch",
               "enqueue", "work", "work_fail",
               "goal_create", "goal_cancel", "goal_resume",
               "goal_resume_duplicate", "goal_enqueue", "goal_claim"]

    for seed in SEEDS:
        rng = random.Random(seed)
        with _tmp() as td:
            root = Path(td)
            projects = root / "projects"
            projects.mkdir(parents=True)
            pm = ProjectManager(repo_root=root, projects_dir=projects)
            m = MemoryUnifier(root / "memory", enable_chroma=False)
            await m.initialize()
            broker = PermissionBroker(mode=DEFAULT_MODE)
            r = ToolRouter({})
            side_effects = {"n": 0}
            ran: list[str] = []

            async def effect(_args):
                side_effects["n"] += 1
                return {"ok": True}

            async def failing(_args):
                side_effects["n"] += 1
                raise RuntimeError("nope")

            async def refusing(_args):
                side_effects["n"] += 1
                return {"ok": False, "error": "refused"}

            async def hanging(_args):
                side_effects["n"] += 1
                await asyncio.sleep(5)

            async def step_a(_args):
                ran.append("A")
                return {"ok": True, "value": 1}

            async def step_b(args):
                ran.append("B")
                if args.get("fail"):
                    return {"ok": False, "error": "B refused"}
                return {"ok": True, "value": int(args.get("value", 0)) + 1}

            async def step_c(_args):
                ran.append("C")
                return {"ok": True}

            r.register("t.ok", effect)
            r.register("t.fail", failing)
            r.register("t.refuse", refusing)
            r.register("t.timeout", hanging, timeout_s=0.05)
            r.register("t.a", step_a)
            r.register("t.b", step_b)
            r.register("t.c", step_c)

            trash_entries: list[str] = []
            errors: list[str] = []
            goal_ids: list[str] = []
            active: str | None = None
            #: what the fuzz BELIEVES about each task it drove, to compare
            #: against what memory actually recorded.
            expected_failed: set[str] = set()

            for step in range(80):
                action = rng.choice(ACTIONS)
                try:
                    if action == "create":
                        name = f"P{rng.randint(0, 4)}"
                        pm.scaffold_project(name)
                        active = pm._sanitize(name)
                    elif action == "delete":
                        live = pm.list_projects()
                        if live:
                            victim = rng.choice(live)
                            res = pm.delete_project(victim)
                            trash_entries.append(res["moved_to_trash"])
                            performed["delete"] = performed.get("delete", 0) + 1
                            if active == victim:
                                active = None      # the pointer is dropped
                    elif action == "restore":
                        if trash_entries:
                            e = trash_entries.pop(rng.randrange(len(trash_entries)))
                            try:
                                pm.restore_project(e)
                                performed["restore"] = performed.get("restore", 0) + 1
                            except FileExistsError:
                                trash_entries.append(e)   # refusing to clobber
                                performed["restore_refused"] = performed.get("restore_refused", 0) + 1
                    elif action == "purge":
                        if trash_entries and rng.random() < 0.5:
                            e = trash_entries.pop(rng.randrange(len(trash_entries)))
                            pm.purge_trash(e)
                            performed["purge"] = performed.get("purge", 0) + 1
                    elif action == "remember":
                        p = f"P{rng.randint(0, 4)}"
                        await m.add_fact(entity=f"project:{p}", attribute="note",
                                         value=f"{p}-s{seed}-{step}", confidence=0.9)
                    elif action == "recall":
                        await m.search(q=f"P{rng.randint(0, 4)}",
                                       conversation_id=None, limit=5)
                    elif action in ("approve", "deny"):
                        d = await broker.request("project.delete", details={})
                        broker.resolve(d["request_id"], action == "approve")
                    elif action == "timeout":
                        d = await broker.request("project.delete", details={})
                        await broker.await_decision(d["request_id"], timeout_s=0.02)
                    elif action == "tool_ok":
                        res = await r.execute(ToolCall(name="t.ok", args={}))
                        if not res.ok:
                            errors.append(f"step {step}: a working tool reported failure")
                    elif action in ("tool_fail", "tool_refuse", "tool_timeout"):
                        name = {"tool_fail": "t.fail", "tool_refuse": "t.refuse",
                                "tool_timeout": "t.timeout"}[action]
                        before = side_effects["n"]
                        res = await r.execute(ToolCall(name=name, args={}))
                        performed[action] = performed.get(action, 0) + 1
                        if side_effects["n"] - before > 1:
                            errors.append(f"step {step}: {name} ran twice")
                        if res.ok:
                            errors.append(f"step {step}: {name} failed but reported ok")
                        if not (res.error or ""):
                            errors.append(f"step {step}: {name} failed with no reason")
                    elif action == "chain":
                        ran.clear()
                        fail = rng.random() < 0.5
                        a = await r.execute(ToolCall(name="t.a", args={}), retries=0)
                        b = await r.execute(
                            ToolCall(name="t.b",
                                     args={"value": a.result["value"], "fail": fail}),
                            retries=0)
                        if b.ok:
                            await r.execute(ToolCall(name="t.c", args={}), retries=0)
                        performed["chain_fail" if fail else "chain_ok"] = performed.get(
                            "chain_fail" if fail else "chain_ok", 0) + 1
                        if fail and "C" in ran:
                            errors.append(f"step {step}: C ran after B failed")
                        if not fail and ran != ["A", "B", "C"]:
                            errors.append(f"step {step}: clean chain ran {ran}")
                    elif action == "list":
                        pm.list_projects()
                        pm.list_trash()
                    elif action == "switch":
                        live = pm.list_projects()
                        active = rng.choice(live) if live else None
                    elif action == "enqueue":
                        await m.enqueue_task(title=f"task-{step}", details="d",
                                             priority=3, project_name="temp",
                                             initiated_by_user=True)
                    elif action == "goal_create":
                        proj = f"P{rng.randint(0, 4)}"
                        g = await m.create_goal(project_name=proj,
                                                title=f"g{step}", objective="o",
                                                success_criteria="c")
                        goal_ids.append(str(g))
                        await m.enqueue_goal_task(goal_id=g, project_name=proj,
                                                  tool_name="__decide__", args={})
                    elif action == "goal_cancel":
                        if goal_ids:
                            await m.cancel_goal(
                                goal_id=UUID(rng.choice(goal_ids)))
                            performed["goal_cancel"] = performed.get("goal_cancel", 0) + 1
                    elif action in ("goal_resume", "goal_resume_duplicate"):
                        if goal_ids:
                            g = UUID(rng.choice(goal_ids))
                            await m.resume_goal(goal_id=g)
                            performed["goal_resume"] = performed.get("goal_resume", 0) + 1
                            if action == "goal_resume_duplicate":
                                await m.resume_goal(goal_id=g)
                                await m.resume_goal(goal_id=g)
                    elif action == "goal_enqueue":
                        if goal_ids:
                            g = UUID(rng.choice(goal_ids))
                            row = await m.get_goal(goal_id=g)
                            if row:
                                # Deliberately enqueue with the goal's OWN
                                # project; nothing in Nova may write another.
                                await m.enqueue_goal_task(
                                    goal_id=g, project_name=row["project_name"],
                                    tool_name="t.ok", args={})
                    elif action == "goal_claim":
                        claimed_task = await m.claim_next_goal_task()
                        if claimed_task:
                            performed["goal_claim"] = performed.get("goal_claim", 0) + 1
                            row = await m.get_goal(
                                goal_id=UUID(str(claimed_task["goal_id"])))
                            if row and row["status"] != "active":
                                errors.append(
                                    f"step {step}: claimed work for a "
                                    f"{row['status']} goal")
                            await m.complete_goal_task(
                                task_id=str(claimed_task["task_id"]),
                                status="done", result={})
                    elif action in ("work", "work_fail"):
                        t = await m.claim_next_task()
                        if t:
                            tid = str(t["task_id"])
                            tool = "t.fail" if action == "work_fail" else "t.ok"
                            res = await r.execute(ToolCall(name=tool, args={}))
                            performed["work"] = performed.get("work", 0) + 1
                            if res.ok:
                                await m.mark_task_done(task_id=tid,
                                                       result={"status": "ok"})
                            else:
                                # A failed tool must NEVER leave the task 'done'.
                                await m.mark_task_failed(task_id=tid,
                                                         error=res.error or "failed")
                                expected_failed.add(tid)
                                performed["work_failed"] = performed.get("work_failed", 0) + 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"step {step} {action}: {type(e).__name__}: {e}")

                # ── INVARIANTS, after every single step ─────────────────────
                names = [p.name for p in projects.iterdir()
                         if p.is_dir() and not p.name.startswith(".")]
                if len(names) != len(set(names)):
                    errors.append(f"step {step}: duplicate project names {names}")
                if sorted(names) != pm.list_projects():
                    errors.append(f"step {step}: listing disagrees with disk")
                if broker.pending():
                    errors.append(f"step {step}: {len(broker.pending())} left pending")
                if active is not None:
                    # A stale pointer must never resurrect a directory, and must
                    # never point at something that is not there.
                    if active not in names:
                        errors.append(f"step {step}: active {active!r} is not live")
                    if not pm.project_path(active).exists():
                        errors.append(f"step {step}: active {active!r} has no dir")
                for entry in (projects / ".trash").iterdir() if (projects / ".trash").exists() else []:
                    if not entry.is_dir():
                        errors.append(f"step {step}: junk in trash: {entry.name}")

                # ── GOAL LIFECYCLE INVARIANTS ──────────────────────────────
                for raw_gid in goal_ids:
                    goal = await m.get_goal(goal_id=UUID(raw_gid))
                    if goal is None:
                        errors.append(f"step {step}: goal {raw_gid[:8]} vanished")
                        continue
                    rows = await m.list_goal_tasks(goal_id=raw_gid, limit=100)
                    runnable = [t for t in rows if t["status"] == "queued"]
                    live_decides = [t for t in rows
                                    if t["tool_name"] == "__decide__"
                                    and t["status"] in ("queued", "running")]
                    if goal["status"] == "cancelled" and runnable:
                        errors.append(
                            f"step {step}: cancelled goal {raw_gid[:8]} still has "
                            f"{len(runnable)} runnable task(s)")
                    if len(live_decides) > 1:
                        errors.append(
                            f"step {step}: goal {raw_gid[:8]} accumulated "
                            f"{len(live_decides)} runnable continuations")
                    wrong = [t for t in rows
                             if t["project_name"] != goal["project_name"]]
                    if wrong:
                        errors.append(
                            f"step {step}: {len(wrong)} task(s) of goal "
                            f"{raw_gid[:8]} are not in {goal['project_name']!r}")

            # ── END OF SEED: the expensive checks ──────────────────────────
            # 1. Every trash entry is INDIVIDUALLY restorable — or honestly
            #    refuses because a live project already holds the name.
            for e in [x["entry"] for x in pm.list_trash()]:
                original = next(x["original"] for x in pm.list_trash()
                                if x["entry"] == e)
                if not original:
                    errors.append(f"trash entry {e} lost its original name")
                    continue
                try:
                    back = pm.restore_project(e)
                    if back["restored"] != original:
                        errors.append(f"{e} restored as {back['restored']!r}, "
                                      f"not {original!r}")
                    if not (projects / back["restored"]).is_dir():
                        errors.append(f"{e} restored nothing on disk")
                    pm.delete_project(back["restored"])       # put it back
                except FileExistsError:
                    pass          # a live project holds the name: correct
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{e} could not be restored: "
                                  f"{type(ex).__name__}: {ex}")

            # 2. No task that failed is recorded as done, anywhere.
            done_ids = {str(t["task_id"])
                        for t in await m.list_tasks(status="done", limit=200)}
            wrongly_done = expected_failed & done_ids
            if wrongly_done:
                errors.append(f"{len(wrongly_done)} failed task(s) recorded done")

            # 3. No cross-project leakage: a note written for one project is
            #    never readable as another project's.
            for i in range(5):
                fact = await m.get_latest_fact(entity=f"project:P{i}",
                                               attribute="note")
                if fact is not None and not str(fact.value).startswith(f"P{i}-"):
                    errors.append(f"P{i} note holds {fact.value!r}")

            check(not errors,
                  f"seed {seed}: 80 mixed actions, no invariant broken "
                  f"({errors[:2] if errors else 'clean'})")

    # The fuzz has to have REACHED the states it claims to have cleared.
    required = ["delete", "restore", "purge", "chain_fail", "chain_ok",
                "work", "work_failed", "tool_fail", "tool_refuse", "tool_timeout",
                "goal_cancel", "goal_resume", "goal_claim"]
    missing = [k for k in required if performed.get(k, 0) < 1]
    check(not missing,
          f"the sequences actually reached every interesting state "
          f"(missing: {missing}; counts: "
          f"{ {k: performed.get(k, 0) for k in required} })")


# ── STAGE 7 ─────────────────────────────────────────────────────────────────
async def test_rapid_delete_restore_delete_does_not_collide():
    """Found by seed 7, step 38: two deletes inside one second collided.

    The trash id is timestamped to the SECOND. `shutil.move` onto an existing
    directory does not fail cleanly — it moves the source INSIDE it — so the entry
    became `p4--<ts>/p4` and a later restore would have brought back a nested
    wreck. This is precisely the interleaving nobody writes by hand.
    """
    check.section("Stage 11 finding: same-second delete/restore/delete")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        # The sequence the fuzzer actually hit: delete, RECREATE, delete again —
        # all inside one second, so the first trash entry is still sitting there.
        # (After a *restore* the id is freed and reusing it is correct, which is
        # why the first version of this test asserted the wrong thing.)
        pm.scaffold_project("p4")
        first = pm.delete_project("p4")["moved_to_trash"]
        pm.scaffold_project("p4")
        second = pm.delete_project("p4")["moved_to_trash"]

        check(first != second,
              f"the second delete gets its OWN trash id ({first!r} vs {second!r})")
        entries = sorted(p.name for p in (projects / ".trash").iterdir())
        check(entries == sorted([first, second]),
              f"both entries coexist ({entries})")
        nested = list((projects / ".trash" / first).glob("p4"))
        check(not nested,
              f"and NOTHING was nested inside the first ({[n.name for n in nested]})")

        from core.project_names import safe_trash_entry
        check(safe_trash_entry(second) == second,
              f"the suffixed id survives the trash sanitizer ({second!r})")
        res = pm.restore_project(second)
        check(res["restored"] == "p4" and (projects / "p4").is_dir(),
              f"and it restores cleanly ({res['restored']!r})")

        # Several delete/recreate cycles inside one second: every entry that is
        # still IN the trash must have its own id. (A restored id is legitimately
        # free to reuse, so only co-existing entries are compared.)
        ids = []
        for _ in range(4):
            pm.scaffold_project("p4")
            ids.append(pm.delete_project("p4")["moved_to_trash"])
        live_entries = sorted(x.name for x in (projects / ".trash").iterdir())
        check(len(set(ids)) == len(ids),
              f"four same-second delete/recreate cycles all stay distinct "
              f"({len(set(ids))} of {len(ids)}: {ids})")
        check(sorted(set(ids + [first])) == live_entries,
              f"and every id names a real, separate entry ({live_entries})")


async def test_failed_delete_leaves_no_mislabelled_entry():
    """Found in self-review of THIS PR's own nested-trash fix.

    The holder directory is created by `_trash_target` and the sidecar naming
    the project was written AFTER the move. A move that failed in between —
    disk full, a lock, a permission — would leave a trash entry whose only clue
    to its identity was the TRUNCATED label, and restoring that would have
    brought the project back under a shortened name. Silently renaming a
    project is the one thing the legacy-identity contract forbids.
    """
    check.section("Stage 11: a failed delete leaves nothing mislabelled")

    import shutil as _real_shutil

    import core.project_manager as PMOD
    from core.project_names import MAX_COMPONENT_LEN

    class _MoveFails:
        """The real shutil, with `move` refusing — nothing else changed."""

        def __getattr__(self, name):
            return getattr(_real_shutil, name)

        @staticmethod
        def move(*_a, **_k):
            raise OSError("simulated: the move failed")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        long_name = "L" * (MAX_COMPONENT_LEN - 4)          # forces the NESTED form
        for name in ("short-one", long_name):
            (projects / name).mkdir()
            (projects / name / "PROJECT.md").write_text("# keep me\n", encoding="utf-8")
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        original_shutil = PMOD.shutil
        PMOD.shutil = _MoveFails()
        try:
            for label, name in (("flat", "short-one"), ("nested", long_name)):
                try:
                    pm.delete_project(name)
                    raised = ""
                except OSError as e:
                    raised = str(e)
                check("simulated" in raised,
                      f"{label}: the failure is reported, not swallowed ({raised[:40]})")
                check((projects / name / "PROJECT.md").is_file(),
                      f"{label}: the project is still there, untouched")
                check(pm.list_trash() == [],
                      f"{label}: and NOTHING was left in the trash "
                      f"({[e['entry'][:18] for e in pm.list_trash()]})")
        finally:
            PMOD.shutil = original_shutil

        # With the real shutil back, the same delete works and keeps the exact
        # identity — proving the guard above did not break the ordinary path.
        entry = pm.delete_project(long_name)["moved_to_trash"]
        listed = pm.list_trash()
        check(len(listed) == 1 and listed[0]["original"] == long_name,
              f"the real delete still records the exact identity "
              f"({len(listed)} entries)")
        check(pm.restore_project(entry)["restored"] == long_name,
              "and it restores under that same exact name")


async def test_supervisor_never_retries_a_refusal():
    """BUG 10, in the stage suite: a refusal is final, an exception is not.

    The router stopped retrying explicit refusals; AgentSupervisor kept its own
    loop and saw only `ok is False` + `is_retry_safe(name)`. Measured with a
    read-only tool answering `missing_query`: 116 invocations for one goal.
    """
    check.section("Stage 10: a refusal is final at the supervisor too")

    from harness import ScriptedLLM

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig

    async def run(tool_impl, retry_safe: bool) -> tuple[int, list[dict]]:
        with _tmp() as td:
            mem = MemoryUnifier(Path(td), enable_chroma=False)
            await mem.initialize()
            llm = ScriptedLLM()
            step = {"n": 0}

            def decide(_p: str) -> str:
                step["n"] += 1
                return ('{"type":"tool","name":"demo.t","args":{}}' if step["n"] == 1
                        else '{"type":"final","message":"done"}')

            llm.when(lambda _p: True, decide)
            router = ToolRouter({"demo.t": tool_impl}, {"demo.t": "a tool"})
            router.set_retry_safe("demo.t", retry_safe)
            sup = AgentSupervisor(memory=mem, llm=llm, router=router,
                                  tool_descriptions={"demo.t": "a tool"},
                                  cfg=SupervisorConfig(tick_seconds=0.05,
                                                       max_retries=3,
                                                       max_steps_per_goal=6))
            gid = await mem.create_goal(project_name="temp", title="t",
                                        objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=gid, project_name="temp",
                                        tool_name="__decide__", args={})
            sup.start()
            await asyncio.sleep(7.0)
            await sup.stop()
            rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
            return len([t for t in rows if t["tool_name"] == "demo.t"]), rows

    calls = {"n": 0}

    async def refuses(_args):
        calls["n"] += 1
        return {"ok": False, "error": "missing_query"}

    n_tasks, rows = await run(refuses, retry_safe=True)
    check(calls["n"] == 1,
          f"a retry-safe tool's refusal is executed ONCE ({calls['n']}x)")
    check(n_tasks == 1,
          f"and produces exactly one task, never a requeue ({n_tasks})")
    bad = [t for t in rows if t["tool_name"] == "demo.t"]
    check(all(int(t.get("attempts") or 0) == 0 for t in bad),
          f"with no attempt consumed ({[t.get('attempts') for t in bad]})")
    check(all(t["status"] == "failed" for t in bad),
          f"recorded as failed, not done ({[t['status'] for t in bad]})")

    calls["n"] = 0

    async def blips(_args):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return {"ok": True}

    n_tasks, rows = await run(blips, retry_safe=True)
    check(calls["n"] >= 2,
          f"but a transient EXCEPTION still retries ({calls['n']}x)")
    ok = [t for t in rows if t["tool_name"] == "demo.t" and t["status"] == "done"]
    check(bool(ok), f"and recovers ({[t['status'] for t in rows if t['tool_name'] == 'demo.t']})")

    calls["n"] = 0

    async def unsafe_boom(_args):
        calls["n"] += 1
        raise RuntimeError("side effect may have landed")

    n_tasks, _ = await run(unsafe_boom, retry_safe=False)
    check(calls["n"] == 1,
          f"an UNSAFE tool's exception is still never retried ({calls['n']}x)")


# ══ ROUND 3: THE GOAL LIFECYCLE ═════════════════════════════════════════════
async def _goal_app():
    """The real autonomy router over a real MemoryUnifier, no HTTP server."""
    import httpx
    from fastapi import FastAPI

    from backend.state import STATE
    from backend.routers import autonomy

    app = FastAPI()
    app.include_router(autonomy.router)
    return app, httpx.ASGITransport(app=app), STATE


async def test_cancelled_goal_starts_no_new_work():
    """BUG 11A: a CANCELLED goal kept executing.

    Cancel changed `goals.status` and nothing else. Its queued tasks stayed
    `queued`, and the claim looked only at the task row — so the supervisor
    went on running a goal the user had cancelled. Measured: 3 tool calls after
    cancellation, and the supervisor then overwrote the goal's own status with
    `paused`, so even the cancellation did not survive.
    """
    check.section("Stage 9: a cancelled goal starts no new work")

    from harness import ScriptedLLM

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        llm = ScriptedLLM()
        llm.default_reply = '{"type":"tool","name":"demo.act","args":{}}'
        acts = {"n": 0}

        async def act(_args):
            acts["n"] += 1
            return {"ok": True}

        router = ToolRouter({"demo.act": act}, {"demo.act": "acts"})
        sup = AgentSupervisor(memory=mem, llm=llm, router=router,
                              tool_descriptions={"demo.act": "acts"},
                              cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                                   max_steps_per_goal=6))
        gid = await mem.create_goal(project_name="alpha", title="t", objective="o",
                                    success_criteria="c")
        # Several queued steps, cancelled before the supervisor ever runs.
        for name in ("__decide__", "demo.act", "demo.act"):
            await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                        tool_name=name, args={})
        out = await mem.cancel_goal(goal_id=gid)
        check(out is not None and out["cancelled_tasks"] == 3,
              f"cancel stops every queued task ({out and out['cancelled_tasks']})")
        check(out["already_running"] == 0,
              f"and reports nothing was mid-flight ({out['already_running']})")

        sup.start()
        await asyncio.sleep(3.0)
        await sup.stop()

        check(acts["n"] == 0, f"NOTHING executed after cancellation ({acts['n']})")
        check(len(llm.prompts) == 0,
              f"the planner was never even consulted ({len(llm.prompts)} calls)")
        goals = await mem.list_goals(project_name="alpha")
        g = next(x for x in goals if x["goal_id"] == str(gid))
        check(g["status"] == "cancelled",
              f"the goal is still cancelled afterwards ({g['status']})")
        tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        check(len(tasks) == 3 and all(t["status"] == "cancelled" for t in tasks),
              f"no new task was spawned and none ran "
              f"({[(t['tool_name'], t['status']) for t in tasks]})")
        check(await mem.claim_next_goal_task() is None,
              "and nothing is claimable")

        # Cancelling twice is honest about having nothing left to stop.
        again = await mem.cancel_goal(goal_id=gid)
        check(again is not None and again["cancelled_tasks"] == 0,
              f"a second cancel stops 0 more ({again and again['cancelled_tasks']})")
        check(await mem.cancel_goal(goal_id=uuid4()) is None,
              "and cancelling a goal that does not exist says so")


async def test_cancel_races_the_claim():
    """Either the claim wins ONE task, or the cancel does — never more."""
    check.section("Stage 9: cancel racing the claim")

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t", objective="o",
                                    success_criteria="c")
        for _ in range(4):
            await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                        tool_name="demo.act", args={})
        barrier = asyncio.Barrier(2)
        claimed: list[dict | None] = []

        async def claimer():
            await barrier.wait()
            claimed.append(await mem.claim_next_goal_task())

        async def canceller():
            await barrier.wait()
            await mem.cancel_goal(goal_id=gid)

        await asyncio.gather(claimer(), canceller())

        got = [c for c in claimed if c]
        check(len(got) <= 1,
              f"at most ONE task was claimed in the race ({len(got)})")
        # Whoever won, nothing further may start.
        check(await mem.claim_next_goal_task() is None,
              "and no further task is claimable after the cancel")
        tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        started = [t for t in tasks if t["status"] == "running"]
        check(len(started) == len(got),
              f"the record matches what actually started "
              f"({len(started)} running, {len(got)} claimed)")
        # An already-running task is NOT pretended to be undone.
        goals = await mem.list_goals(project_name="alpha")
        g = next(x for x in goals if x["goal_id"] == str(gid))
        check(g["status"] == "cancelled", f"the goal is cancelled ({g['status']})")


async def test_resume_is_idempotent_and_keeps_its_project():
    """BUG 11B + 11C, over real HTTP."""
    check.section("Stage 7: resume is idempotent and project-authoritative")

    import httpx

    app, transport, STATE = await _goal_app()
    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        saved = STATE.memory
        STATE.memory = mem
        try:
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                async def runnable(gid):
                    rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
                    return [t for t in rows
                            if t["tool_name"] == "__decide__"
                            and t["status"] in ("queued", "running")]

                # ── sequential duplicates ───────────────────────────────────
                gid = await mem.create_goal(project_name="alpha", title="t",
                                            objective="o", success_criteria="c")
                await mem.update_goal_status(goal_id=gid, status="paused")
                codes = []
                for _ in range(3):
                    r = await client.post(f"/goals/{gid}/resume")
                    codes.append(r.status_code)
                check(codes == [200, 200, 200], f"three resumes all answer 200 ({codes})")
                live = await runnable(gid)
                check(len(live) == 1,
                      f"but leave exactly ONE runnable continuation ({len(live)})")
                check(live[0]["project_name"] == "alpha",
                      f"in the goal's OWN project ({live[0]['project_name']})")

                # ── concurrent duplicates ──────────────────────────────────
                gid2 = await mem.create_goal(project_name="alpha", title="t2",
                                             objective="o", success_criteria="c")
                await mem.update_goal_status(goal_id=gid2, status="paused")
                results = await asyncio.gather(
                    *[client.post(f"/goals/{gid2}/resume") for _ in range(8)])
                check(all(r.status_code == 200 for r in results),
                      f"8 concurrent resumes all answer 200 "
                      f"({sorted({r.status_code for r in results})})")
                created = [r for r in results
                           if r.json().get("continuation_enqueued") is True]
                check(len(created) == 1,
                      f"exactly one of them reports that it created the "
                      f"continuation ({len(created)})")
                live2 = await runnable(gid2)
                check(len(live2) == 1,
                      f"and exactly one runnable __decide__ exists ({len(live2)})")
                rows2 = await mem.list_goal_tasks(goal_id=str(gid2), limit=50)
                ids = [t["task_id"] for t in rows2]
                check(len(ids) == len(set(ids)) == 1,
                      f"with no duplicate task identities ({len(ids)} rows)")

                # ── project authority ──────────────────────────────────────
                gid3 = await mem.create_goal(project_name="alpha", title="t3",
                                             objective="o", success_criteria="c")
                await mem.update_goal_status(goal_id=gid3, status="paused")
                r = await client.post(f"/goals/{gid3}/resume",
                                      params={"project": "beta"})
                check(r.status_code == 409,
                      f"resuming into a DIFFERENT project is refused ({r.status_code})")
                rows3 = await mem.list_goal_tasks(goal_id=str(gid3), limit=50)
                check(not rows3, f"and enqueues nothing at all ({len(rows3)} tasks)")
                r = await client.post(f"/goals/{gid3}/resume",
                                      params={"project": "alpha"})
                check(r.status_code == 200,
                      f"naming the goal's real project is fine ({r.status_code})")
                rows3 = await mem.list_goal_tasks(goal_id=str(gid3), limit=50)
                check({t["project_name"] for t in rows3} == {"alpha"},
                      f"and the work lands in alpha "
                      f"({sorted({t['project_name'] for t in rows3})})")

                # ── cancel over the wire, with queued work ─────────────────
                gid4 = await mem.create_goal(project_name="alpha", title="t4",
                                             objective="o", success_criteria="c")
                for name in ("__decide__", "demo.act"):
                    await mem.enqueue_goal_task(goal_id=gid4, project_name="alpha",
                                                tool_name=name, args={})
                r = await client.post(f"/goals/{gid4}/cancel")
                body = r.json()
                check(r.status_code == 200 and body["cancelled_tasks"] == 2,
                      f"cancel reports the work it stopped ({body})")
                check(body["already_running"] == 0,
                      f"and that nothing was mid-flight ({body['already_running']})")
                rows4 = await mem.list_goal_tasks(goal_id=str(gid4), limit=50)
                check(all(t["status"] == "cancelled" for t in rows4),
                      f"the state matches the response "
                      f"({[t['status'] for t in rows4]})")
                r = await client.post(f"/goals/{gid4}/cancel")
                check(r.status_code == 200 and r.json()["cancelled_tasks"] == 0,
                      f"a repeat cancel stops nothing more ({r.json()})")

                # ── resuming a cancelled goal is allowed and singular ──────
                r = await client.post(f"/goals/{gid4}/resume")
                check(r.status_code == 200 and r.json()["continuation_enqueued"] is True,
                      f"a cancelled goal can be resumed deliberately ({r.json()})")
                check(len(await runnable(gid4)) == 1,
                      "with exactly one continuation")

                # ── the absent and the malformed ───────────────────────────
                missing = "00000000-0000-4000-8000-000000000000"
                for path in (f"/goals/{missing}/resume", f"/goals/{missing}/cancel"):
                    r = await client.post(path)
                    check(r.status_code == 404, f"{path[:28]}… -> 404 ({r.status_code})")
                for path in ("/goals/not-a-uuid/resume", "/goals/not-a-uuid/cancel"):
                    r = await client.post(path)
                    check(r.status_code == 422, f"{path[:28]}… -> 422 ({r.status_code})")
                orphans = await mem.list_goal_tasks(goal_id=missing, limit=10)
                check(not orphans,
                      f"and none of that enqueued anything ({len(orphans)})")
        finally:
            STATE.memory = saved


async def test_supervisor_honours_the_wire_lifecycle():
    """The state a 200 reports is the state the supervisor then acts on."""
    check.section("Stage 9: the supervisor obeys cancel/resume from the wire")

    import httpx
    from harness import ScriptedLLM

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig

    app, transport, STATE = await _goal_app()
    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        saved = STATE.memory
        STATE.memory = mem
        try:
            llm = ScriptedLLM()
            llm.default_reply = '{"type":"tool","name":"demo.act","args":{}}'
            acts = {"n": 0}

            async def act(_args):
                acts["n"] += 1
                return {"ok": True}

            router = ToolRouter({"demo.act": act}, {"demo.act": "acts"})
            sup = AgentSupervisor(memory=mem, llm=llm, router=router,
                                  tool_descriptions={"demo.act": "acts"},
                                  cfg=SupervisorConfig(tick_seconds=0.05,
                                                       max_retries=1,
                                                       max_steps_per_goal=6))
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                r = await client.post("/goals", json={"objective": "do the thing",
                                                      "project": "alpha"})
                gid = r.json()["goal_id"]
                await client.post(f"/goals/{gid}/cancel")
                sup.start()
                await asyncio.sleep(2.5)
                await sup.stop()
                check(acts["n"] == 0,
                      f"a goal cancelled over HTTP executes nothing ({acts['n']})")

                # Resumed over HTTP, it runs again — the guard is a gate, not a wall.
                await client.post(f"/goals/{gid}/resume")
                sup.start()
                await asyncio.sleep(2.5)
                await sup.stop()
                check(acts["n"] >= 1,
                      f"and resuming it lets the work proceed ({acts['n']})")
                rows = await mem.list_goal_tasks(goal_id=gid, limit=50)
                check({t["project_name"] for t in rows} == {"alpha"},
                      f"all of it under the goal's own project "
                      f"({sorted({t['project_name'] for t in rows})})")
        finally:
            STATE.memory = saved


async def test_cancelled_goal_accepts_no_new_work():
    """Found by the widened fuzz (seeds 13, 99, 31337) AFTER the cancel fix.

    Cancelling stopped the queued rows and the claim refused a non-active
    goal — but nothing stopped new rows being ADDED. In production the window
    is real: the supervisor claims a step, the user cancels while it runs, and
    the supervisor then enqueues the next one. Nothing executed, because the
    claim still refused it, so the only visible symptom was a cancelled goal
    quietly accumulating queued work — until someone resumed the goal, at which
    point a step planned BEFORE the cancellation would run.
    """
    check.section("Stage 11 finding: a cancelled goal accepts no new work")

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t", objective="o",
                                    success_criteria="c")
        first = await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                            tool_name="__decide__", args={})
        check(first is not None, "an ACTIVE goal accepts work")

        await mem.cancel_goal(goal_id=gid)
        refused = await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                              tool_name="demo.act", args={})
        check(refused is None,
              f"a CANCELLED goal refuses new work, and says so ({refused})")
        rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        check(len(rows) == 1,
              f"nothing was written ({len(rows)} row(s))")
        check(not [t for t in rows if t["status"] == "queued"],
              f"and nothing is runnable ({[t['status'] for t in rows]})")

        # The decisive part: RESUMING must not resurrect pre-cancellation work.
        out = await mem.resume_goal(goal_id=gid)
        check(out is not None and out["continuation_enqueued"] is True,
              f"resume starts a FRESH continuation ({out})")
        rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        runnable = [t for t in rows if t["status"] == "queued"]
        check(len(runnable) == 1 and runnable[0]["tool_name"] == "__decide__",
              f"and the only runnable step is that fresh decision "
              f"({[(t['tool_name'], t['status']) for t in runnable]})")
        check(await mem.claim_next_goal_task() is not None,
              "which is claimable again, because the goal is active")

        # A COMPLETED goal is equally closed.
        gid2 = await mem.create_goal(project_name="alpha", title="t2",
                                     objective="o", success_criteria="c")
        await mem.update_goal_status(goal_id=gid2, status="completed")
        check(await mem.enqueue_goal_task(goal_id=gid2, project_name="alpha",
                                          tool_name="demo.act", args={}) is None,
              "a COMPLETED goal refuses new work too")

        # A PAUSED goal still accepts it — pause is a hold, not an ending, and
        # resume is meant to continue the plan.
        gid3 = await mem.create_goal(project_name="alpha", title="t3",
                                     objective="o", success_criteria="c")
        await mem.update_goal_status(goal_id=gid3, status="paused")
        check(await mem.enqueue_goal_task(goal_id=gid3, project_name="alpha",
                                          tool_name="demo.act", args={}) is not None,
              "a PAUSED goal still accepts work (pause is a hold, not an end)")


async def test_the_claim_itself_refuses_a_non_active_goal():
    """The claim is the SECOND half of the cancel invariant, and stands alone.

    `cancel_goal` marks the queued rows, so a test that always cancels through
    it can pass with the claim guard removed — which is exactly what happened:
    the mutant that deletes `AND g.status='active'` survived the first round of
    this suite. It matters independently because the supervisor pauses a goal
    ITSELF (step budget, or a question for Marcus) without touching its task
    rows, and a task queued before that pause must not be picked up.
    """
    check.section("Stage 9: the claim refuses a non-active goal on its own")

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t", objective="o",
                                    success_criteria="c")
        await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                    tool_name="demo.act", args={})

        # Status changed WITHOUT cancel_goal: the rows stay `queued`.
        for status in ("paused", "cancelled", "completed"):
            await mem.update_goal_status(goal_id=gid, status=status)
            rows = await mem.list_goal_tasks(goal_id=str(gid), limit=10)
            check(any(t["status"] == "queued" for t in rows),
                  f"{status}: the task row is still queued "
                  f"({[t['status'] for t in rows]})")
            check(await mem.claim_next_goal_task() is None,
                  f"{status}: and the claim refuses it anyway")

        await mem.update_goal_status(goal_id=gid, status="active")
        got = await mem.claim_next_goal_task()
        check(got is not None and str(got["tool_name"]) == "demo.act",
              f"active again -> the same task is claimable ({got and got['tool_name']})")

        # A task with no goal row at all FAILS CLOSED (V3 P10 C8). This used to
        # assert the opposite, preserved as compatibility — but the supervisor
        # hands a claimed real tool straight to the router without looking its
        # parent up, so an orphan row naming a side-effecting tool executed on
        # nothing's authority. Storage refuses to create one and refuses to
        # hand one out.
        orphan = uuid4()
        made = await mem.enqueue_goal_task(goal_id=orphan, project_name="alpha",
                                           tool_name="demo.orphan", args={})
        check(made is None,
              f"work cannot be queued for a goal that does not exist ({made})")

        # Seed one DIRECTLY as well — rows left by older builds already exist,
        # and asserting the claim only after a refused enqueue would pass
        # vacuously (there would be no row to claim either way).
        import aiosqlite
        async with aiosqlite.connect(mem._sqlite._db_path) as db:
            await db.execute(
                "INSERT INTO tasks(task_id, goal_id, project_name, tool_name, "
                "args_json, status, attempts, run_after, last_error, result_json, "
                "created_at, updated_at, generation) VALUES(?,?,?,?,'{}','queued',"
                "0,?,'','{}',?,?,0)",
                (str(uuid4()), str(orphan), "alpha", "demo.orphan",
                 "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00",
                 "2000-01-01T00:00:00+00:00"))
            await db.commit()
        got = await mem.claim_next_goal_task()
        check(got is None,
              f"and no orphan task is claimable ({got and got['tool_name']})")


async def test_supervisor_does_not_report_a_step_it_could_not_schedule():
    """The mid-flight window: cancelled WHILE the decision was being made.

    The supervisor decides, then enqueues the chosen tool and the next
    decision. If the goal was cancelled in between, the enqueue is refused —
    and writing "Next: <tool>" to the goal's progress at that point would
    report a plan that does not exist. Driven through the REAL supervisor by
    cancelling from inside the planner call, which is precisely the window.
    """
    check.section("Stage 10: no report of a step that was never scheduled")

    import sqlite3

    from harness import ScriptedLLM

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t", objective="o",
                                    success_criteria="c")
        await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                    tool_name="__decide__", args={})

        acts = {"n": 0}

        async def act(_args):
            acts["n"] += 1
            return {"ok": True}

        llm = ScriptedLLM()
        llm.default_reply = '{"type":"tool","name":"demo.act","args":{}}'
        router = ToolRouter({"demo.act": act}, {"demo.act": "acts"})
        sup = AgentSupervisor(memory=mem, llm=llm, router=router,
                              tool_descriptions={"demo.act": "acts"},
                              cfg=SupervisorConfig(tick_seconds=0.05,
                                                   max_retries=1,
                                                   max_steps_per_goal=6))

        # Cancel from OUTSIDE, between the claim and the enqueue: hold the
        # planner until the cancel has landed.
        gate = asyncio.Event()
        original_decide = sup._decide_next

        async def gated_decide(**kw):
            out = await original_decide(**kw)
            await mem.cancel_goal(goal_id=gid)     # the window, deterministically
            gate.set()
            return out

        sup._decide_next = gated_decide
        sup.start()
        try:
            await asyncio.wait_for(gate.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            check(False, "the supervisor never reached the decision step")
        await asyncio.sleep(1.0)
        await sup.stop()

        check(acts["n"] == 0,
              f"the chosen tool never ran ({acts['n']}x)")
        rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        check(not [t for t in rows if t["tool_name"] == "demo.act"],
              f"and was never even queued "
              f"({[t['tool_name'] for t in rows]})")

        con = sqlite3.connect(str(mem._sqlite._db_path))
        try:
            events = con.execute(
                "SELECT kind, message FROM progress_events WHERE goal_id=?",
                (str(gid),)).fetchall()
        finally:
            con.close()
        plans = [m for k, m in events if k == "plan"]
        blocked = [m for k, m in events if k == "blocked"]
        check(not plans,
              f"nothing claims a next step was planned ({plans})")
        # Either honest explanation is acceptable, and which one appears
        # depends on WHEN the cancel landed. Cancelling from inside the
        # decision now also moves the goal's lifecycle generation, so the
        # supervisor discards the whole decision as stale before it gets as far
        # as trying to schedule the step — a strictly more specific reason than
        # "could not schedule it". What must never appear is a claim that the
        # step WAS planned, which `plans` above asserts.
        check(any(("NOT scheduled" in m) or ("discarded" in m) for m in blocked),
              f"and the record explains why nothing was scheduled ({blocked})")


async def test_wire_contracts():
    """STAGE 7 — the wire matrix: every implemented HTTP family, EXPECTED codes.

    The first version of this test only asserted `status < 500`. That is a much
    weaker claim than it looks: it passes for an endpoint that accepts garbage
    and answers 200, and it passed while `POST /goals/<not-a-uuid>/cancel` was
    answering 500 in a family the test never called. Each case below states the
    code it must produce, so "did not blow up" and "was correctly rejected" are
    different results.
    """
    check.section("Stage 7: the wire matrix, with expected status codes")

    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as e:  # noqa: BLE001
        check(False, f"FastAPI test client unavailable: {e}")
        return

    import types

    from backend.state import STATE
    from backend.routers import autonomy, dev, memory_api, speaker, web_maps
    from core.permissions import DEFAULT_MODE, PermissionBroker

    prev_dev_env = os.environ.get("NOVA_DEV_MODE")
    saved = (STATE.memory, STATE.runtime, STATE.config, STATE.dev_mode)
    with _tmp() as td:
        root = Path(td)
        (root / "projects").mkdir()
        (root / "core").mkdir()
        (root / "core" / "sample.py").write_text("x = 1\n", encoding="utf-8")
        try:
            m = MemoryUnifier(root / "memory", enable_chroma=False)
            await m.initialize()
            await m.add_fact(entity="user", attribute="name", value="Marcus",
                             confidence=0.9)
            gid = await m.create_goal(project_name="temp", title="real goal",
                                      objective="o", success_criteria="c")
            broker = PermissionBroker(mode=DEFAULT_MODE)
            os.environ["NOVA_DEV_MODE"] = "1"
            from core.dev_mode import DevMode

            STATE.memory = m
            STATE.runtime = types.SimpleNamespace(permission_broker=broker)
            STATE.config = types.SimpleNamespace(repo_root=root,
                                                 projects_dir=root / "projects")
            STATE.dev_mode = DevMode(repo_root=root,
                                     projects_dir=root / "projects")

            app = FastAPI()
            for r in (memory_api.router, autonomy.router, dev.router,
                      speaker.router, web_maps.router):
                app.include_router(r)
            client = TestClient(app, raise_server_exceptions=False)

            # (family, what is sent, the code it must produce, and why)
            matrix = [
                # ── memory / recall ──────────────────────────────────────────
                ("memory", "no query at all", (422,),
                 lambda: client.get("/memory/search")),
                ("memory", "empty query", (422,),
                 lambda: client.get("/memory/search", params={"q": ""})),
                ("memory", "5000-char query (long, not malformed)", (200,),
                 lambda: client.get("/memory/search", params={"q": "x" * 5000})),
                ("memory", "non-integer limit", (422,),
                 lambda: client.get("/memory/recent", params={"limit": "abc"})),
                ("memory", "negative limit", (422,),
                 lambda: client.get("/memory/recent", params={"limit": -1})),
                ("memory", "limit of a billion", (422,),
                 lambda: client.get("/memory/recent", params={"limit": 10 ** 9})),
                ("memory", "purge with no body", (422,),
                 lambda: client.post("/memory/purge")),
                ("memory", "purge with malformed JSON", (422,),
                 lambda: client.post("/memory/purge", content=b"{not json",
                                     headers={"content-type": "application/json"})),
                ("memory", "purge with null body", (422,),
                 lambda: client.post("/memory/purge", json=None)),
                ("memory", "purge with wrong field types", (422,),
                 lambda: client.post("/memory/purge",
                                     json={"entity": 123, "attribute": []})),
                ("memory", "a real search", (200,),
                 lambda: client.get("/memory/search", params={"q": "Marcus"})),
                # ── reminders ────────────────────────────────────────────────
                ("reminders", "no fields", (422,),
                 lambda: client.post("/reminders", json={})),
                ("reminders", "empty title", (422,),
                 lambda: client.post("/reminders",
                                     json={"title": "  ", "when": "5pm"})),
                ("reminders", "a time nobody can parse", (422,),
                 lambda: client.post("/reminders",
                                     json={"title": "t", "when": "zzzz"})),
                ("reminders", "cancel one that does not exist", (404,),
                 lambda: client.delete("/reminders/not-a-real-id")),
                # ── goals / autonomy ─────────────────────────────────────────
                ("goals", "create with a blank objective", (422,),
                 lambda: client.post("/goals", json={"objective": "   "})),
                ("goals", "create with the wrong type", (422,),
                 lambda: client.post("/goals", json={"objective": 5})),
                ("goals", "cancel a non-UUID id", (422,),
                 lambda: client.post("/goals/not-a-uuid/cancel")),
                ("goals", "resume a non-UUID id", (422,),
                 lambda: client.post("/goals/not-a-uuid/resume")),
                ("goals", "tasks for a non-UUID id", (422,),
                 lambda: client.get("/goals/not-a-uuid/tasks")),
                ("goals", "cancel a well-formed id that is not a goal", (404,),
                 lambda: client.post(
                     "/goals/00000000-0000-4000-8000-000000000000/cancel")),
                ("goals", "resume a well-formed id that is not a goal", (404,),
                 lambda: client.post(
                     "/goals/00000000-0000-4000-8000-000000000000/resume")),
                ("goals", "cancel a REAL goal", (200,),
                 lambda: client.post(f"/goals/{gid}/cancel")),
                ("goals", "a plan that was never written", (404,),
                 lambda: client.get("/plans/not-a-uuid")),
                ("goals", "list with a bad limit", (422,),
                 lambda: client.get("/goals", params={"limit": "abc"})),
                # ── permissions / approval ───────────────────────────────────
                ("permissions", "audit with a bad limit", (422,),
                 lambda: client.get("/permissions/audit", params={"limit": "abc"})),
                ("permissions", "audit", (200,),
                 lambda: client.get("/permissions/audit")),
                ("permissions", "resolve with no request_id", (422,),
                 lambda: client.post("/permissions/resolve",
                                     json={"approved": True})),
                ("permissions", "resolve with a non-boolean decision", (422,),
                 lambda: client.post("/permissions/resolve",
                                     json={"request_id": "x",
                                           "approved": "yes please"})),
                ("permissions", "resolve something not pending", (200,),
                 lambda: client.post("/permissions/resolve",
                                     json={"request_id": "nope",
                                           "approved": True})),
                # ── dev / self-editing ───────────────────────────────────────
                ("dev", "inspect with no path", (422,),
                 lambda: client.post("/dev/inspect", json={})),
                ("dev", "inspect outside the repo", (400,),
                 lambda: client.post("/dev/inspect",
                                     json={"path": "../../secrets.txt"})),
                ("dev", "inspect an absolute system path", (400,),
                 lambda: client.post("/dev/inspect",
                                     json={"path": "C:/Windows/win.ini"})),
                ("dev", "inspect a secrets file", (400,),
                 lambda: client.post("/dev/inspect", json={"path": ".env"})),
                ("dev", "inspect a path with a NUL byte", (400,),
                 lambda: client.post("/dev/inspect",
                                     json={"path": "core/sam\x00ple.py"})),
                ("dev", "propose to a path outside the repo", (400,),
                 lambda: client.post("/dev/propose",
                                     json={"path": "../evil.py",
                                           "new_content": "x"})),
                ("dev", "apply an unknown proposal", (400,),
                 lambda: client.post("/dev/apply",
                                     json={"proposal_id": "nope",
                                           "confirm": True})),
                ("dev", "apply without confirmation", (400,),
                 lambda: client.post("/dev/apply",
                                     json={"proposal_id": "nope"})),
                ("dev", "roll back an unknown proposal", (400,),
                 lambda: client.post("/dev/rollback",
                                     json={"proposal_id": "nope"})),
                ("dev", "read a real file", (200,),
                 lambda: client.post("/dev/inspect",
                                     json={"path": "core/sample.py"})),
                # ── speaker (request validation only; see the note below) ────
                ("speaker", "enrol with no audio", (422,),
                 lambda: client.post("/speaker/enroll",
                                     data={"display_name": "x"})),
                ("speaker", "enrol with no name", (422,),
                 lambda: client.post("/speaker/enroll",
                                     files={"files": ("a.wav", b"RIFF")})),
                ("speaker", "identify with no audio", (422,),
                 lambda: client.post("/speaker/identify")),
                ("speaker", "calibrate with a non-object body", (422,),
                 lambda: client.post("/speaker/calibration", json=[1, 2, 3])),
                # ── plugins / web / maps ─────────────────────────────────────
                ("plugins", "execute with no tool name", (422,),
                 lambda: client.post("/plugins/execute", json={})),
                ("plugins", "execute a tool that does not exist", (404,),
                 lambda: client.post("/plugins/execute",
                                     json={"name": "nope.zzz", "args": {}})),
                ("plugins", "web search with no query", (422,),
                 lambda: client.get("/api/web/search")),
                ("plugins", "geocode with no address", (422,),
                 lambda: client.get("/api/maps/geocode")),
                ("plugins", "directions with no endpoints", (422,),
                 lambda: client.get("/api/maps/directions")),
                # ── routing itself ───────────────────────────────────────────
                ("routing", "a route that does not exist", (404,),
                 lambda: client.get("/does-not-exist")),
                ("routing", "the wrong method on a real route", (405,),
                 lambda: client.delete("/memory/search")),
            ]

            server_errors: list[str] = []
            families: dict[str, int] = {}
            for family, label, expected, call in matrix:
                families[family] = families.get(family, 0) + 1
                try:
                    code = call().status_code
                except Exception as e:  # noqa: BLE001
                    check(False, f"[{family}] {label}: raised {type(e).__name__}")
                    server_errors.append(f"{family}/{label}")
                    continue
                if code >= 500:
                    server_errors.append(f"{family}/{label} -> {code}")
                check(code in expected,
                      f"[{family}] {label}: HTTP {code} "
                      f"(expected {'/'.join(str(x) for x in expected)})")

            check(not server_errors,
                  f"no case in the matrix produced a 5xx ({server_errors[:3]})")
            check(sorted(families) == ["dev", "goals", "memory", "permissions",
                                       "plugins", "reminders", "routing",
                                       "speaker"],
                  f"every implemented HTTP family is represented ({families})")

            # Two reads of the same thing agree — the surface is not stateful by
            # accident.
            first = client.get("/memory/search", params={"q": "Marcus"})
            again = client.get("/memory/search", params={"q": "Marcus"})
            check(first.status_code == 200 and first.json() == again.json(),
                  "repeating a read returns the same thing")

            # A rejected write must not have happened. The blank-objective goal
            # and the unparseable reminder are refused above; nothing appeared.
            goals = (await m.list_goals(project_name=None, limit=50))
            check(len(goals) == 1,
                  f"no refused request created a goal ({len(goals)} goal)")
            check(not await m.list_reminders(status=None, limit=50),
                  "and none created a reminder")
            # The 404 resume must not have enqueued work for a goal that does
            # not exist — the reason that route is a 404 and not a shrug.
            orphans = await m.list_goal_tasks(
                goal_id="00000000-0000-4000-8000-000000000000", limit=10)
            check(not orphans,
                  f"and resuming an unknown goal enqueued nothing ({len(orphans)})")

            # ── FAMILIES WITH NO HTTP SURFACE — recorded N/A, with evidence ──
            paths = sorted({getattr(r, "path", "") for r in app.routes})
            project_writes = [p for p in paths
                              if p.startswith("/projects") or "/project/" in p]
            check(not project_writes,
                  f"N/A — project create/delete/restore has NO HTTP endpoint; it "
                  f"is tool-only ({len(paths)} routes inspected, matches: "
                  f"{project_writes})")
            tool_exec = [p for p in paths if "execute" in p or "/tools" in p]
            check(tool_exec == ["/plugins/execute"],
                  f"N/A — the only arbitrary-execution endpoint is "
                  f"/plugins/execute ({tool_exec})")
            check(not [p for p in paths if "embedding" in p or "vector" in p],
                  "N/A — no endpoint exposes embeddings or vectors")

            # Model-backed speaker routes (/speaker/status, /profiles, /enroll
            # with real audio, /identify, POST /calibration) need a loaded
            # embedding model and real recordings. They are NOT asserted here
            # and are NOT claimed to pass: their acceptance is the human P5.2
            # calibration run, which remains outstanding.
            check(True,
                  "recorded: model-backed speaker routes are out of scope for a "
                  "deterministic stage — they are P5.2 human acceptance, still "
                  "PENDING")
        finally:
            STATE.memory, STATE.runtime, STATE.config, STATE.dev_mode = saved
            if prev_dev_env is None:
                os.environ.pop("NOVA_DEV_MODE", None)
            else:
                os.environ["NOVA_DEV_MODE"] = prev_dev_env


# ══ ROUND 2 OF THIS PR ══════════════════════════════════════════════════════
async def test_structured_failure_is_a_failure():
    """A tool that RETURNS a failure used to arrive as ToolResult(ok=True).

    Nova's tools answer with a top-level `ok` — `missing_fact`,
    `unverified_speaker`, `scoped_unavailable`, `not_approved`. Every one of
    those reached the orchestrator as a SUCCESS, so the next decision was told
    the step had worked and planned on top of it.
    """
    check.section("Stage 10: a structured tool failure is not a success")

    r = ToolRouter({})

    async def not_found(_a):
        return {"ok": False, "error": "not_found"}

    async def denied(_a):
        return {"ok": False, "status": "denied", "detail": "needs approval"}

    async def bare_false(_a):
        return {"ok": False}

    async def succeeded(_a):
        return {"ok": True, "value": 7}

    async def plain(_a):
        return {"count": 3, "error_rate": 0.1}

    async def nothing(_a):
        return None

    async def truthy_ok(_a):
        return {"ok": 1, "note": "still fine"}

    for n, f in (("t.notfound", not_found), ("t.denied", denied),
                 ("t.bare", bare_false), ("t.ok", succeeded),
                 ("t.plain", plain), ("t.none", nothing),
                 ("t.truthy", truthy_ok)):
        r.register(n, f)

    res = await r.execute(ToolCall(name="t.notfound", args={}), retries=0)
    check(res.ok is False, "ok=False -> ToolResult.ok is False")
    check(res.error == "not_found", f"with a usable error ({res.error!r})")
    check(isinstance(res.result, dict) and res.result.get("error") == "not_found",
          f"and the structured payload is KEPT, not discarded ({res.result})")

    res = await r.execute(ToolCall(name="t.denied", args={}), retries=0)
    check(res.ok is False and "approval" in (res.error or ""),
          f"a refusal carries its own words ({res.error!r})")

    res = await r.execute(ToolCall(name="t.bare", args={}), retries=0)
    check(res.ok is False and res.error == "tool reported failure",
          f"ok=False with no message still explains itself ({res.error!r})")

    res = await r.execute(ToolCall(name="t.ok", args={}), retries=0)
    check(res.ok is True and res.result["value"] == 7, "ok=True is a success")

    res = await r.execute(ToolCall(name="t.plain", args={}), retries=0)
    check(res.ok is True,
          "a dict with no `ok` key keeps success semantics, even containing "
          "the word error")

    res = await r.execute(ToolCall(name="t.truthy", args={}), retries=0)
    check(res.ok is True, "only an `ok` that is exactly False counts as failure")

    res = await r.execute(ToolCall(name="t.none", args={}), retries=0)
    check(res.ok is True and res.result is None, "None keeps success semantics")

    # An explicit logical failure is not a transient blip: even a declared
    # read-only tool is not re-invoked after saying "no".
    hits = {"n": 0}

    async def read_only_refusal(_a):
        hits["n"] += 1
        return {"ok": False, "error": "missing_query"}

    r.register("t.readfail", read_only_refusal, retry_safe=True)
    res = await r.execute(ToolCall(name="t.readfail", args={}))
    check(hits["n"] == 1,
          f"a read-only tool's explicit refusal is not retried ({hits['n']}x)")
    check(res.ok is False, "and it is still reported as a failure")


async def test_real_builtin_structured_failure():
    """The same contract, through the REAL built router and a real built-in."""
    check.section("Stage 10: a real built-in's structured failure")

    from core.tooling import build_tool_router

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        m = MemoryUnifier(root / "memory", enable_chroma=False)
        await m.initialize()
        router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)

        # memory.recall with no query answers {"ok": false, "error": "missing_query"}.
        res = await router.execute(ToolCall(name="memory.recall", args={}), retries=0)
        check(isinstance(res.result, dict) and res.result.get("ok") is False,
              f"memory.recall still returns its structured refusal ({res.result})")
        check(res.ok is False,
              f"and the router reports it as a FAILURE (ok={res.ok})")
        check(res.error == "missing_query", f"naming the reason ({res.error!r})")

        # The same tool succeeds normally — the guard is not refusing everything.
        await m.add_fact(entity="user", attribute="name", value="Marcus",
                         confidence=0.9)
        ok = await router.execute(ToolCall(name="memory.recall",
                                           args={"query": "name"}), retries=0)
        check(ok.ok is True, f"a real recall still succeeds (ok={ok.ok})")


async def test_production_retry_classification():
    """Stage 10: retry safety over the REAL registry, tool by tool."""
    check.section("Stage 10: production tools are classified by name")

    from core.tooling import build_tool_router

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        m = MemoryUnifier(root / "memory", enable_chroma=False)
        await m.initialize()
        router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
        names = set(router.list_tools())

        # Everything on this list changes something durable: a file, a memory
        # record, a plan position, an external account, or Nova's own source.
        side_effecting = [
            "project.scaffold", "project.delete", "project.restore",
            "project.purge_trash", "code.write", "shell.exec",
            "memory.remember", "memory.correct", "memory.forget",
            "plan.advance", "research.track", "experiment.record",
            "experiment.trial", "self.propose_change", "self.register_project",
            "self.apply_change",
        ]
        present = [n for n in side_effecting if n in names]
        check(len(present) >= 8,
              f"the side-effecting tools this asserts about exist ({len(present)} "
              f"of {len(side_effecting)})")
        unsafe_ok = [n for n in present if not router.is_retry_safe(n)]
        check(unsafe_ok == present,
              f"NONE of them is retry-safe (offenders: "
              f"{sorted(set(present) - set(unsafe_ok))})")

        # Stated here INDEPENDENTLY of core/tooling.py: every tool Nova is
        # allowed to re-invoke automatically, and nothing else. If someone adds
        # a name to the router's list, this fails until a human has agreed the
        # tool really only reads.
        read_only = {
            "memory.recall", "memory.recall_person", "memory.related",
            "memory.path", "memory.timeline", "code.read", "self.read_code",
            "self.list_code", "project.status", "project.trash", "plan.status",
            "research.list", "research.findings", "experiment.list",
            "experiment.analyze", "agents.roster", "agent.recall",
            "executive.brief", "skill.detect",
        }
        declared = sorted(n for n in read_only if n in names and router.is_retry_safe(n))
        check(len(declared) >= 12,
              f"the read-only built-ins ARE declared retry-safe ({len(declared)})")

        # The default is what protects everything nobody thought about.
        safe_names = {n for n in names if router.is_retry_safe(n)}
        check(safe_names == (read_only & names),
              f"retry safety covers EXACTLY that list — no more "
              f"({sorted(safe_names - read_only)}), no fewer "
              f"({sorted((read_only & names) - safe_names)})")
        check(len(names) - len(safe_names) > 20,
              f"so the large majority default to unsafe "
              f"({len(names) - len(safe_names)} of {len(names)})")

        # A plugin tool arrives through the same register() path and gets the
        # same default — plugins reach external accounts, so this is the case
        # that matters most.
        async def plugin_like(_a):
            return {"ok": True}

        router.register("plugin.send_thing", plugin_like,
                        description="a plugin that does something outward")
        check(not router.is_retry_safe("plugin.send_thing"),
              "a newly registered plugin tool defaults to NOT retry-safe")
        check(router.timeout_for("plugin.send_thing") == 25.0,
              f"and inherits the ordinary budget "
              f"({router.timeout_for('plugin.send_thing')}s)")


async def test_agent_supervisor_never_retries_an_unsafe_tool():
    """BUG 4, through the REAL AgentSupervisor loop.

    The router refuses to re-invoke an unsafe tool, but the supervisor kept its
    own retry: on failure it bumped `attempts` and requeued the SAME task, so a
    side effect was re-run one layer above the guard that exists to stop it.
    """
    check.section("Stage 10: the goal supervisor honours retry safety")

    from harness import ScriptedLLM

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        llm = ScriptedLLM()
        llm.default_reply = '{"type":"tool","name":"demo.writes","args":{}}'
        runs = {"n": 0}

        async def writes_then_fails(_args):
            runs["n"] += 1
            raise RuntimeError("failed AFTER writing")

        router = ToolRouter({"demo.writes": writes_then_fails},
                            {"demo.writes": "writes something, then fails"})
        sup = AgentSupervisor(
            memory=mem, llm=llm, router=router,
            tool_descriptions={"demo.writes": "writes"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=2,
                                 max_steps_per_goal=4),
        )
        gid = await mem.create_goal(project_name="temp", title="t",
                                    objective="o", success_criteria="c")
        await mem.enqueue_goal_task(goal_id=gid, project_name="temp",
                                    tool_name="__decide__", args={})
        sup.start()
        await asyncio.sleep(6.0)
        await sup.stop()

        tasks = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        bad = [t for t in tasks if t["tool_name"] == "demo.writes"]
        check(bool(bad), f"the supervisor really ran the tool ({len(bad)} task(s))")
        check(runs["n"] == len(bad),
              f"the side effect happened ONCE per task, not twice "
              f"({runs['n']} runs for {len(bad)} tasks)")
        check(all(int(t.get("attempts") or 0) == 0 for t in bad),
              f"no task was requeued for a retry "
              f"({[t.get('attempts') for t in bad]})")
        check(all(t["status"] != "done" for t in bad),
              f"and a failed tool is never recorded as done "
              f"({[t['status'] for t in bad]})")
        check(any("not retried" in (t.get("last_error") or "") for t in bad),
              f"the record says WHY it stopped "
              f"({[(t.get('last_error') or '')[:44] for t in bad]})")
        check(not [t for t in tasks if t["status"] not in ("done", "failed")],
              "and nothing is left claimed")


async def test_autonomy_supervisor_blocks_the_rest_of_the_plan():
    """BUG 5, through the REAL AutonomySupervisorWorker.

    A tool plan is ORDERED. The loop ran every call regardless of the previous
    result and then marked the task `tools_done`, so a plan whose second step
    failed was reported finished and its third step ran on a precondition that
    never held.
    """
    check.section("Stage 10: a failed step blocks the rest of the plan")

    import json
    import sqlite3

    from core.policy.contracts import AutonomyPlannerOutput, ToolPlanItem
    from core.workers.autonomy_supervisor import AutonomySupervisorWorker

    class _FixedPlanner:
        """The real planner's contract, without the model."""

        def __init__(self, tools: list[str]) -> None:
            self._tools = tools

        async def plan(self, **_kw) -> AutonomyPlannerOutput:
            return AutonomyPlannerOutput(
                action="tool", reason="fixed plan",
                tool_calls=[ToolPlanItem(tool=t, args={}) for t in self._tools],
                new_tasks=[], message_to_user=None)

    async def run_plan(td: Path, b_impl) -> tuple[list[str], dict]:
        ran: list[str] = []

        async def a(_args):
            ran.append("A")
            return {"ok": True}

        async def c(_args):
            ran.append("C")
            return {"ok": True}

        async def b(_args):
            ran.append("B")
            return await b_impl(_args)

        mem = MemoryUnifier(td, enable_chroma=False)
        await mem.initialize()
        router = ToolRouter({})
        router.register("s.a", a)
        router.register("s.b", b)
        router.register("s.c", c)
        worker = AutonomySupervisorWorker(
            memory=mem, planner=_FixedPlanner(["s.a", "s.b", "s.c"]),
            router=router, tick_seconds=0.05)
        tid = await mem.enqueue_task(title="do the thing", details="d",
                                     priority=1, project_name="temp",
                                     initiated_by_user=True)
        worker.start()
        for _ in range(60):
            if await mem.list_tasks(status="done", limit=5):
                break
            await asyncio.sleep(0.25)
        await worker.stop()

        con = sqlite3.connect(str(mem._sqlite._db_path))
        try:
            row = con.execute(
                "SELECT result_json FROM autonomy_tasks WHERE task_id=?",
                (str(tid),)).fetchone()
        finally:
            con.close()
        return ran, (json.loads(row[0]) if row and row[0] else {})

    async def explodes(_args):
        raise RuntimeError("B exploded")

    async def refuses(_args):
        return {"ok": False, "error": "B refused"}

    with _tmp() as td:
        for label, impl in (("an exception", explodes),
                            ("a structured refusal", refuses)):
            ran, result = await run_plan(Path(td) / label.replace(" ", "-"), impl)
            check(ran == ["A", "B"],
                  f"{label}: step C never ran after B failed ({ran})")
            check(result.get("status") == "tools_blocked",
                  f"{label}: the task is NOT reported as tools_done "
                  f"({result.get('status')!r})")
            check(result.get("failed_tool") == "s.b",
                  f"{label}: it names the step that failed "
                  f"({result.get('failed_tool')!r})")
            check(result.get("not_attempted") == ["s.c"],
                  f"{label}: and what was never attempted "
                  f"({result.get('not_attempted')})")
            check(bool(result.get("error")),
                  f"{label}: with a reason ({str(result.get('error'))[:50]!r})")

        # A clean plan still runs to the end and still says tools_done.
        async def fine(_args):
            return {"ok": True}

        ran, result = await run_plan(Path(td) / "clean", fine)
        check(ran == ["A", "B", "C"], f"a clean plan runs every step ({ran})")
        check(result.get("status") == "tools_done",
              f"and is reported as done ({result.get('status')!r})")


async def test_long_legacy_trash_lifecycle():
    """BUG 6: a near-limit legacy identity could not fit `<name>--<stamp>`."""
    check.section("Stage 11: a near-limit legacy project through the trash")

    from core.project_names import MAX_COMPONENT_LEN

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        name = "L" * (MAX_COMPONENT_LEN - 4)
        d = projects / name
        d.mkdir()
        (d / "PROJECT.md").write_text("# long legacy\n", encoding="utf-8")
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        res = pm.delete_project(name)
        entry = res["moved_to_trash"]
        check(res["project"] == name,
              f"delete reports the EXACT original identity ({len(res['project'])} chars)")
        check(len(entry) <= MAX_COMPONENT_LEN,
              f"and the trash entry fits the filesystem ({len(entry)} chars)")
        check(res["files"] == 1, f"with its one file counted ({res['files']})")

        listed = pm.list_trash()
        check(len(listed) == 1 and listed[0]["original"] == name,
              f"list_trash reports the exact original ({len(listed)} entries)")
        check(listed[0]["files"] == 1,
              f"and measures the project, not the wrapper ({listed[0]['files']})")

        back = pm.restore_project(entry)
        check(back["restored"] == name,
              f"restore brings back that exact name ({len(back['restored'])} chars)")
        check((projects / name / "PROJECT.md").read_text(encoding="utf-8").strip()
              == "# long legacy", "with its contents intact")

        # Same-second repeats must stay distinct in the long form too.
        ids = []
        for _ in range(3):
            ids.append(pm.delete_project(name)["moved_to_trash"])
            (projects / name).mkdir(exist_ok=True)
        check(len(set(ids)) == 3,
              f"three same-second long deletes stay distinct ({len(set(ids))})")
        for e in ids:
            check(len(e) <= MAX_COMPONENT_LEN, f"each entry still fits ({len(e)})")

        purged = pm.purge_trash(ids[-1])
        check(purged.get("permanent") is True,
              f"purge works on the long form ({purged.get('purged')})")
        check(ids[-1] not in [x["entry"] for x in pm.list_trash()],
              "and the entry is gone")

        # Every remaining entry is still individually restorable and confined.
        trash_root = (projects / ".trash").resolve()
        for e in pm.list_trash():
            p = (projects / ".trash" / e["entry"]).resolve()
            check(str(p).startswith(str(trash_root)),
                  f"{e['entry'][:16]}... stays inside the trash directory")
            check(e["original"] == name,
                  f"and still knows its original identity ({len(e['original'])})")


async def main():
    await test_side_effects_are_never_retried()
    await test_tool_contract_edges()
    await test_tool_chain_dependency()
    await test_concurrency_races()
    await test_concurrency_matrix()
    await test_seeded_sequence_fuzzing()
    await test_rapid_delete_restore_delete_does_not_collide()
    await test_structured_failure_is_a_failure()
    await test_real_builtin_structured_failure()
    await test_production_retry_classification()
    await test_agent_supervisor_never_retries_an_unsafe_tool()
    await test_autonomy_supervisor_blocks_the_rest_of_the_plan()
    await test_long_legacy_trash_lifecycle()
    await test_failed_delete_leaves_no_mislabelled_entry()
    await test_supervisor_never_retries_a_refusal()
    await test_cancelled_goal_starts_no_new_work()
    await test_cancel_races_the_claim()
    await test_resume_is_idempotent_and_keeps_its_project()
    await test_supervisor_honours_the_wire_lifecycle()
    await test_cancelled_goal_accepts_no_new_work()
    await test_the_claim_itself_refuses_a_non_active_goal()
    await test_supervisor_does_not_report_a_step_it_could_not_schedule()
    await test_wire_contracts()
    check.finish()


if __name__ == "__main__":
    run(main)
