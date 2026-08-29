"""Six long stories, told across process deaths (Stage 13C §13).

Each journey is something a person would actually do over a week: start a
project, change their mind, lose the machine, come back, ask what happened,
carry on. What makes them worth writing is not their length but that the
machine dies in the middle of each one, repeatedly, and has to pick the story
back up from nothing but what reached the disk.

WORK IS STARTED AND RUN INSIDE ONE LIFE, which is not a detail. Every boot
cancels the queued work it inherits - deliberately, so nothing runs unasked -
so a journey that queued steps in one life and tried to claim them in the next
would find nothing but the continuation a resume creates, and would quietly be
testing almost nothing. A running Nova plans and executes in the same process;
these journeys do too, and the restarts then interrupt work that is genuinely
in flight.

HOW TRANSITIONS ARE COUNTED. Not by counting steps taken - a test can take a
thousand steps and change nothing. After every life the authoritative rows are
diffed against the previous snapshot BY IDENTITY (goal id, task id), and each
field that genuinely changed counts once. A row appearing counts once, not
once per field. A step that changes nothing scores nothing, so the total
cannot be padded by doing more; it goes up only by reaching more states.

Run:  venv\\Scripts\\python.exe tests\\test_journeys_s13c.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "3600")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

TARGET = 225
A, B = "flappy-bird", "quickcalc"
MARKER = "the sprite sheet is missing"

VALID_PAIRS = {
    ("queued", "pending"), ("running", "pending"), ("blocked", "pending"),
    ("done", "succeeded"), ("done", "unknown"),
    ("failed", "failed"), ("failed", "unknown"),
    ("cancelled", "never_started"), ("cancelled", "unknown"),
    ("superseded", "never_started"), ("superseded", "unknown"),
    ("superseded", "succeeded"), ("superseded", "failed"),
}

#: Prepended to every life: recovery, then the tools a story needs.
SNAP = '''
    import uuid as _uuid
    MARKER = "the sprite sheet is missing"
    _rec = await mem.cancel_pending_background_work()

    async def snap():
        gs = await mem.list_goals(limit=100)
        ts = await mem.list_goal_tasks(limit=400)
        out = {}
        for g in gs:
            out["goal:" + str(g["goal_id"])] = {
                "status": g["status"], "generation": int(g["generation"]),
                "project": g["project_name"]}
        for t in ts:
            out["task:" + str(t["task_id"])] = {
                "status": t["status"], "outcome": t["outcome"],
                "generation": int(t["generation"]),
                "project": t["project_name"]}
        return out

    async def claim_for(gid):
        """The next step OF THIS GOAL. A bare claim takes the oldest runnable
        row anywhere, which is rarely the one the story is about."""
        for _ in range(60):
            c = await mem.claim_next_goal_task()
            if c is None:
                return None
            if str(c["goal_id"]) == str(gid):
                return c
        return None

    async def work(gid, project, tools, finish):
        """Resume, queue some steps, and take them as far as `finish` says.

        `finish` is one verdict per tool: "done", "failed", or "leave" to walk
        away holding it - which is what a crash interrupts.
        """
        await mem.resume_goal(goal_id=_uuid.UUID(str(gid)))
        for _tool in tools:
            await mem.enqueue_goal_task(goal_id=_uuid.UUID(str(gid)),
                                        project_name=project,
                                        tool_name=_tool, args={})
        seen = []
        for _want in finish:
            c = await claim_for(gid)
            if c is None:
                break
            seen.append([c["tool_name"], str(c["task_id"])])
            if _want == "leave":
                continue
            await mem.complete_goal_task(
                task_id=str(c["task_id"]),
                status=("done" if _want == "done" else "failed"),
                result=({"ok": True} if _want == "done" else {}),
                error=("" if _want == "done" else MARKER),
                expected_generation=int(c["generation"]))
        return seen
'''

FINISH = '''
    emit({"snapshot": await snap()})
'''


class Journey:
    """One story. Holds the directory, the snapshots, and the transitions."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = root
        self.prev: dict = {}
        self.transitions: list[tuple[str, str, object, object]] = []
        self.bad: list[str] = []
        self.last: list[dict] = []

    def live(self, body: str, *, crash: bool = False, full: bool = False,
             timeout: float = 300.0) -> list[dict]:
        """One life: a fresh interpreter, recovery, the body, a snapshot."""
        src = SNAP + body + ("\n    CRASH()\n" if crash else FINISH)
        out = run_step(self.root, src, expect_crash=crash, full=full,
                       timeout=timeout)
        self.last = out
        if not crash:
            self._absorb(one(out, "snapshot") or {})
        return out

    def _absorb(self, snapshot: dict) -> None:
        for key, fields in snapshot.items():
            before = self.prev.get(key)
            if before is None:
                self.transitions.append(
                    (key, "created", None, fields.get("status")))
            else:
                for field, value in fields.items():
                    if before.get(field) != value:
                        self.transitions.append(
                            (key, field, before.get(field), value))
            if key.startswith("task:"):
                pair = (fields.get("status"), fields.get("outcome"))
                if pair not in VALID_PAIRS:
                    self.bad.append(f"{key[:14]} is {pair[0]}/{pair[1]}")
        for key in self.prev:
            if key not in snapshot:
                self.bad.append(f"{key[:14]} vanished")
        self.prev = snapshot

    def report(self) -> int:
        n = len(self.transitions)
        check(not self.bad,
              f"{self.name}: every state along the way was coherent "
              f"({self.bad[:2] if self.bad else 'ok'})")
        print(f"      {self.name}: {n} transitions")
        return n


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _new_goal(project: str, title: str) -> str:
    return f'''
    _g = await mem.create_goal(project_name="{project}", title="{title}",
                               objective="o", success_criteria="c")
    emit({{"goal": str(_g)}})
'''


