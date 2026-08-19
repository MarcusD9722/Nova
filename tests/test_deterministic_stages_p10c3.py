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


# ── STAGE 11 ────────────────────────────────────────────────────────────────
async def test_seeded_sequence_fuzzing():
    check.section("Stage 11: fixed-seed sequences over the four subsystems")

    SEEDS = [1, 7, 13, 42, 99, 2026, 31337]
    ACTIONS = ["create", "delete", "restore", "remember", "approve", "deny",
               "timeout", "tool_ok", "tool_fail", "tool_timeout", "list"]

    for seed in SEEDS:
        rng = random.Random(seed)
        with _tmp() as td:
            root = Path(td)
            projects = root / "projects"
            projects.mkdir(parents=True)
            pm = ProjectManager(repo_root=root, projects_dir=projects)
            m = MemoryUnifier(root / "memory")
            await m.initialize()
            broker = PermissionBroker(mode=DEFAULT_MODE)
            r = ToolRouter({})
            side_effects = {"n": 0}

            async def effect(_args):
                side_effects["n"] += 1
                return {"ok": True}

            async def failing(_args):
                side_effects["n"] += 1
                raise RuntimeError("nope")

            async def hanging(_args):
                side_effects["n"] += 1
                await asyncio.sleep(5)

            r.register("t.ok", effect)
            r.register("t.fail", failing)
            r.register("t.timeout", hanging, timeout_s=0.05)

            trash_entries: list[str] = []
            errors: list[str] = []

            for step in range(40):
                action = rng.choice(ACTIONS)
                try:
                    if action == "create":
                        pm.scaffold_project(f"P{rng.randint(0, 4)}")
                    elif action == "delete":
                        live = pm.list_projects()
                        if live:
                            res = pm.delete_project(rng.choice(live))
                            trash_entries.append(res["moved_to_trash"])
                    elif action == "restore":
                        if trash_entries:
                            e = trash_entries.pop(rng.randrange(len(trash_entries)))
                            try:
                                pm.restore_project(e)
                            except FileExistsError:
                                pass       # refusing to clobber is correct
                    elif action == "remember":
                        await m.add_fact(entity=f"project:P{rng.randint(0, 4)}",
                                         attribute="note",
                                         value=f"s{seed}-{step}", confidence=0.9)
                    elif action in ("approve", "deny"):
                        d = await broker.request("project.delete", details={})
                        broker.resolve(d["request_id"], action == "approve")
                    elif action == "timeout":
                        d = await broker.request("project.delete", details={})
                        await broker.await_decision(d["request_id"], timeout_s=0.02)
                    elif action == "tool_ok":
                        await r.execute(ToolCall(name="t.ok", args={}))
                    elif action == "tool_fail":
                        before = side_effects["n"]
                        await r.execute(ToolCall(name="t.fail", args={}))
                        if side_effects["n"] - before > 1:
                            errors.append(f"step {step}: t.fail ran twice")
                    elif action == "tool_timeout":
                        before = side_effects["n"]
                        await r.execute(ToolCall(name="t.timeout", args={}))
                        if side_effects["n"] - before > 1:
                            errors.append(f"step {step}: t.timeout ran twice")
                    elif action == "list":
                        pm.list_projects()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"step {step} {action}: {type(e).__name__}: {e}")

                # INVARIANTS, after every single step.
                names = [p.name for p in projects.iterdir()
                         if p.is_dir() and not p.name.startswith(".")]
                if len(names) != len(set(names)):
                    errors.append(f"step {step}: duplicate project names {names}")
                for n in names:
                    if not (projects / n).is_dir():
                        errors.append(f"step {step}: listed but missing {n}")
                if broker.pending():
                    # Every request in this fuzz is settled synchronously or timed
                    # out, so nothing may be left hanging.
                    errors.append(f"step {step}: {len(broker.pending())} left pending")

            check(not errors,
                  f"seed {seed}: 40 mixed actions, no invariant broken "
                  f"({errors[:2] if errors else 'clean'})")


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


async def test_wire_contracts():
    check.section("Stage 7: real HTTP, malformed input never 5xx")

    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as e:  # noqa: BLE001
        check(False, f"FastAPI test client unavailable: {e}")
        return

    from backend.app import STATE
    from backend.routers import memory_api

    with _tmp() as td:
        m = MemoryUnifier(Path(td))
        await m.initialize()
        await m.add_fact(entity="user", attribute="name", value="Marcus",
                         confidence=0.9)
        STATE.memory = m

        app = FastAPI()
        app.include_router(memory_api.router)
        client = TestClient(app, raise_server_exceptions=False)

        cases = [
            ("missing required query", lambda: client.get("/memory/search")),
            ("empty query", lambda: client.get("/memory/search", params={"q": ""})),
            ("oversized query",
             lambda: client.get("/memory/search", params={"q": "x" * 5000})),
            ("non-integer limit",
             lambda: client.get("/memory/recent", params={"limit": "abc"})),
            ("negative limit",
             lambda: client.get("/memory/recent", params={"limit": -1})),
            ("absurd limit",
             lambda: client.get("/memory/recent", params={"limit": 10 ** 9})),
            ("unknown id", lambda: client.get("/plans/not-a-uuid")),
            ("no body", lambda: client.post("/memory/purge")),
            ("malformed json",
             lambda: client.post("/memory/purge", content=b"{not json",
                                 headers={"content-type": "application/json"})),
            ("null body", lambda: client.post("/memory/purge", json=None)),
            ("wrong field types",
             lambda: client.post("/memory/purge",
                                 json={"entity": 123, "attribute": []})),
            ("missing body fields", lambda: client.post("/reminders", json={})),
            ("duplicate submission",
             lambda: client.get("/memory/search", params={"q": "Marcus"})),
        ]

        for label, call in cases:
            try:
                resp = call()
                code = resp.status_code
                blew_up = False
            except Exception as e:  # noqa: BLE001
                code, blew_up = f"{type(e).__name__}", True
            check(not blew_up and isinstance(code, int) and code < 500,
                  f"{label}: HTTP {code} (never 5xx, never an unhandled exception)")

        # A valid request still works — the validation is not simply refusing all.
        ok = client.get("/memory/search", params={"q": "Marcus"})
        check(ok.status_code == 200, f"a valid search still succeeds ({ok.status_code})")

        # Repeating a read is stable.
        again = client.get("/memory/search", params={"q": "Marcus"})
        check(again.status_code == 200 and again.json() == ok.json(),
              "and repeating it returns the same thing")

        STATE.memory = None


async def main():
    await test_side_effects_are_never_retried()
    await test_tool_contract_edges()
    await test_tool_chain_dependency()
    await test_concurrency_races()
    await test_seeded_sequence_fuzzing()
    await test_rapid_delete_restore_delete_does_not_collide()
    await test_wire_contracts()
    check.finish()


if __name__ == "__main__":
    run(main)
