"""Stage 15 — the permission handoff knows WHAT it is asking about.

Two destructive requests can be in flight at once. Everything downstream of the
broker then depends on one question: can the thing that answers tell them apart?

`pending()` used to return `[{"request_id": "6f17d791dadd"}]`. Nothing else. A
person cannot approve an id, and the one production consumer --
`GET /permissions/audit`, which ships it straight to a client -- was handing out
a list of live destructive actions with no indication of what any of them would
destroy. The information existed in the audit trail, so nothing was lost; it was
being dropped at the layer whose entire job is to answer "what is waiting for
you?", leaving the consumer to rebuild it by matching ids. That is §19's case:
one layer drops provenance and the next one guesses.

Found by a Stage 15 probe that tried to approve one of two concurrent deletions
and could not work out which request was which.

Invariants exercised:
  I16  one explicit destructive request creates at most one execution path
  I28  project A state never modifies project B
  I34  destructive actions require the correct permission AND target
  I13  permissions terminate according to their authoritative lifecycle

Run:  venv\\Scripts\\python.exe tests\\test_s15_permission_targeting.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from s15_bus import Recorder  # noqa: E402

check = Checks()


def seed(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


def script_delete_from_user_line(nova) -> None:
    """Ask to delete whatever THIS turn's user message names.

    Deliberately keyed on the `User message:` line rather than on the whole
    prompt. The decider prompt carries a Context blob listing every project, so
    a naive substring match finds every project name in every turn -- which is
    how the first version of this test had both turns aiming at the same
    target, and looked exactly like a cross-conversation leak until the prompt
    was actually read.
    """
    def decide(prompt: str) -> str:
        line = ""
        for raw in prompt.splitlines():
            if raw.strip().startswith("User message:"):
                line = raw
                break
        name = ""
        for word in line.replace(".", " ").split():
            if word.startswith("proj-"):
                name = word
                break
        return ('{"action":"tool","tool":"project.delete","args":{"name":"'
                + name + '"}}')

    nova.llm.when("agent brain for Nova", decide, label="delete")
    nova.llm.rules.insert(0, nova.llm.rules.pop())


def shorten(nova, seconds: float) -> None:
    broker = nova.runtime._permission_broker
    real = broker.await_decision

    async def fast(request_id, *, timeout_s=seconds):
        return await real(request_id, timeout_s=seconds)
    broker.await_decision = fast


async def test_pending_says_what_it_would_do():
    check.section("§19 a pending request names its capability and target")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "proj-solo")
        script_delete_from_user_line(nova)
        shorten(nova, 3.0)
        broker = nova.runtime._permission_broker

        seen: list[dict] = []

        async def look():
            for _ in range(300):
                await asyncio.sleep(0.02)
                rows = broker.pending()
                if rows:
                    seen.extend(rows)
                    broker.resolve(str(rows[0]["request_id"]), False)
                    return

        await asyncio.gather(
            nova.brain.chat("Delete the project proj-solo",
                            conversation_id=str(uuid4())),
            look())

        check(len(seen) == 1, f"one request was pending ({len(seen)})")
        row = seen[0] if seen else {}
        check(str(row.get("capability")) == "project.delete",
              f"it names the capability ({row.get('capability')!r})")
        check(str((row.get("details") or {}).get("project")) == "proj-solo",
              f"and the project it would delete ({row.get('details')!r})")
        check(str(row.get("tier")) == "admin",
              f"and the tier it costs ({row.get('tier')!r})")
        check(bool(row.get("requested_at")),
              f"and when it was asked ({row.get('requested_at')!r})")

        check(broker.pending() == [],
              f"and once answered it is no longer waiting ({broker.pending()})")


async def test_two_live_requests_are_told_apart_and_only_one_runs():
    check.section("I16/I28/I34 two deletions in flight, one approved")
    async with boot(default_reply="Sure.") as nova:
        keep = seed(nova, "proj-keep")
        goes = seed(nova, "proj-goes")
        script_delete_from_user_line(nova)
        shorten(nova, 6.0)
        broker = nova.runtime._permission_broker

        picked: dict[str, str] = {}

        async def approve_only_the_doomed_one():
            """Wait for BOTH, then approve the one aimed at proj-goes."""
            rows: dict[str, dict] = {}
            for _ in range(500):
                await asyncio.sleep(0.02)
                for row in broker.pending():
                    rows[str(row["request_id"])] = row
                if len(rows) >= 2:
                    break
            targets = {rid: str((r.get("details") or {}).get("project") or "")
                       for rid, r in rows.items()}
            picked.update(targets)
            doomed = [rid for rid, t in targets.items() if t == "proj-goes"]
            if doomed:
                broker.resolve(doomed[0], True)
            # The other is left to time out on its own, which is the point:
            # answering one question is not answering both.

        with Recorder() as rec:
            await asyncio.gather(
                nova.brain.chat("Delete the project proj-goes",
                                conversation_id=str(uuid4())),
                nova.brain.chat("Delete the project proj-keep",
                                conversation_id=str(uuid4())),
                approve_only_the_doomed_one())
            asked = rec.of("permission.requested")
            expired_outcomes = [str(e.data.get("outcome"))
                                for e in rec.of("permission.expired")]
        check(len(asked) == 2, f"two requests were raised ({len(asked)})")
        check(sorted(picked.values()) == ["proj-goes", "proj-keep"],
              f"and the two live requests named DIFFERENT targets ({picked})")

        check(not (goes / "PROJECT.md").exists(),
              "the approved project is gone")
        check((keep / "PROJECT.md").exists(),
              "and the one nobody approved is still on disk")

        check(expired_outcomes == ["timeout"],
              f"exactly one request timed out, and it ended as a timeout "
              f"({expired_outcomes})")
        check(broker.pending() == [],
              f"nothing is left waiting ({broker.pending()})")


async def test_a_settled_request_stops_advertising_a_target():
    check.section("I13 a settled request is not still 'waiting to delete X'")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "proj-gone")
        script_delete_from_user_line(nova)
        shorten(nova, 3.0)
        broker = nova.runtime._permission_broker

        ids: list[str] = []

        async def reject_it():
            for _ in range(300):
                await asyncio.sleep(0.02)
                rows = broker.pending()
                if rows:
                    ids.append(str(rows[0]["request_id"]))
                    broker.resolve(ids[0], False)
                    return

        await asyncio.gather(
            nova.brain.chat("Delete the project proj-gone",
                            conversation_id=str(uuid4())),
            reject_it())

        check(bool(ids), "the request existed")
        check(broker.pending() == [],
              f"and is no longer listed as waiting ({broker.pending()})")
        check(broker.settled_as(ids[0]) == "rejected",
              f"the refusal is what stands ({broker.settled_as(ids[0])!r})")
        # The audit keeps it; `pending` does not. Those are different questions.
        trail = [e for e in broker.audit_log(limit=50)
                 if str(e.get("request_id")) == ids[0]]
        check(any(str(e.get("capability")) == "project.delete" for e in trail),
              "the audit still knows what it was about")


async def main() -> None:
    await test_pending_says_what_it_would_do()
    await test_two_live_requests_are_told_apart_and_only_one_runs()
    await test_a_settled_request_stops_advertising_a_target()
    check.finish()


if __name__ == "__main__":
    run(main)