async def journey_1_a_build_that_keeps_being_interrupted(total: list[int]):
    """Six working lives, dying with a step in flight in half of them."""
    with _tmp() as td:
        j = Journey("J1 a build interrupted again and again", Path(td) / "n")
        gid = one(j.live(_new_goal(A, "build the game")), "goal")

        plan = [(["code.plan", "code.write"], ["done", "leave"], True),
                (["code.write", "code.test"], ["done", "done"], False),
                (["code.test", "code.art"], ["failed", "leave"], True),
                (["code.art", "code.sound"], ["done", "leave"], True),
                (["code.sound", "code.ship"], ["done", "done"], False),
                (["code.menu", "code.pause"], ["done", "leave"], True),
                (["code.pause", "code.score"], ["failed", "done"], False),
                (["code.score", "code.tune"], ["done", "leave"], True),
                (["code.tune", "code.balance"], ["done", "done"], False),
                (["code.polish"], ["leave"], False)]
        for tools, finish, dies in plan:
            j.live(f'''
    seen = await work("{gid}", "{A}", {tools!r}, {finish!r})
    emit({{"seen": seen}})
''', crash=dies)
            if dies:
                j.live('    emit({"picked_up": True})')

        final = j.prev
        gen = final.get("goal:" + gid, {}).get("generation", 0)
        outcomes = sorted({v.get("outcome") for k, v in final.items()
                           if k.startswith("task:")})
        check(gen >= 10, f"J1: many revisions later ({gen})")
        check("unknown" in outcomes,
              f"J1: the steps a crash interrupted are still unknown ({outcomes})")
        check("succeeded" in outcomes and "failed" in outcomes,
              f"J1: alongside real successes and a real failure ({outcomes})")
        total.append(j.report())


async def journey_2_two_changes_of_mind(total: list[int]):
    """R1 -> R2 -> R3, with a worker from each revision reporting late."""
    with _tmp() as td:
        j = Journey("J2 two changes of mind", Path(td) / "n")
        gid = one(j.live(_new_goal(A, "add a pause menu")), "goal")

        held = j.live(f'''
    seen = await work("{gid}", "{A}", ["code.write", "code.test"],
                      ["done", "leave"])
    rows = await mem.list_goal_tasks(goal_id="{gid}", limit=30)
    live = [t for t in rows if t["status"] == "running"]
    emit({{"seen": seen,
           "held": None if not live else [str(live[0]["task_id"]),
                                          int(live[0]["generation"])]}})
''')
        t1, g1 = (one(held, "held") or [None, 0])
        check(t1 is not None, "J2: a step is genuinely in flight")

        j.live(f'''
    await mem.cancel_goal(goal_id=_uuid.UUID("{gid}"))
    await mem.resume_goal(goal_id=_uuid.UUID("{gid}"))
    emit({{"r2": True}})
''', crash=True)

        after = j.live(f'''
    v = await mem.complete_goal_task(
        task_id="{t1}", status="done", result={{"ok": True}}, error="",
        expected_generation={g1})
    emit({{"verdict": v}})
''')
        check(one(after, "verdict") in ("ignored", "superseded"),
              f"J2: the R1 worker's report does not land ({one(after, 'verdict')})")

        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.rework", "code.test"],
                      ["failed", "leave"])
    emit({{"seen": seen}})
