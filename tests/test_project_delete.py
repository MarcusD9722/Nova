"""Project deletion: recoverable by default, permanent only under CRITICAL.

Deleting is irreversible in the worst case, so the design under test is:
delete MOVES to projects/.trash/ (undoable), and only an explicit purge erases
bytes. These tests assert the sandbox holds and that nothing destroys data by
accident.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.permissions import ADMIN, CRITICAL, STANDARD, evaluate, tier_of
from core.project_manager import ProjectManager

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def make_project(pm: ProjectManager, name: str, files=3) -> Path:
    p = pm.project_path(name)
    p.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (p / f"file{i}.py").write_text(f"# file {i}\nprint({i})\n", encoding="utf-8")
    return p


async def main():
    # ── Permission tiers encode the safety story ──
    check(tier_of("project.delete") == ADMIN, "delete is ADMIN (needs approval, recoverable)")
    check(tier_of("project.purge") == CRITICAL, "purge is CRITICAL (permanent)")
    check(tier_of("project.restore") == STANDARD, "restore is STANDARD (undoing is low-risk)")
    check(evaluate("project.delete", mode="guarded") == "confirm", "delete needs confirmation by default")
    check(evaluate("project.purge", mode="guarded") == "deny",
          "PERMANENT purge is DENIED in the default mode")
    check(evaluate("project.purge", mode="trusted") == "confirm",
          "purge is never auto-allowed — confirmation even when trusted")
    check(evaluate("project.delete", mode="locked") == "deny", "locked mode refuses deletion entirely")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir()
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        make_project(pm, "doomed", files=3)
        make_project(pm, "keeper", files=2)
        check(pm.list_projects() == ["doomed", "keeper"], "both projects listed")

        # ── delete MOVES to trash; bytes survive ──
        res = pm.delete_project("doomed")
        check(res["files"] == 3 and res["recoverable"] is True, f"delete reports what it moved ({res['files']} files)")
        check(not (projects / "doomed").exists(), "project folder gone from projects/")
        check(pm.list_projects() == ["keeper"], "deleted project no longer listed")
        trashed = projects / ".trash" / res["moved_to_trash"]
        check(trashed.is_dir() and len(list(trashed.glob("*.py"))) == 3,
              "files still exist in trash — nothing was destroyed")

        # ── trash is not a project ──
        check(".trash" not in pm.list_projects(), "the trash folder is never listed as a project")

        # ── restore brings it back intact ──
        listed = pm.list_trash()
        check(len(listed) == 1 and listed[0]["original"] == "doomed", "trash listing shows the original name")
        pm.restore_project(res["moved_to_trash"])
        check((projects / "doomed").is_dir(), "project restored")
        check(len(list((projects / "doomed").glob("*.py"))) == 3, "all files came back")
        check(pm.list_trash() == [], "trash is empty after restore")

        # ── restore refuses to clobber a live project ──
        pm.delete_project("doomed")
        make_project(pm, "doomed", files=1)          # recreate with the same name
        entry = pm.list_trash()[0]["entry"]
        try:
            pm.restore_project(entry)
            check(False, "restore should refuse to overwrite an existing project")
        except FileExistsError:
            check(True, "restore refuses to clobber an existing project")

        # ── purge is the ONLY thing that destroys data ──
        purged = pm.purge_trash(entry)
        check(purged["permanent"] is True and purged["purged"][0]["files"] == 3, "purge reports what it erased")
        check(not (projects / ".trash" / entry).exists(), "purged entry is gone for good")
        check((projects / "doomed").is_dir(), "the live project was untouched by the purge")

        # ── sandbox: no escaping projects/ ──
        outside = root / "secrets"
        outside.mkdir()
        (outside / "keys.txt").write_text("sensitive", encoding="utf-8")
        for evil in ("../secrets", "..\\secrets", "../../etc"):
            try:
                pm.delete_project(evil)
                check(False, f"traversal '{evil}' should be refused")
            except Exception:
                check(True, f"path traversal refused: {evil}")
        check((outside / "keys.txt").exists(), "files outside projects/ are untouched")

        # ── never delete the projects dir itself ──
        try:
            pm.delete_project(".")
            check(False, "deleting the projects dir should be refused")
        except Exception:
            check(True, "refuses to delete the projects directory itself")

        # ── deleting something that isn't there is an honest error ──
        try:
            pm.delete_project("never-existed")
            check(False, "deleting a missing project should raise")
        except FileNotFoundError:
            check(True, "missing project reports not-found honestly")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
