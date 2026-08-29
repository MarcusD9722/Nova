"""Asked after a restart, Nova answers from the record (Stage 13C §12/§15).

TWO THINGS ARE PROVED HERE.

RECONSTRUCTION. A durable store is built in one process; a SECOND process boots
the whole backend against it and is asked, over real `POST /chat`, what
happened. The transcript in that process is empty - it has never spoken to
anyone - so any correct answer can only have come from the durable record.

The expected facts are built FIRST from authoritative rows, then compared
against what the answer step was actually given. With a scripted model the
reply text would only test the script; what a real model receives is what
decides whether it can answer at all.

CHECKPOINT DURABILITY (§15, and crash window 6). A dev-mode proposal is the
closest thing Nova has to a checkpoint: it is durable, it names a file, and it
carries the digest of what that file looked like when the plan was made. The
question is whether that protection survives process death - a checkpoint that
forgets what it was planned against is worse than none, because it looks valid.

Run:  venv\\Scripts\\python.exe tests\\test_reconstruction_s13c.py
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
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "1800")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

P = "flappy-bird"
MARKER = "the sprite sheet is missing"


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


#: Build a world in process 1: one success, one failure with a real error, one
#: still-queued step, and a separate cancelled goal.
SEED = f"""
    goal = await mem.create_goal(project_name="{P}", title="add a pause menu",
                                 objective="pause menu", success_criteria="it pauses")
    for tool in ("code.write", "code.test", "code.ship"):
        await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                    tool_name=tool, args={{}})
    a = await mem.claim_next_goal_task()
    await mem.complete_goal_task(task_id=str(a["task_id"]), status="done",
                                 result={{"ok": True}}, error="",
                                 expected_generation=int(a["generation"]))
    b = await mem.claim_next_goal_task()
    await mem.complete_goal_task(task_id=str(b["task_id"]), status="failed",
                                 result={{}}, error="{MARKER}",
                                 expected_generation=int(b["generation"]))
    killed = await mem.create_goal(project_name="{P}", title="add sound",
                                   objective="sound", success_criteria="beeps")
    await mem.enqueue_goal_task(goal_id=killed, project_name="{P}",
                                tool_name="code.write", args={{}})
    await mem.cancel_goal(goal_id=killed)
    emit({{"goal": str(goal), "killed": str(killed),
           "done": str(a["task_id"]), "failed": str(b["task_id"])}})