''')
        j.live(f'''
    await mem.cancel_goal(goal_id=_uuid.UUID("{gid}"))
    await mem.resume_goal(goal_id=_uuid.UUID("{gid}"))
    emit({{"r3": True}})
''', crash=True)
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.final", "code.check"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        j.live(f'''
    await mem.cancel_goal(goal_id=_uuid.UUID("{gid}"))
    seen = await work("{gid}", "{A}", ["code.redo", "code.verify"],
                      ["done", "done"])
    emit({{"r4": True, "seen": seen}})
''')

        end = j.live(f'''
    g = await mem.get_goal(goal_id=_uuid.UUID("{gid}"))
    rows = await mem.list_goal_tasks(goal_id="{gid}", limit=30)
    emit({{"gen": int(g["generation"]),
           "gens": sorted({{int(t["generation"]) for t in rows}})}})
''')
        check(len(one(end, "gens") or []) >= 3,
              f"J2: three revisions of work, still told apart ({one(end, 'gens')})")
        check(int(one(end, "gen")) >= 4,
              f"J2: and the goal is on the newest of them ({one(end, 'gen')})")
        total.append(j.report())


async def journey_3_two_projects_that_never_touch(total: list[int]):
    with _tmp() as td:
        j = Journey("J3 two projects, one machine", Path(td) / "n")
        ga = one(j.live(_new_goal(A, "A work")), "goal")
        gb = one(j.live(_new_goal(B, "B work")), "goal")

        for tools_a, fin_a, tools_b, fin_b, dies in (
                (["a.plan", "a.write"], ["done", "leave"],
                 ["b.plan", "b.write"], ["done", "done"], True),
                (["a.write", "a.test"], ["done", "done"],
                 ["b.test"], ["failed"], False),
                (["a.art", "a.sound"], ["done", "leave"],
                 ["b.ui", "b.keys"], ["failed", "leave"], True),
                (["a.sound", "a.menu"], ["failed", "done"],
                 ["b.keys", "b.help"], ["done", "done"], False),
                (["a.ship"], ["leave"], ["b.ship"], ["leave"], True)):
            j.live(f'''
    sa = await work("{ga}", "{A}", {tools_a!r}, {fin_a!r})
    sb = await work("{gb}", "{B}", {tools_b!r}, {fin_b!r})
    emit({{"a": sa, "b": sb}})
''', crash=dies)
            if dies:
                j.live('    emit({"picked_up": True})')

        j.live(f'''
    await mem.cancel_goal(goal_id=_uuid.UUID("{ga}"))
    await mem.add_fact(entity="projects", attribute="last_active",
                       value="{B}", confidence=0.95)
    emit({{"cancelled_a": True}})
''', crash=True)

        seen = j.live(f'''
    sb = await work("{gb}", "{B}", ["b.final"], ["done"])
    ga_now = await mem.get_goal(goal_id=_uuid.UUID("{ga}"))
    rows = await mem.list_goal_tasks(limit=200)
    emit({{"a_status": ga_now["status"], "b": sb,
           "a_live": [t["tool_name"] for t in rows
                      if t["project_name"] == "{A}"
                      and t["status"] in ("queued", "running")],
           "a_owned": sorted({{t["project_name"] for t in rows
                              if t["tool_name"].startswith("a.")}}),
           "b_owned": sorted({{t["project_name"] for t in rows
                              if t["tool_name"].startswith("b.")}})}})
''')
        check(one(seen, "a_status") == "cancelled",
              f"J3: A stays cancelled through all of B's work "
              f"({one(seen, 'a_status')})")
        check(one(seen, "a_live") == [],
              f"J3: and nothing of A's is running ({one(seen, 'a_live')})")
        check(one(seen, "a_owned") == [A] and one(seen, "b_owned") == [B],
              f"J3: every step still belongs to the project that made it "
              f"({one(seen, 'a_owned')} / {one(seen, 'b_owned')})")
        total.append(j.report())


async def journey_4_a_deletion_nobody_answered(total: list[int]):
    """Asked, died, came back, asked again, approved."""
    with _tmp() as td:
        j = Journey("J4 a deletion across three lives", Path(td) / "n")
        gid = one(j.live(_new_goal(A, "the project itself")), "goal")
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.write", "code.test"],
                      ["done", "failed"])
    emit({{"seen": seen}})
''')

        j.live('''
    import asyncio
    from core.tool_router import ToolCall
    proj = nova.projects_dir / "flappy-bird"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "PROJECT.md").write_text("# flappy-bird\\n", encoding="utf-8")
    broker = nova.runtime.permission_broker
    asyncio.create_task(nova.runtime.router.execute(
        ToolCall(name="project.delete", args={"name": "flappy-bird"})))
    for _ in range(500):
        if broker.pending():
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the delete never asked for permission")
''', crash=True, full=True)

        back = j.live('''
    broker = nova.runtime.permission_broker
    emit({"pending": broker.pending(),
          "exists": (nova.projects_dir / "flappy-bird" / "PROJECT.md").exists()})
''', full=True)
        check(one(back, "pending") == [] and one(back, "exists") is True,
              f"J4: the unanswered deletion did not happen "
              f"({one(back, 'pending')}, exists={one(back, 'exists')})")

        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.more", "code.again"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.again", "code.finish"],
                      ["failed", "done"])
    emit({{"seen": seen}})
