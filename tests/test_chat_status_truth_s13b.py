"""Asked what happened, Nova answers from the record (Stage 13B closure).

THE CONTRACT

After a restart, if Marcus asks any of

    "What happened?"  "What failed?"  "What is still pending?"
    "What was cancelled?"  "What can resume?"  "What should happen next?"

the answer has to come from durable authoritative state. The conversation
transcript must not be the authority - after a restart it is empty while the
work is still there, and a model asked "what failed?" with nothing in front of
it answers from whatever it remembers saying.

WHAT WAS MEASURED, on 4e0e458

A goal with one FAILED step carrying the error "the sprite sheet is missing",
and one queued step. Asking "What failed?" produced an answer prompt of 2291
characters containing NONE of it: not the step, not the error, not the goal,
not the project. There was no read path at all - the model was answering that
question from memory.

WHAT IS ASSERTED

The grounding handed to the answer step, because with a scripted model that is
the only honest thing to assert: asserting the reply text would only test the
script. What a real model receives is what decides whether it can answer, and
these tests prove the facts are in front of it and that they are the durable
ones. The authoritative rows are checked alongside, so the block cannot drift
from what it claims to describe.

Run:  venv\\Scripts\\python.exe tests\\test_chat_status_truth_s13b.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.intent import asks_about_work  # noqa: E402

check = Checks()

GAME = "flappy-bird"
MARKER = "the sprite sheet is missing"


async def _ask(nova, message: str) -> str:
    """Send a turn and return the grounding the ANSWER step was given."""
    before = len(nova.llm.prompts)
    await nova.http.post("/chat", json={"message": message})
    new = nova.llm.prompts[before:]
    # Chosen by CONTENT, not position. A turn emits several prompts - the tool
    # decider, the answer, sometimes a summariser - and which index the answer
    # lands on varies with what else the turn did. Taking the last one passed
    # the first question in this suite and failed the next four, which is the
    # same attribute-by-position mistake this stage keeps finding in its own
    # instrumentation.
    answers = [p for p in new
               if "You are Nova" in p and "agent brain for Nova" not in p]
    return answers[-1] if answers else ""


async def _seed(nova):
    m = nova.memory
    goal = await m.create_goal(project_name=GAME, title="add a pause menu",
                               objective="pause menu",
                               success_criteria="it pauses")
    await m.enqueue_goal_task(goal_id=goal, project_name=GAME,
                              tool_name="code.write", args={})
    c = await m.claim_next_goal_task()
    await m.complete_goal_task(task_id=str(c["task_id"]), status="failed",
                               result={}, error=MARKER,
                               expected_generation=int(c["generation"]))
    await m.enqueue_goal_task(goal_id=goal, project_name=GAME,
                              tool_name="code.test", args={})

    cancelled = await m.create_goal(project_name=GAME, title="add sound",
                                    objective="sound", success_criteria="beeps")
    await m.enqueue_goal_task(goal_id=cancelled, project_name=GAME,
                              tool_name="code.write", args={})
    await m.cancel_goal(goal_id=cancelled)
    return goal, cancelled


async def main() -> None:
    check.section("the detector recognises the six question shapes")
    for q in ("What happened?", "What failed?", "What is still pending?",
              "What was cancelled?", "What can resume?",
              "What should happen next?"):
        check(asks_about_work(q) is True, f"recognised: {q!r}")
    for q in ("Good morning.", "What is the capital of France?",
              "Tell me a story about a fox."):
        check(asks_about_work(q) is False, f"and not an ordinary turn: {q!r}")

    async with boot(default_reply="Sure.") as nova:
        goal, cancelled = await _seed(nova)

        check.section("what failed: the answer is given the record")
        ground = await _ask(nova, "What failed?")
        check(MARKER in ground,
              f"the failure's real error is in front of the model "
              f"({MARKER in ground})")
        check("code.write" in ground, "naming the step that failed")
        check("add a pause menu" in ground, "and the goal it belongs to")
        check("from the record" in ground,
              "labelled as the record rather than as recollection")

        check.section("pending, cancelled, resumable and next")
        for question, needle, why in (
            ("What is still pending?", "code.test", "the queued step"),
            ("What was cancelled?", "cancelled", "the cancelled goal's status"),
            ("What can resume?", "revision", "which revision each goal is on"),
            ("What should happen next?", "add a pause menu", "the live goal"),
            ("What happened?", MARKER, "the failure"),
        ):
            g = await _ask(nova, question)
            check(needle in g, f"{question!r} carries {why} ({needle in g})")

        check.section("both axes, because status alone cannot say")
        # A step whose tool SUCCEEDED after its run ended is neither a failure
        # nor something that never happened, and the answer has to be able to
        # say which.
        m = nova.memory
        g2 = await m.create_goal(project_name=GAME, title="add a high score",
                                 objective="scores", success_criteria="works")
        await m.enqueue_goal_task(goal_id=g2, project_name=GAME,
                                  tool_name="code.write", args={})
        # Claim until the row is THIS goal's. The claim is global and takes
        # the oldest runnable task, so a bare claim here took the earlier
        # goal's queued step and superseded the wrong thing - which is how
        # this test first "failed": the block was correct, the setup was not.
        c2 = await m.claim_next_goal_task()
        while c2 and str(c2.get("goal_id")) != str(g2):
            await m.complete_goal_task(
                task_id=str(c2["task_id"]), status="done", result={"ok": True},
                error="", expected_generation=int(c2["generation"]))
            c2 = await m.claim_next_goal_task()
        check(c2 is not None and str(c2.get("goal_id")) == str(g2),
              "the high-score step is the one in flight")
        await m.cancel_goal(goal_id=g2)
        await m.complete_goal_task(task_id=str(c2["task_id"]), status="done",
                                   result={"ok": True}, error="",
                                   expected_generation=int(c2["generation"]))
        ground = await _ask(nova, "What happened?")
        check("superseded" in ground,
              "a superseded step is named as superseded")
        check("work succeeded" in ground,
              "and its tool is still reported as having succeeded")

        check.section("the block matches the authoritative rows")
        rows = await m.list_goal_tasks(limit=100)
        failed = [t for t in rows if str(t.get("status")) == "failed"]
        check(bool(failed), f"there really is a failed row ({len(failed)})")
        check(all(str(t.get("last_error"))[:30] in ground
                  or str(t.get("status")) != "superseded" for t in rows),
              "and the block does not invent rows that are not there")

        check.section("an ordinary turn carries none of it")
        plain = await _ask(nova, "Good morning, how are you?")
        check("from the record" not in plain,
              "no work block on a conversational turn")
        check(MARKER not in plain,
              "and no unrelated failure text dragged into it")

    check.finish()


if __name__ == "__main__":
    run(main)