"""

#: In process 2, ask over real HTTP and return the grounding the ANSWER step
#: was given. Selected by CONTENT, never by position in the prompt list.
ASK = '''
    async def ask(message):
        before = len(nova.llm.prompts)
        r = await nova.http.post("/chat", json={"message": message})
        new = nova.llm.prompts[before:]
        answers = [p for p in new
                   if "You are Nova" in p and "agent brain for Nova" not in p]
        return answers[-1] if answers else ""
'''


async def test_a_fresh_process_answers_from_the_record():
    check.section("§12 a second process, an empty transcript, real questions")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, SEED)
        gid = one(made, "goal")

        # Everything the ANSWER should contain, derived from rows first.
        facts = run_step(root, f"""
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            emit({{"expect_failed_error": "{MARKER}",
                   "expect_goal_title": g["title"],
                   "expect_pending_tool": [t["tool_name"] for t in rows
                                           if t["status"] == "queued"],
                   "expect_generation": g["generation"]}})
        """)
        pending_tool = (one(facts, "expect_pending_tool") or ["code.ship"])[0]

        asked = run_step(root, ASK + '''
    await mem.cancel_pending_background_work()
    out = {}
    for q in ("What happened?", "What failed?", "What is still pending?",
              "What was cancelled?", "What can resume?",
              "What should happen next?"):
        out[q] = await ask(q)
    emit({"grounding": out,
          "transcript_was_empty": True})
''', full=True)
        ground = one(asked, "grounding") or {}

        check(MARKER in ground.get("What failed?", ""),
              f"'What failed?' is given the real error "
              f"({MARKER in ground.get('What failed?', '')})")
        check("add a pause menu" in ground.get("What happened?", ""),
              "'What happened?' is given the goal it belongs to")
        check(pending_tool in ground.get("What is still pending?", ""),
              f"'What is still pending?' names the queued step ({pending_tool})")
        check("cancelled" in ground.get("What was cancelled?", ""),
              "'What was cancelled?' is given a cancelled goal")
        check("revision" in ground.get("What can resume?", ""),
              "'What can resume?' is given which revision each goal is on")
        unlabelled = [q for q, v in ground.items()
                      if "from the record" not in v]
        check(len(ground) == 6 and not unlabelled,
              f"every one of the six answers is labelled as the record, not "
              f"recollection ({unlabelled or len(ground)})")

        # The decisive one: this process has never spoken to anyone.
        check(one(asked, "transcript_was_empty") is True,
              "the answering process had no prior conversation to draw on")


async def test_b_a_misleading_premise_is_corrected_from_the_record():
    check.section("§12 'everything finished, right?' when something failed")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, SEED)
        asked = run_step(root, ASK + '''
    await mem.cancel_pending_background_work()
    g = await ask("So everything finished successfully, right?")
    emit({"grounding": g})
''', full=True)
        ground = one(asked, "grounding") or ""
        check(MARKER in ground,
              f"the failure is in front of the model ({MARKER in ground})")
        check("failed" in ground,
              "so the premise can be corrected rather than accepted")
        check("from the record" in ground,
              "and it is the durable record that contradicts it")


async def test_b2_asking_whether_anything_is_still_running():
    """§12. The natural question after a restart, and the one whose only
    honest source is the record: the transcript is empty, so a model asked
    "is anything still running?" with nothing attached answers from nothing."""
    check.section("§12 'is anything still running?' after the process died")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, SEED)
        asked = run_step(root, ASK + '''
    await mem.cancel_pending_background_work()
    out = {}
    for q in ("Is anything still running?",
              # Each of the next two is caught by ONE branch only. Without
              # them the branches cover for each other and either could be
              # deleted with every assertion still green.
              "Are the tasks still running?",     # needs the subject branch
              "Anything still running?",          # needs the bare branch
              "Are you still working on the pause menu?",
              "What's going on with my work?",
              "Is anything in progress?"):
        out[q] = await ask(q)
    emit({"grounding": out})
''', full=True)
        ground = one(asked, "grounding") or {}
        for q, g in sorted(ground.items()):
            check("The work you are actually tracking" in g,
                  f"{q!r} is answered from the record")
        # And the failure is in front of it, not just any state.
        check(all(MARKER in g for g in ground.values()),
              "each of them can see the step that failed")


async def test_b3_ordinary_talk_is_left_alone():
    """COUNTER-TEST. Attaching state to every turn would be its own defect:
    the record is only attached when the turn is genuinely about the work."""
    check.section("§12 turns that are not about the work stay untouched")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, SEED)
        asked = run_step(root, ASK + '''
    await mem.cancel_pending_background_work()
    out = {}
    for q in ("Is the tap still running?",
              "Are you working on Sunday?",
              "What's going on with your day?",
              "Everything is fine, thanks.",
              "I finished my coffee."):
        out[q] = await ask(q)
    emit({"grounding": out})
''', full=True)
        ground = one(asked, "grounding") or {}
        for q, g in sorted(ground.items()):
            check("The work you are actually tracking" not in g,
                  f"{q!r} is left alone")


async def test_c_a_checkpoint_survives_and_still_knows_its_baseline():
    check.section("§15/W6 a checkpoint, a crash, and the file it was planned against")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            from core import dev_mode as dm
            outside = Path(r"{root}") / "outside" / "proj"
            outside.mkdir(parents=True, exist_ok=True)
            (outside / "game.py").write_text("VERSION = 'A'\\nSPEED = 1\\n",
                                             encoding="utf-8")
            d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                           projects_dir=Path(r"{root}") / "repo" / "projects")
            d.register_external_root("proj", str(outside))
            p = d.propose_change("game.py", "VERSION = 'A'\\nSPEED = 2\\n",
                                 reason="bump speed", project="proj")
            emit({{"proposal": p.id, "base_sha": p.base_sha}})
        """)
        pid = one(made, "proposal")
        check(bool(one(made, "base_sha")),
              f"the checkpoint records its baseline ({str(one(made,'base_sha'))[:12]}...)")

        run_step(root, "CRASH()", expect_crash=True)

        # The file moves on while Nova is dead.
        target = root / "outside" / "proj" / "game.py"
        target.write_text("VERSION = 'B'\nSPEED = 1\nNEW_FEATURE = True\n",
                          encoding="utf-8")

        after = run_step(root, f"""
            from core import dev_mode as dm
            d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                           projects_dir=Path(r"{root}") / "repo" / "projects")
            d.register_external_root("proj", str(Path(r"{root}") / "outside" / "proj"))
            found = [x for x in d.list_proposals()] if hasattr(d, "list_proposals") else []
            try:
                d.apply_proposal("{pid}", confirm=True)
                emit({{"applied": True, "why": ""}})
            except Exception as e:
                emit({{"applied": False, "why": str(e)[:160]}})
        """)
        content = target.read_text(encoding="utf-8")
        check(one(after, "applied") is False,
              f"a fresh process refuses the stale checkpoint "
              f"({one(after, 'why')[:60]!r})")
        check("has changed since" in str(one(after, "why")),
              "because the file moved on while Nova was gone")
        check("NEW_FEATURE" in content and "VERSION = 'B'" in content,
              f"and the newer content is intact ({content!r})")
        check("SPEED = 2" not in content,
              "the stale plan was not executed against stale assumptions")