''')

        refused = j.live('''
    import asyncio
    from core.tool_router import ToolCall
    broker = nova.runtime.permission_broker
    task = asyncio.create_task(nova.runtime.router.execute(
        ToolCall(name="project.delete", args={"name": "flappy-bird"})))
    for _ in range(500):
        if broker.pending():
            break
        await asyncio.sleep(0.02)
    rid = broker.pending()[0]["request_id"]
    broker.resolve(rid, False, by="journey")
    out = await task
    emit({"ok": out.ok, "settled": broker.settled_as(rid),
          "exists": (nova.projects_dir / "flappy-bird" / "PROJECT.md").exists()})
''', full=True)
        check(one(refused, "ok") is False
              and one(refused, "settled") == "rejected",
              f"J4: declining it keeps the project ({one(refused, 'settled')})")
        check(one(refused, "exists") is True, "J4: the folder is still there")

        # Declining is not abandoning: the work carries on afterwards.
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.carry", "code.on"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.on", "code.last"],
                      ["done", "failed"])
    emit({{"seen": seen}})
''')

        done = j.live('''
    import asyncio
    from core.tool_router import ToolCall
    broker = nova.runtime.permission_broker
    task = asyncio.create_task(nova.runtime.router.execute(
        ToolCall(name="project.delete", args={"name": "flappy-bird"})))
    for _ in range(500):
        if broker.pending():
            break
        await asyncio.sleep(0.02)
    rid = broker.pending()[0]["request_id"]
    broker.resolve(rid, True, by="journey")
    out = await task
    emit({"ok": out.ok, "settled": broker.settled_as(rid),
          "exists": (nova.projects_dir / "flappy-bird" / "PROJECT.md").exists()})
''', full=True)
        check(one(done, "ok") is True and one(done, "settled") == "approved",
              f"J4: asking a third time and approving does delete it "
              f"({one(done, 'ok')})")
        check(one(done, "exists") is False, "J4: and the folder is gone")
        total.append(j.report())


async def journey_5_a_plan_and_the_file_underneath_it(total: list[int]):
    with _tmp() as td:
        root = Path(td) / "n"
        j = Journey("J5 a plan, a crash, a changed file", root)
        gid = one(j.live(_new_goal(A, "tune the physics")), "goal")
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.measure", "code.tune"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''')

        made = j.live(f'''
    from core import dev_mode as dm
    outside = Path(r"{root}") / "outside" / "proj"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "game.py").write_text("SPEED = 1\\n", encoding="utf-8")
    d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                   projects_dir=Path(r"{root}") / "repo" / "projects")
    d.register_external_root("proj", str(outside))
    p1 = d.propose_change("game.py", "SPEED = 2\\n", reason="faster",
                          project="proj")
    p2 = d.propose_change("game.py", "SPEED = 3\\n", reason="faster still",
                          project="proj")
    emit({{"p1": p1.id, "p2": p2.id, "base": p1.base_sha}})
''')
        p1, p2 = one(made, "p1"), one(made, "p2")
        check(bool(one(made, "base")), "J5: each plan records its baseline")

        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.tune", "code.trial"],
                      ["failed", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.trial", "code.measure2"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        (root / "outside" / "proj" / "game.py").write_text(
            "SPEED = 1\nGRAVITY = 9\n", encoding="utf-8")

        out = j.live(f'''
    from core import dev_mode as dm
    d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                   projects_dir=Path(r"{root}") / "repo" / "projects")
    d.register_external_root("proj", str(Path(r"{root}") / "outside" / "proj"))
    res = {{}}
    for _pid in ("{p1}", "{p2}"):
        try:
            d.apply_proposal(_pid, confirm=True)
            res[_pid] = "applied"
        except Exception as e:
            res[_pid] = str(e)[:80]
    emit({{"res": res}})
''')
        res = one(out, "res") or {}
        check(all(v != "applied" for v in res.values()),
              f"J5: neither stale plan is applied to a file that moved on ({res})")
        content = (root / "outside" / "proj" / "game.py").read_text(encoding="utf-8")
        check("GRAVITY = 9" in content and "SPEED = 1" in content,
              f"J5: and the newer file is untouched ({content!r})")

        # The work goes on regardless: a refused plan is not a dead project.
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.retune", "code.verify"],
                      ["done", "leave"])
    emit({{"seen": seen}})
