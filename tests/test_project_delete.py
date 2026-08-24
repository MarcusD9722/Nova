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
from core.project_manager import NotAProjectError, ProjectManager

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def make_project(pm: ProjectManager, name: str, files=3) -> Path:
    """A REAL project, i.e. one carrying the identity document.

    This used to `mkdir` and stop, which made a directory rather than a
    project. That was invisible while ProjectManager counted any directory
    as a project; now that there is one definition and PROJECT.md is it,
    the fixture has to build the thing it claims to build. Going through
    `ensure_workspace` — the production creation path — also keeps it
    honest about what a project actually looks like on disk.
    """
    p = pm.ensure_workspace(name)
    for i in range(files):
        (p / f"file{i}.py").write_text(f"# file {i}\nprint({i})\n", encoding="utf-8")
    return p


async def test_delete_obeys_the_project_identity_contract():
    """Destroying something is the last place a definition may drift.

    Every read surface agrees that a directory without PROJECT.md is not a
    project: `list_projects` omits it, conversation cannot name it, `select`
    refuses it, `status` denies it, `last_active` will not return it. Measured
    on b2a931e, `delete_project()` still accepted it and moved it to trash —
    the one surface that disagreed was the destructive one.
    """
    print("\nidentity: delete refuses what is not a project")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir()
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        real = make_project(pm, "keeper", files=2)
        raw = projects / "orphan-dir"
        raw.mkdir()
        (raw / "main.py").write_text("x = 1\n", encoding="utf-8")
        before = (raw / "main.py").read_bytes()

        check(pm.list_projects() == ["keeper"],
              f"the raw directory is not a project ({pm.list_projects()})")
        check(pm.list_unadopted() == ["orphan-dir"],
              f"…it is unadopted ({pm.list_unadopted()})")

        try:
            pm.delete_project("orphan-dir")
            check(False, "delete should refuse a directory that is not a project")
        except NotAProjectError as e:
            check("PROJECT.md" in str(e),
                  f"delete refuses it, and says why ({str(e)[:70]!r})")

        check(raw.is_dir(), "the directory is still there")
        check((raw / "main.py").read_bytes() == before,
              "its files are byte-identical, in place")
        check(pm.list_unadopted() == ["orphan-dir"],
              "and it is still reported as unadopted")
        check(not (projects / ".trash").exists()
              or not any((projects / ".trash").iterdir()),
              "nothing was moved to trash")

        # …and "not a project" is distinguishable from "not there at all",
        # because only one of them has a remedy.
        try:
            pm.delete_project("no-such-thing")
            check(False, "a missing directory should still raise")
        except NotAProjectError:
            check(False, "a missing directory must not report as 'not a project'")
        except FileNotFoundError:
            check(True, "a missing directory is still FileNotFoundError")

        # ADOPT, then delete works normally.
        res = pm.adopt_project("orphan-dir")
        check(res["adopted"] is True, f"adoption converts it ({res})")
        check("orphan-dir" in pm.list_projects(), "now it is a project")
        info = pm.delete_project("orphan-dir")
        check(info["project"] == "orphan-dir" and info["recoverable"] is True,
              f"and delete works ({info})")
        check(not raw.exists(), "the directory moved to trash")
        restored = pm.restore_project(info["moved_to_trash"])
        check(restored["restored"] == "orphan-dir",
              f"restore brings it back under its own name ({restored})")
        check((raw / "main.py").read_bytes() == before,
              "with its original file intact")

        # A real project is unaffected by any of this.
        check("keeper" in pm.list_projects(), "the real project is untouched")
        keep = pm.delete_project("keeper")
        check(keep["project"] == "keeper", "and still deletes normally")
        pm.restore_project(keep["moved_to_trash"])
        check(real.is_dir() and "keeper" in pm.list_projects(),
              "and restores normally")


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

        doomed = make_project(pm, "doomed", files=3)
        make_project(pm, "keeper", files=2)
        # Counted, not assumed. A real project carries its identity
        # document and chat log as well as the fixture's source files, and
        # the claim worth testing is that delete/purge REPORT what they
        # actually moved — not that a project happens to hold three files.
        doomed_files = sum(1 for f in doomed.rglob("*") if f.is_file())
        check(doomed_files > 3,
              f"a real project is more than its source files ({doomed_files})")
        check(pm.list_projects() == ["doomed", "keeper"], "both projects listed")

        # ── delete MOVES to trash; bytes survive ──
        res = pm.delete_project("doomed")
        check(res["files"] == doomed_files and res["recoverable"] is True,
              f"delete reports what it moved ({res['files']} of {doomed_files})")
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
        check(purged["permanent"] is True
              and purged["purged"][0]["files"] == doomed_files,
              f"purge reports what it erased ({purged['purged'][0]['files']})")
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

    await test_delete_obeys_the_project_identity_contract()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
