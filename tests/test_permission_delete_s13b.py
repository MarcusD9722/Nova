"""A destructive action asks once, and says what really happened (Stage 13B).

JOURNEY 7. Reproduced from a live session against the real frontend:

    user: "Can you delete the project with-you?"
    permission.requested  capability=project.delete tier=admin recoverable=true
    (no approval surface existed, so nothing could answer it)
    ~120s later: permission.expired outcome=timeout
    tool.error: "You didn't approve it (declined or timed out) - nothing was touched."
    then a SECOND permission.requested, with no new user request
    ... and Nova answered "I'll take with-you off the list"

Four separate defects in one turn, all reproduced on 55c485b before any fix:

  D1  nothing in the frontend consumed `permission.requested`, so an
      admin-tier capability asked for consent that was impossible to give.
  D2  ONE user request produced TWO permission requests (ids 7debd9d5796f then
      78bc4e077809, both timing out). `ToolLoopExecutor.run` allows a second
      automatic attempt, and the model re-emits the same call once the refusal
      appears among its observations. Asking a human again, unprompted, is not
      a retry - it is nagging for consent that was already withheld, and the
      second prompt is the one most likely to be approved by accident.
  D3  Nova reported the deletion as done when the authoritative outcome was not
      approved, not executed, nothing touched. The observation reaching the
      answer step was already truthful and already labelled "trust these over
      your own knowledge"; the model said otherwise regardless. So the outcome
      is now composed deterministically from the tool's own payload rather than
      asked for politely.
  D4  a re-phrased delete ("Please delete with-you, I mean it.") never reached
      the delete tool at all. The project pre-pass treats any authorised
      mutation as an IMPROVE instruction, so it started an autonomous code edit
      of with-you and answered "working on those improvements to with-you now"
      - a real write to disk, the permission gate never reached, and the delete
      never done.

WHAT IS ASSERTED. `permission.requested` counted by request_id from the bus,
the assistant text from the HTTP response to the request that asked, and the
project directory on disk. Never ordering, never timing.

ONE booted backend serves all five scenarios, each with its own project. Five
boots plus two full 120s approval windows took ten minutes, which the gate's
180s per-suite watchdog would kill outright - and a suite that cannot survive
the gate proves nothing in it.

Run:  venv\\Scripts\\python.exe tests\\test_permission_delete_s13b.py
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

CLAIMS = ("deleted", "removed", "retired", "off the list", "gone",
          "moved to trash", "i'll take")


class Bus:
    """Permission and tool events, attributed by the request_id on the event."""

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

    def drain(self) -> list:
        while True:
            try:
                self.events.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                return self.events

    def of(self, kind: str) -> list:
        self.drain()
        return [e for e in self.events if e.type == kind]

    def request_ids(self) -> list[str]:
        return [str(e.data.get("request_id"))
                for e in self.of("permission.requested")]


def _seed_project(nova, name: str) -> Path:
    d = nova.root / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "PROJECT.md").write_text(f"# {name}\n\n## Brief\nA thing.\n",
                                  encoding="utf-8")
    return d


def _target(nova, name: str) -> None:
    """Which project the scripted decider asks to delete next."""
    nova._delete_target["name"] = name


def _script_delete(nova) -> None:
    """Make the decider ask to delete, ahead of the harness's own rule.

    The harness already answers the tool decider with {"action":"respond"};
    rules are tried in order, so this one has to go in FRONT of it.
    """
    nova._delete_target = {"name": "with-you"}

    def decide(_prompt: str) -> str:
        name = nova._delete_target["name"]
        return ('{"action":"tool","tool":"project.delete","args":{"name":"'
                + name + '"}}')

    nova.llm.when("agent brain for Nova", decide, label="delete")
    nova.llm.rules.insert(0, nova.llm.rules.pop())


def _shorten_window(nova, seconds: float) -> None:
    """The real approval window is 120s. Tests may not wait for it."""
    broker = nova.runtime._permission_broker
    real = broker.await_decision

    async def fast(request_id, *, timeout_s=seconds):
        return await real(request_id, timeout_s=seconds)
    broker.await_decision = fast


def _said(r) -> str:
    return str((r.json() if r.status_code == 200 else {}).get("assistant") or "")


async def _answer_when_asked(broker, approved: bool) -> None:
    for _ in range(200):
        await asyncio.sleep(0.02)
        pend = broker.pending()
        if pend:
            broker.resolve(str(pend[0]["request_id"]), approved)
            return


async def test_a_timeout_asks_once_and_says_it_did_not_happen(nova):
    check.section("delete: a timeout asks ONCE and admits nothing happened")

    proj = _seed_project(nova, "timeout-case")
    _target(nova, "timeout-case")

    with Bus() as bus:
        r = await nova.http.post(
            "/chat", json={"message": "Can you delete the project timeout-case?"})
        ids = bus.request_ids()
        expired = [str(e.data.get("outcome")) for e in bus.of("permission.expired")]

    said = _said(r)
    check(len(ids) == 1,
          f"exactly ONE permission request for one ask ({len(ids)}: {ids})")
    check(expired == ["timeout"], f"and it ended as a timeout ({expired})")
    check((proj / "PROJECT.md").exists(),
          "the project is still on disk, untouched")
    claimed = [w for w in CLAIMS if w in said.lower()]
    check(not claimed,
          f"Nova does not claim it happened ({claimed}) - {said[:110]!r}")
    check("didn't delete" in said.lower() or "did not delete" in said.lower(),
          f"it says plainly that it did not delete it ({said[:110]!r})")
    check("timed out" in said.lower(), f"and why ({said[:110]!r})")


async def test_a_denial_asks_once_and_says_it_did_not_happen(nova):
    check.section("delete: a denial asks ONCE and admits nothing happened")

    proj = _seed_project(nova, "denied-case")
    _target(nova, "denied-case")
    broker = nova.runtime._permission_broker

    with Bus() as bus:
        denier = asyncio.create_task(_answer_when_asked(broker, False))
        r = await nova.http.post(
            "/chat", json={"message": "Can you delete the project denied-case?"})
        await denier
        ids = bus.request_ids()

    said = _said(r)
    check(len(ids) == 1, f"exactly ONE permission request ({len(ids)}: {ids})")
    check((proj / "PROJECT.md").exists(), "the project is still on disk")
    claimed = [w for w in CLAIMS if w in said.lower()]
    check(not claimed,
          f"Nova does not claim it happened ({claimed}) - {said[:110]!r}")
    check("said no" in said.lower(),
          f"it says the answer was no ({said[:110]!r})")


async def test_an_approval_deletes_exactly_once_and_says_so(nova):
    """COUNTER-TEST. The fence must not block the action the user approved."""
    check.section("delete: approval executes exactly once, and only then")

    proj = _seed_project(nova, "approved-case")
    _target(nova, "approved-case")
    broker = nova.runtime._permission_broker

    with Bus() as bus:
        approver = asyncio.create_task(_answer_when_asked(broker, True))
        r = await nova.http.post(
            "/chat", json={"message": "Can you delete the project approved-case?"})
        await approver
        ids = bus.request_ids()

    check(len(ids) == 1, f"one request, not two ({len(ids)}: {ids})")
    check(not (proj / "PROJECT.md").exists(),
          f"the project is gone from the active listing ({proj.exists()})")
    trash_dir = nova.root / "projects" / ".trash"
    trash = [t.name for t in trash_dir.glob("approved-case*")] \
        if trash_dir.is_dir() else []
    check(bool(trash), f"and recoverable in the trash ({trash})")
    said = _said(r)
    check("didn't delete" not in said.lower(),
          f"Nova does not deny an action it performed ({said[:110]!r})")


async def test_a_late_answer_changes_nothing(nova):
    check.section("delete: an answer that arrives too late does nothing")

    proj = _seed_project(nova, "late-case")
    _target(nova, "late-case")

    with Bus() as bus:
        await nova.http.post(
            "/chat", json={"message": "Can you delete the project late-case?"})
        ids = bus.request_ids()

    check(len(ids) == 1, f"one request ({ids})")
    rid = ids[0]

    # The click lands after the window closed. The endpoint is the surface a UI
    # actually calls, so it is what gets tested.
    res = await nova.http.post("/permissions/resolve",
                               json={"request_id": rid, "approved": True})
    body = res.json() if res.status_code == 200 else {}
    check(res.status_code == 200, f"the endpoint answers ({res.status_code})")
    check(body.get("applied") is False,
          f"and says the click applied NOTHING ({body})")
    check(body.get("resolved") is False,
          f"the request was not resolved by it ({body.get('resolved')})")
    check((proj / "PROJECT.md").exists(),
          "the project is still there after the late approval")

    again = await nova.http.post("/permissions/resolve",
                                 json={"request_id": rid, "approved": True})
    check((again.json() or {}).get("applied") is False,
          "and a repeat of it is inert too")
    check((proj / "PROJECT.md").exists(), "the project survives that as well")


async def test_a_new_user_turn_may_ask_again(nova):
    """The fence is for the TURN, not the session."""
    check.section("delete: a fresh request from the user is allowed to ask")

    proj = _seed_project(nova, "again-case")
    _target(nova, "again-case")

    with Bus() as bus:
        await nova.http.post(
            "/chat", json={"message": "Can you delete the project again-case?"})
        first = len(bus.request_ids())
        # A DIFFERENT phrasing, and an imperative rather than a question. This
        # one used to be swallowed by the project pre-pass and turned into an
        # autonomous code edit; it has to reach the gated tool like any other
        # delete.
        await nova.http.post(
            "/chat", json={"message": "Please delete again-case, I mean it."})
        total = len(bus.request_ids())

    check(first == 1, f"the first turn asked once ({first})")
    check(total == 2,
          f"and the second turn is allowed its own request ({total})")
    check((proj / "PROJECT.md").exists(),
          "and neither turn deleted anything without approval")


async def test_an_imperative_delete_is_not_an_edit(nova):
    """D4. A delete instruction must not become an improve.

    `authorize_project_mutation` says this sentence is an affirmative
    instruction, which is true, and the pre-pass then treated EVERY authorised
    mutation as an instruction to edit. So the sentence that most clearly asks
    for a project to stop existing started rewriting its code instead.
    """
    check.section("delete: an imperative delete does not become an edit")

    proj = _seed_project(nova, "edit-case")
    _target(nova, "edit-case")

    with Bus() as bus:
        r = await nova.http.post(
            "/chat", json={"message": "Please delete edit-case, I mean it."})
        ids = bus.request_ids()

    said = _said(r)
    check(len(ids) == 1,
          f"it reaches the gated delete tool ({len(ids)}: {ids})")
    check("improvement" not in said.lower() and "improving" not in said.lower(),
          f"and Nova does not announce improvements ({said[:120]!r})")
    check((proj / "PROJECT.md").exists(),
          "the project is untouched while approval is outstanding")

    # COUNTER: removing a FEATURE is still ordinary edit work, and must not be
    # mistaken for removing the project.
    from core.project_intent import requests_project_removal
    check(requests_project_removal("remove the pause button from edit-case",
                                   slug="edit-case") is False,
          "removing a feature inside a project is not removing the project")
    check(requests_project_removal("delete edit-case", slug="edit-case") is True,
          "and removing the project itself still is")


async def main():
    async with boot(default_reply="Sure.") as nova:
        _script_delete(nova)
        _shorten_window(nova, 6.0)
        await test_a_timeout_asks_once_and_says_it_did_not_happen(nova)
        await test_a_denial_asks_once_and_says_it_did_not_happen(nova)
        await test_an_approval_deletes_exactly_once_and_says_so(nova)
        await test_a_late_answer_changes_nothing(nova)
        await test_a_new_user_turn_may_ask_again(nova)
        await test_an_imperative_delete_is_not_an_edit(nova)
    check.finish()


if __name__ == "__main__":
    run(main)