''', crash=True)
        j.live(f'''
    seen = await work("{gid}", "{A}", ["code.verify", "code.signoff"],
                      ["done", "done"])
    emit({{"seen": seen}})
''')
        total.append(j.report())


async def journey_6_coming_back_and_asking(total: list[int]):
    """Rich state, a death, six real questions, then acting on the answers."""
    with _tmp() as td:
        j = Journey("J6 coming back and asking", Path(td) / "n")
        g1 = one(j.live(_new_goal(A, "add a pause menu")), "goal")
        g2 = one(j.live(_new_goal(A, "add sound")), "goal")

        g3 = one(j.live(_new_goal(B, "the calculator")), "goal")
        j.live(f'''
    s1 = await work("{g1}", "{A}", ["code.write", "code.test", "code.ship"],
                    ["done", "failed", "leave"])
    s2 = await work("{g2}", "{A}", ["code.write"], ["leave"])
    s3 = await work("{g3}", "{B}", ["calc.parse", "calc.eval"],
                    ["done", "leave"])
    emit({{"s1": s1, "s2": s2, "s3": s3}})
''', crash=True)
        j.live(f'''
    s = await work("{g1}", "{A}", ["code.retry", "code.ship"],
                   ["failed", "leave"])
    emit({{"s": s}})
''', crash=True)

        asked = j.live('''
    async def ask(message):
        before = len(nova.llm.prompts)
        await nova.http.post("/chat", json={"message": message})
        new = nova.llm.prompts[before:]
        answers = [p for p in new
                   if "You are Nova" in p and "agent brain for Nova" not in p]
        return answers[-1] if answers else ""

    out = {}
    for q in ("What happened?", "What failed?", "What is still pending?",
              "Is anything still running?", "What can resume?",
              "So everything finished, right?"):
        out[q] = await ask(q)
    emit({"ground": out})
''', full=True)
        ground = one(asked, "ground") or {}
        missing = [q for q, g in ground.items()
                   if "The work you are actually tracking" not in g]
        check(not missing,
              f"J6: all six questions are answered from the record ({missing})")
        blind = [q for q, g in ground.items() if MARKER not in g]
        check(not blind, f"J6: and every one of them sees the failure ({blind})")

        j.live(f'''
    await mem.resume_goal(goal_id=_uuid.UUID("{g1}"))
    await mem.cancel_goal(goal_id=_uuid.UUID("{g2}"))
    emit({{"acted": True}})
''', crash=True)

        again = j.live('''
    async def ask(message):
        before = len(nova.llm.prompts)
        await nova.http.post("/chat", json={"message": message})
        new = nova.llm.prompts[before:]
        answers = [p for p in new
                   if "You are Nova" in p and "agent brain for Nova" not in p]
        return answers[-1] if answers else ""
    emit({"ground": await ask("What happened?"),
          "cancelled": await ask("What was cancelled?")})
''', full=True)
        check(MARKER in (one(again, "ground") or ""),
              "J6: the failure is still there two restarts later")
        check("add sound" in (one(again, "cancelled") or ""),
              "J6: and the goal the user cancelled is named as cancelled")
        total.append(j.report())


async def main() -> None:
    check.section("§13 six journeys, each of them interrupted")
    total: list[int] = []
    await journey_1_a_build_that_keeps_being_interrupted(total)
    await journey_2_two_changes_of_mind(total)
    await journey_3_two_projects_that_never_touch(total)
    await journey_4_a_deletion_nobody_answered(total)
    await journey_5_a_plan_and_the_file_underneath_it(total)
    await journey_6_coming_back_and_asking(total)

    check.section("§13 transitions")
    n = sum(total)
    check(len(total) == 6, f"six journeys completed ({len(total)})")
    check(n > TARGET,
          f"{n} genuine state transitions, counted by diffing authoritative "
          f"rows by identity (target > {TARGET})")


if __name__ == "__main__":
    run(main)
    check.finish()
