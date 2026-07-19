"""Offline tests for WS-G (old_content persistence + get_proposal) and WS-I
(external project roots) in core/dev_mode.py. Uses a TEMP repo_root so no
.nova_dev state touches the real repo."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ["NOVA_DEV_MODE"] = "1"

from core.dev_mode import DevMode, DevModeError

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        repo = Path(td) / "fake_repo"
        projects = Path(td) / "fake_projects"
        external = Path(td) / "marcus_other_project"
        for d in (repo, projects, external):
            d.mkdir(parents=True)

        dev = DevMode(repo_root=repo, projects_dir=projects)

        # ── WS-G: old_content persisted + get_proposal detail ──
        target = repo / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        p = dev.propose_change("mod.py", "x = 2\n", reason="ws-g test")
        check(p.old_content == "x = 1\n", "proposal stores old_content")
        on_disk = json.loads((repo / ".nova_dev" / "proposals" / f"{p.id}.json").read_text(encoding="utf-8"))
        check(on_disk.get("old_content") == "x = 1\n", "old_content persisted to disk")
        detail = dev.get_proposal(p.id)
        check(detail["old_content"] == "x = 1\n" and detail["new_content"] == "x = 2\n", "get_proposal returns old+new")
        check(detail["display_path"] == "mod.py", f"repo file display_path is relative (got {detail['display_path']})")

        # Legacy proposal without old_content/project fields still loads
        legacy = dict(on_disk)
        legacy["id"] = "legacy123"
        legacy.pop("old_content"); legacy.pop("project")
        (repo / ".nova_dev" / "proposals" / "legacy123.json").write_text(json.dumps(legacy), encoding="utf-8")
        dev2 = DevMode(repo_root=repo, projects_dir=projects)
        check("legacy123" in dev2._proposals, "legacy proposal (no old_content field) loads without crashing")
        check(dev2._proposals["legacy123"].old_content == "", "legacy old_content defaults to ''")

        # ── WS-I: registration ──
        reg = dev.register_external_root("My Other App!", str(external))
        check(reg["project"] == "my-other-app", f"name slugified (got {reg['project']})")
        check((repo / ".nova_dev" / "external_roots.json").exists(), "external roots persisted")

        try:
            dev.register_external_root("bad", r"C:\Windows\System32")
            check(False, "system dir registration should fail")
        except DevModeError:
            check(True, "system dir registration refused")

        try:
            dev.register_external_root("inside", str(repo / "core"))
            check(False, "inside-repo registration should fail")
        except DevModeError:
            check(True, "inside-own-repo registration refused")

        try:
            dev.register_external_root("ghost", str(Path(td) / "nope"))
            check(False, "nonexistent dir registration should fail")
        except DevModeError:
            check(True, "nonexistent dir registration refused")

        # Registered roots reload on a fresh instance
        dev3 = DevMode(repo_root=repo, projects_dir=projects)
        check("my-other-app" in dev3._extra_roots, "external root survives reload")

        # ── WS-I: guarded flow on the external project ──
        ext_file = external / "script.py"
        ext_file.write_text("value = 'old'\n", encoding="utf-8")

        # read via project arg with relative path
        r = dev.read_file("script.py", project="my-other-app")
        check(r["content"] == "value = 'old'\n", "read_file resolves relative path against external root")

        # deny-list still applies inside external roots
        (external / ".env").write_text("SECRET=1", encoding="utf-8")
        try:
            dev.read_file(".env", project="my-other-app")
            check(False, ".env in external project should be denied")
        except DevModeError:
            check(True, ".env denied inside external project")

        # unknown project errors clearly
        try:
            dev.read_file("script.py", project="nonexistent")
            check(False, "unknown project should fail")
        except DevModeError as e:
            check("Unknown project" in str(e), "unknown project raises a clear error")

        # propose → apply → honest boot skip → backup → rollback
        p2 = dev.propose_change("script.py", "value = 'new'\n", reason="ws-i test", project="my-other-app")
        check(p2.project == "my-other-app", "proposal tagged with project name")
        check(p2.old_content == "value = 'old'\n", "external proposal stores old_content too")
        check("my-other-app:" in dev.get_proposal(p2.id)["display_path"], "external display_path prefixed with project")

        result = dev.apply_proposal(p2.id, confirm=True)
        check(result["status"] == "applied", "external apply succeeds")
        check(result["boot_test"] == "skipped_external_project", f"boot test honestly skipped (got {result['boot_test']})")
        check(ext_file.read_text(encoding="utf-8") == "value = 'new'\n", "external file actually updated")
        backup_path = Path(result["backup"])
        check(backup_path.exists() and str(backup_path).startswith(str(repo)), "backup lives under Nova's .nova_dev, not the external project")
        check("my-other-app" in backup_path.name, "backup filename prefixed with project name")

        rb = dev.rollback_proposal(p2.id)
        check(rb["status"] == "reverted", "external rollback returns reverted")
        check(ext_file.read_text(encoding="utf-8") == "value = 'old'\n", "external file restored on rollback")

        # syntax check still guards external .py applies
        p3 = dev.propose_change("script.py", "def broken(:\n", reason="syntax", project="my-other-app")
        try:
            dev.apply_proposal(p3.id, confirm=True)
            check(False, "syntax-broken external apply should fail")
        except DevModeError as e:
            check("syntax" in str(e).lower(), "external apply refused on syntax error")
        check(ext_file.read_text(encoding="utf-8") == "value = 'old'\n", "file untouched after refused apply")

        # Nova's own code still gets a real boot test (not skipped-external)
        p4 = dev.propose_change("mod.py", "x = 3\n", reason="own-code boot test")
        result4 = dev.apply_proposal(p4.id, confirm=True)
        check(result4["boot_test"] == "passed", f"own-code boot test still runs (got {result4['boot_test']})")

        # Forgotten-project guard: relative path that doesn't exist in the repo
        # but DOES exist in a registered external project must be refused with
        # a self-correcting hint, not silently proposed as a new repo file.
        try:
            dev.propose_change("script.py", "value = 'oops'\n", reason="forgot project arg")
            check(False, "forgotten project arg should be refused")
        except DevModeError as e:
            check("my-other-app" in str(e) and "project=" in str(e), f"forgotten-project guard names the right project (got: {str(e)[:90]})")

        # A genuinely new repo file (name that exists nowhere) is still allowed
        p5 = dev.propose_change("brand_new_module.py", "y = 1\n", reason="genuine new file")
        check(p5.status == "pending", "genuinely new repo file still proposable")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
