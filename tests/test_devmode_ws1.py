"""WS1 verification: guarded self-editing — backup, compile-check, boot-test,
auto-rollback, manual rollback, persistence, and security denials."""
import os, sys, tempfile, shutil, subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["NOVA_DEV_MODE"] = "1"

from core.dev_mode import DevMode, DevModeError  # noqa: E402

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

# Work on a throwaway module inside the repo so boot-test (import) works, then clean up.
SANDBOX = REPO / "_ws1_sandbox_tmp"
SANDBOX.mkdir(exist_ok=True)
(SANDBOX / "__init__.py").write_text("", encoding="utf-8")
mod = SANDBOX / "mod.py"
mod.write_text("VALUE = 1\n", encoding="utf-8")

try:
    projects = REPO / "projects"
    dev = DevMode(repo_root=REPO, projects_dir=projects)

    # 1) Propose a good change -> apply -> backup made + boot test passes
    p1 = dev.propose_change(str(mod), "VALUE = 2\n", reason="bump")
    r1 = dev.apply_proposal(p1.id, confirm=True)
    check(r1["status"] == "applied", f"good change applies (status={r1['status']})")
    check(mod.read_text().strip() == "VALUE = 2", "file content updated on apply")
    check(bool(r1.get("backup")) and Path(r1["backup"]).exists(), "backup created before apply")

    # 2) Apply requires confirm
    p2 = dev.propose_change(str(mod), "VALUE = 3\n", reason="x")
    try:
        dev.apply_proposal(p2.id, confirm=False)
        check(False, "apply without confirm should raise")
    except DevModeError:
        check(True, "apply without confirm refused")

    # 3) Syntax-broken change is refused BEFORE writing (compile-check)
    p3 = dev.propose_change(str(mod), "def broken(:\n  pass\n", reason="bad syntax")
    try:
        dev.apply_proposal(p3.id, confirm=True)
        check(False, "syntax-broken change should be refused")
    except DevModeError as e:
        check("syntax" in str(e).lower(), "syntax-broken change refused pre-apply")
    check(mod.read_text().strip() == "VALUE = 2", "file untouched after refused syntax change")

    # 4) Import-time error -> passes compile but fails boot test -> AUTO-ROLLBACK
    p4 = dev.propose_change(str(mod), "import nonexistent_pkg_xyz123\nVALUE = 9\n", reason="import bomb")
    r4 = dev.apply_proposal(p4.id, confirm=True)
    check(r4["status"] == "reverted" and r4.get("rolled_back"), f"import error auto-rolled back (status={r4['status']})")
    check(mod.read_text().strip() == "VALUE = 2", "file restored to pre-apply content after failed boot test")

    # 5) Manual rollback of an applied change
    p5 = dev.propose_change(str(mod), "VALUE = 42\n", reason="to be rolled back")
    dev.apply_proposal(p5.id, confirm=True)
    check(mod.read_text().strip() == "VALUE = 42", "applied before manual rollback")
    dev.rollback_proposal(p5.id)
    check(mod.read_text().strip() == "VALUE = 2", "manual rollback restored previous content")

    # 6) Persistence across a fresh DevMode instance
    dev2 = DevMode(repo_root=REPO, projects_dir=projects)
    ids = {p["id"] for p in dev2.list_proposals()}
    check(p1.id in ids and p5.id in ids, "proposals persisted across restart")

    # 7) SECURITY: deny .env, .git, model/, and outside-repo
    for bad, label in [
        (str(REPO / ".env"), ".env"),
        (str(REPO / ".git" / "config"), ".git/config"),
        (str(REPO / "model" / "x.gguf"), "model/ weights"),
        (r"C:\Windows\System32\drivers\etc\hosts", "system file (outside repo)"),
        (str(REPO / ".nova_dev" / "proposals" / "x.json"), ".nova_dev state"),
    ]:
        try:
            dev.propose_change(bad, "hacked", reason="should be denied")
            check(False, f"deny write to {label}")
        except DevModeError:
            check(True, f"deny write to {label}")

    print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
    sys.exit(1 if fails else 0)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    # clean the throwaway proposals/backups we created for the sandbox module
    state = REPO / ".nova_dev"
    if state.exists():
        for jf in (state / "proposals").glob("*.json"):
            try:
                import json
                d = json.loads(jf.read_text())
                if "_ws1_sandbox_tmp" in d.get("path", ""):
                    jf.unlink()
            except Exception:
                pass
        for bf in (state / "backups").glob("*_ws1_sandbox_tmp*"):
            bf.unlink(missing_ok=True)
        for bf in (state / "backups").glob("*__mod.py"):
            bf.unlink(missing_ok=True)
