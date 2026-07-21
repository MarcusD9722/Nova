"""Phase 8 / #2: autonomous skill learning — detect (offer), store, parameterize,
version, branch. Detection is deterministic; learning is always a proposal."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skills import detect_repeated_workflow, render_steps, workflow_parameters
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── Detection: a 3x-repeated 3-step workflow amid noise ──
    wf = ["open_downloads", "rename_file", "move_to_invoices"]
    activity = ["boot"] + wf + ["email"] + wf + ["chat", "browse"] + wf + ["shutdown"]
    found = detect_repeated_workflow(activity, min_repeats=3)
    check(found is not None and found["steps"] == wf, f"detects the repeated 3-step workflow (got {found})")
    check(found["occurrences"] == 3, "counts the repetitions")

    # prefers the LONGEST repeating pattern, not just a common pair
    a2 = wf * 3
    check(len(detect_repeated_workflow(a2, min_repeats=3)["steps"]) == 3, "prefers the fullest workflow, not a sub-pair")

    # too few repeats -> nothing offered
    check(detect_repeated_workflow(wf * 2, min_repeats=3) is None, "under the repeat threshold -> no candidate")
    check(detect_repeated_workflow(["a", "b", "c", "d"], min_repeats=3) is None, "no repetition -> None")

    # ── Parameters ──
    check(workflow_parameters(["download {invoice}", "save to {folder}"]) == ["invoice", "folder"], "extracts {parameters}")
    check(render_steps(["save to {folder}"], {"folder": "Invoices"}) == ["save to Invoices"], "renders parameters")
    check(render_steps(["x {missing}"], {}) == ["x {missing}"], "leaves an unfilled parameter intact")

    # ── Store: learn / list / version / branch / delete ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # activity log -> detection facade
        for step in activity:
            await mem.log_activity(step)
        cand = await mem.detect_learnable_workflow(min_repeats=3)
        check(cand and cand["steps"] == wf, "detection works over the recorded activity log")

        sid = await mem.learn_skill("Invoice filing", ["open_downloads", "rename {file}", "move_to_invoices"])
        check(bool(sid), "skill learned, id returned")
        skill = await mem.get_skill(sid)
        check(skill["version"] == 1 and skill["parameters"] == ["file"], "v1 with detected parameter")

        listed = await mem.list_skills()
        check(len(listed) == 1 and listed[0]["id"] == sid, "skill appears in the roster")

        updated = await mem.update_skill(sid, ["open_downloads", "rename {file}", "tag {label}", "move_to_invoices"])
        check(updated["version"] == 2, "editing bumps the version")
        again = await mem.get_skill(sid)
        check(len(again["versions"]) == 1 and again["versions"][0]["version"] == 1, "prior version kept in history")
        check("label" in again["parameters"], "new parameter picked up after edit")

        branch_id = await mem.branch_skill(sid, "Receipt filing")
        check(branch_id and branch_id != sid, "branch creates an independent new skill")
        check(len(await mem.list_skills()) == 2, "branch adds to the roster")

        check(await mem.delete_skill(sid) is True, "skill deletable")
        check(len(await mem.list_skills()) == 1, "deleted skill removed from roster")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