async def test_d_an_undisturbed_checkpoint_still_applies_after_a_restart():
    """COUNTER-TEST. Surviving must not mean refusing everything."""
    check.section("§15 an unchanged file still takes the change after a restart")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            from core import dev_mode as dm
            outside = Path(r"{root}") / "outside" / "proj"
            outside.mkdir(parents=True, exist_ok=True)
            (outside / "game.py").write_text("VERSION = 'A'\\nSPEED = 1\\n",
                                             encoding="utf-8")
            d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                           projects_dir=Path(r"{root}") / "repo" / "projects")
            d.register_external_root("proj", str(outside))
            p = d.propose_change("game.py", "VERSION = 'A'\\nSPEED = 2\\n",
                                 reason="bump speed", project="proj")
            emit({{"proposal": p.id}})
        """)
        pid = one(made, "proposal")
        run_step(root, "CRASH()", expect_crash=True)

        after = run_step(root, f"""
            from core import dev_mode as dm
            d = dm.DevMode(repo_root=Path(r"{root}") / "repo",
                           projects_dir=Path(r"{root}") / "repo" / "projects")
            d.register_external_root("proj", str(Path(r"{root}") / "outside" / "proj"))
            try:
                out = d.apply_proposal("{pid}", confirm=True)
                emit({{"applied": True, "status": str(out.get("status"))}})
            except Exception as e:
                emit({{"applied": False, "why": str(e)[:160]}})
        """)
        content = (root / "outside" / "proj" / "game.py").read_text(encoding="utf-8")
        check(one(after, "applied") is True,
              f"the checkpoint survives and applies ({one(after, 'why') if not one(after,'applied') else 'ok'})")
        check("SPEED = 2" in content,
              f"carrying its intended change ({content!r})")


async def main() -> None:
    await test_a_fresh_process_answers_from_the_record()
    await test_b_a_misleading_premise_is_corrected_from_the_record()
    await test_b2_asking_whether_anything_is_still_running()
    await test_b3_ordinary_talk_is_left_alone()
    await test_c_a_checkpoint_survives_and_still_knows_its_baseline()
    await test_d_an_undisturbed_checkpoint_still_applies_after_a_restart()
    check.finish()


if __name__ == "__main__":
    run(main)
