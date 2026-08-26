"""Journeys 10 and 11 (Stage 13B closure round 2).

JOURNEY 10 - the destructive-intent boundary, end to end through /chat.

The rule the whole journey exists to prove:

    UNCERTAIN DESTRUCTIVE INTENT NEVER DEFAULTS TO A FILESYSTEM EDIT.

Measured on 683b7ab, with flappy-bird as the last-active project:

    "Delete it from my projects."
      -> ProjectBuilder.improve("flappy-bird", "Delete it from my projects.")
      -> "Got it - working on those improvements to flappy-bird now."

A removal command started an autonomous write. Its siblings were just as bad in
the other direction: "Remove the bird from flappy-bird." classified as a
WHOLE-PROJECT removal, because `bird` is a component of the slug - so a
feature-removal request was routed at a permission-gated delete. And "Retire
flappy-bird." mapped onto delete, a lifecycle Nova does not have.

JOURNEY 11 - current versus historical progress.

Goals are generation-fenced; progress events were not. A retry and a failure
from run N read identically to activity on run N+1, with nothing to tell them
apart. Provenance is now stamped at WRITE time by the producer - never at read
time from the goal's current generation, which would relabel history every time
a goal resumed.

Run:  venv\\Scripts\\python.exe tests\\test_removal_and_provenance_journeys_s13b.py
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

from harness import Checks, boot, run  # noqa: E402

from core.event_bus import BUS  # noqa: E402

check = Checks()

GAME = "flappy-bird"
CLAIMS = ("deleted", "removed", "retired", "archived", "off the list", "gone")


class Journey:
    def __init__(self) -> None:
        self.n = 0

    def step(self, what: str) -> str:
        self.n += 1
        return f"[{self.n:02d}] {what}"


class Bus:
    def __init__(self) -> None:
        self._q = None
        self.events: list = []

    def __enter__(self):
        self._q = BUS.subscribe()
        return self

    def __exit__(self, *exc):
        self.drain()
        if self._q is not None:
            BUS.unsubscribe(self._q)
        self._q = None

    def drain(self):
        while True:
            try:
                self.events.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                return self.events

    def deletes(self) -> list[str]:
        self.drain()
        return [str(e.data.get("request_id")) for e in self.events
                if e.type == "permission.requested"
                and str((e.data or {}).get("capability")) == "project.delete"]


async def _say(nova, message: str) -> tuple[str, list[str], list]:
    """One turn. Returns (assistant text, delete request ids, improve calls)."""
    nova._improves.clear()
    with Bus() as bus:
        r = await nova.http.post("/chat", json={"message": message})
        ids = bus.deletes()
    said = str((r.json() if r.status_code == 200 else {}).get("assistant") or "")
    return said, ids, list(nova._improves)


async def journey_ten_destructive_intent_boundary(nova):
    check.section("journey 10: the destructive-intent boundary")
    j = Journey()

    proj = nova.root / "projects" / GAME
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "PROJECT.md").write_text(f"# {GAME}\n\n## Brief\nA game.\n",
                                     encoding="utf-8")
    pb = nova.runtime._project_builder
    await pb._set_last_active(GAME)
    check(await pb.last_active() == GAME,
          j.step(f"the current project is {GAME}"))
    check((proj / "PROJECT.md").exists(),
          j.step("and it exists on disk"))

    # 03-06  an explicit whole-project delete reaches the gate, and a denial
    #        leaves the project alone.
    broker = nova.runtime._permission_broker

    async def answer(approved: bool):
        for _ in range(300):
            await asyncio.sleep(0.02)
            if broker.pending():
                broker.resolve(str(broker.pending()[0]["request_id"]), approved)
                return

    nova._delete_target["name"] = GAME
    denier = asyncio.create_task(answer(False))
    said, ids, improves = await _say(nova, f"Delete the project {GAME}?")
    await denier
    check(len(ids) == 1, j.step(f"an explicit delete asks once ({len(ids)})"))
    check(not improves, j.step(f"and starts no edit ({improves})"))
    check((proj / "PROJECT.md").exists(),
          j.step("a denial leaves the project on disk"))
    check(not [w for w in CLAIMS if w in said.lower()],
          j.step(f"and Nova does not claim otherwise ({said[:70]!r})"))

    # 07-10  the ambiguous removal: NO edit, NO delete, a question instead.
    said, ids, improves = await _say(nova, "Delete it from my projects.")
    check(not improves,
          j.step(f"an ambiguous removal starts NO edit ({improves})"))
    check(not ids,
          j.step(f"and raises no delete request ({ids})"))
    check((proj / "PROJECT.md").exists(),
          j.step("the project is untouched"))
    check("delete the whole" in said.lower() or "inside it" in said.lower(),
          j.step(f"Nova asks which was meant ({said[:80]!r})"))

    said2, ids2, improves2 = await _say(nova, "Remove it from the projects list.")
    check(not improves2 and not ids2,
          j.step(f"the same for a second phrasing ({improves2}, {ids2})"))

    # 11-14  a feature removal whose object SHARES a slug component.
    said, ids, improves = await _say(nova, f"Remove the bird from {GAME}.")
    check(not ids,
          j.step(f"removing a feature raises NO delete request ({ids})"))
    check(len(improves) == 1,
          j.step(f"it is ordinary edit work ({improves})"))
    check(improves and improves[0][1] == f"Remove the bird from {GAME}.",
          j.step("and the EXACT instruction reaches the edit path"))
    check((proj / "PROJECT.md").exists(),
          j.step("with the project still there"))

    said, ids, improves = await _say(nova, f"Remove the project banner from {GAME}.")
    check(not ids and len(improves) == 1,
          j.step(f"a generic word as a MODIFIER is still a feature "
                 f"({ids}, {len(improves)})"))

    # 16-19  retire/archive: a lifecycle Nova does not have.
    for word in ("Retire", "Archive"):
        said, ids, improves = await _say(nova, f"{word} {GAME}.")
        check(not ids and not improves,
              j.step(f"{word.lower()} does nothing ({ids}, {improves})"))
        check("retire or archive" in said.lower(),
              j.step(f"and says there is no such state ({said[:60]!r})"))

    # 20-24  a fresh explicit delete, approved: exactly one deletion.
    nova._delete_target["name"] = GAME
    approver = asyncio.create_task(answer(True))
    said, ids, improves = await _say(nova, f"Delete the project {GAME}?")
    await approver
    check(len(ids) == 1, j.step(f"the explicit delete asks once ({len(ids)})"))
    check(not (proj / "PROJECT.md").exists(),
          j.step("and on approval the project is gone from the listing"))
    trash_dir = nova.root / "projects" / ".trash"
    trash = [t.name for t in trash_dir.glob(f"{GAME}*")] if trash_dir.is_dir() else []
    check(bool(trash), j.step(f"recoverable in the trash ({trash})"))
    check(not improves, j.step("and no edit was ever started for it"))

    # 25-26  it stays gone across a restart of the memory layer.
    await nova.memory.cancel_pending_background_work()
    check(not (proj / "PROJECT.md").exists(),
          j.step("after boot recovery it is still deleted"))
    check(bool([t.name for t in trash_dir.glob(f"{GAME}*")]),
          j.step("and still in the trash"))

    check(j.n >= 20, f"the journey ran {j.n} checked transitions")
    return j.n


async def journey_eleven_current_vs_history(nova):
    check.section("journey 11: current versus historical progress")
    j = Journey()
    m = nova.memory

    goal = await m.create_goal(project_name=GAME, title="add a pause menu",
                               objective="pause menu",
                               success_criteria="it pauses")
    await m.enqueue_goal_task(goal_id=goal, project_name=GAME,
                              tool_name="code.write", args={})
    c = await m.claim_next_goal_task()
    gen0 = int(c["generation"])
    check(gen0 == 0, j.step(f"the goal starts on run {gen0}"))

    # 02-05  progress on run 0: a retry and a failure.
    await m.add_progress_event(goal_id=goal, project_name=GAME, kind="retry",
                               message="code.write failed, retrying (1/2)",
                               generation=gen0, task_id=str(c["task_id"]),
                               attempt=1)
    await m.complete_goal_task(task_id=str(c["task_id"]), status="failed",
                               result={}, error="the sprite sheet is missing",
                               expected_generation=gen0)
    await m.add_progress_event(goal_id=goal, project_name=GAME, kind="error",
                               message="code.write failed: sprite sheet",
                               generation=gen0, task_id=str(c["task_id"]))
    run0 = await m.list_progress_events(goal_id=str(goal), generation=gen0,
                                        limit=50)
    check(len(run0) == 2, j.step(f"run {gen0} has two events ({len(run0)})"))
    check(all(e.get("generation") == gen0 for e in run0),
          j.step("each stamped with the run that produced it"))
    check(all(e.get("task_id") for e in run0),
          j.step("and with the task that produced it"))
    check(any(e.get("attempt") == 1 for e in run0),
          j.step("the retry carries its attempt number"))

    # 06-08  cancel and resume: a NEW run.
    await m.cancel_goal(goal_id=goal)
    await m.resume_goal(goal_id=goal)
    row = await m.get_goal(goal_id=goal) or {}
    gen1 = int(row.get("generation"))
    check(gen1 > gen0, j.step(f"resuming opens run {gen1}"))
    await m.add_progress_event(goal_id=goal, project_name=GAME, kind="tool",
                               message="code.write completed",
                               generation=gen1)
    run1 = await m.list_progress_events(goal_id=str(goal), generation=gen1,
                                        limit=50)
    check(len(run1) == 1, j.step(f"run {gen1} has its own event ({len(run1)})"))
    check(all(e.get("generation") == gen1 for e in run1),
          j.step("stamped with the new run"))

    # 09-12  the DEFAULT read is the current run, and does not carry run 0.
    r = await nova.http.post("/chat", json={"message": "hello"})  # keep the app warm
    resp = await nova.http.get(f"/goals/{goal}/progress")
    body = resp.json() if resp.status_code == 200 else {}
    events = body.get("events", [])
    msgs = " ".join(str(e.get("message")) for e in events)
    check(resp.status_code == 200, j.step(f"the endpoint answers ({resp.status_code})"))
    check(int(body.get("current_generation")) == gen1,
          j.step(f"it reports the current run ({body.get('current_generation')})"))
    check("completed" in msgs,
          j.step(f"the current run's progress is there ({len(events)} events)"))
    check("retrying" not in msgs and "sprite sheet" not in msgs,
          j.step(f"and run {gen0}'s retry/failure are NOT presented as current "
                 f"({msgs[:70]!r})"))

    # 13-16  history is one query away, and says which run each came from.
    resp = await nova.http.get(f"/goals/{goal}/progress?history=true")
    body = resp.json() if resp.status_code == 200 else {}
    events = body.get("events", [])
    gens = sorted({e.get("generation") for e in events})
    msgs = " ".join(str(e.get("message")) for e in events)
    check(len(events) == 3, j.step(f"history has everything ({len(events)})"))
    check(gens == [gen0, gen1], j.step(f"across both runs ({gens})"))
    check("sprite sheet" in msgs and "completed" in msgs,
          j.step("old and new, both present"))
    check(body.get("history") is True,
          j.step("and the response says it is history"))

    # 17-19  a restart changes none of it, and repeated reads are non-destructive.
    await m.cancel_pending_background_work()
    again = (await nova.http.get(f"/goals/{goal}/progress?history=true")).json()
    once_more = (await nova.http.get(f"/goals/{goal}/progress?history=true")).json()
    # The restart adds its OWN progress event - it paused the goal - and that
    # event carries the run it interrupted rather than no provenance at all.
    ev_again = again.get("events", [])
    check(len(ev_again) == 4,
          j.step(f"after a restart the history is intact, plus the restart's "
                 f"own note ({len(ev_again)})"))
    check(len(once_more.get("events", [])) == len(ev_again),
          j.step("and reading it twice does not consume it"))
    paused = [e for e in ev_again if "restart" in str(e.get("message"))]
    check(len(paused) == 1 and paused[0].get("generation") == gen1,
          j.step(f"the restart's note is stamped with the run it interrupted "
                 f"({[e.get('generation') for e in paused]})"))
    old_still = [e for e in again.get("events", [])
                 if e.get("generation") == gen0]
    check(len(old_still) == 2,
          j.step(f"run {gen0}'s events are still run {gen0}'s ({len(old_still)})"))

    # 20-21  an event whose run is unknown is reported as unknown.
    await m._sqlite.add_progress_event(
        event_id=__import__("uuid").uuid4(), goal_id=goal, project_name=GAME,
        kind="tool", message="a legacy event from before provenance")
    legacy = [e for e in (await nova.http.get(
        f"/goals/{goal}/progress?history=true")).json().get("events", [])
        if e.get("generation") is None]
    check(len(legacy) == 1,
          j.step(f"a legacy event reads as unknown, not as current ({len(legacy)})"))
    check(str(legacy[0].get("message")).startswith("a legacy"),
          j.step("and is still readable"))

    check(j.n >= 15, f"the journey ran {j.n} checked transitions")
    return j.n


async def main() -> None:
    async with boot(default_reply="Sure.") as nova:
        # The decider asks to delete whatever the journey points it at; the
        # harness's own rule answers {"action":"respond"} and is tried first,
        # so this has to go in FRONT of it.
        nova._improves = []
        nova._delete_target = {"name": GAME}

        def decide(_p: str) -> str:
            return ('{"action":"tool","tool":"project.delete","args":{"name":"'
                    + nova._delete_target["name"] + '"}}')

        nova.llm.when("agent brain for Nova", decide, label="delete")
        nova.llm.rules.insert(0, nova.llm.rules.pop())

        pb = nova.runtime._project_builder
        real_improve = pb.improve

        async def spy_improve(*, slug, instructions, **kw):
            nova._improves.append((slug, instructions))
            return {"started": True, "slug": slug}
        pb.improve = spy_improve

        broker = nova.runtime._permission_broker
        real_await = broker.await_decision

        async def fast(rid, *, timeout_s=6.0):
            return await real_await(rid, timeout_s=6.0)
        broker.await_decision = fast

        n10 = await journey_ten_destructive_intent_boundary(nova)
        n11 = await journey_eleven_current_vs_history(nova)
        check.section("totals")
        check(n10 + n11 >= 35,
              f"journeys 10 and 11 ran {n10} + {n11} = {n10 + n11} "
              f"checked transitions")
    check.finish()


if __name__ == "__main__":
    run(main)
