"""A plan made against version A is not applied to version B (13B closure).

THE FAMILY

Stage 13B's brief names artifact drift: a checkpoint or plan is created against
one version of an artifact, something else changes it, and the work resumes.
Nova must notice that the inputs moved before applying stale work. Re-inspect,
invalidate, replan or ask are all fine. Blindly executing the stale assumption
is not.

WHAT WAS MEASURED, on 4d7a316

A proposal is a statement about a SPECIFIC version of a file - "change this
line, in this text" - and applying it writes `new_content` wholesale.

    propose against:  VERSION = 'A'  /  SPEED = 1
    file becomes:     VERSION = 'B'  /  SPEED = 1  /  NEW_FEATURE = True
    apply            -> VERSION = 'A'  /  SPEED = 2
                        status "applied", no warning

Both of the other edits were gone. The diff Marcus approved described a change
to a file that no longer existed in that form, and someone else's work was
overwritten to make the old diff true.

It was backed up, so it was recoverable. Recoverable is not the same as
correct: the user approved one thing and a different thing happened.

THE CHECK is the file's digest at propose time, re-verified at apply time.
Proposals written before that field existed are exempt, because refusing those
would be a guess rather than a check.

Run:  venv\\Scripts\\python.exe tests\\test_artifact_drift_s13b.py
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
os.environ["NOVA_DEV_MODE"] = "1"

from harness import Checks, run  # noqa: E402

from core import dev_mode as dm  # noqa: E402

check = Checks()

VERSION_A = "VERSION = 'A'\nSPEED = 1\n"
VERSION_B = "VERSION = 'B'\nSPEED = 1\nNEW_FEATURE = True\n"
PROPOSED = "VERSION = 'A'\nSPEED = 2\n"


def _workspace(td: str):
    """A dev-mode instance with one registered external project."""
    root = Path(td) / "outside" / "proj"
    root.mkdir(parents=True)
    repo = Path(td) / "repo"
    repo.mkdir(parents=True)
    d = dm.DevMode(repo_root=repo, projects_dir=repo / "projects")
    d.register_external_root("proj", str(root))
    return d, root


async def test_a_changed_file_refuses_the_stale_diff():
    check.section("drift: the file moved on, so the diff no longer applies")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        d, root = _workspace(td)
        f = root / "game.py"
        f.write_text(VERSION_A, encoding="utf-8")

        p = d.propose_change("game.py", PROPOSED, reason="bump speed",
                             project="proj")
        check(bool(p.base_sha),
              f"the proposal records what it was computed against "
              f"({p.base_sha[:12]}...)")
        check("SPEED = 2" in p.diff, "and the diff is the change requested")

        # Something else edits the file: a person, another tool, a git pull.
        f.write_text(VERSION_B, encoding="utf-8")

        refused = ""
        try:
            d.apply_proposal(p.id, confirm=True)
        except dm.DevModeError as e:
            refused = str(e)

        after = f.read_text(encoding="utf-8")
        check(bool(refused), f"applying it is refused ({refused[:70]!r})")
        check("has changed since" in refused,
              "and says why, in terms of the file having moved on")
        check(after == VERSION_B,
              f"the newer content is intact ({after!r})")
        check("NEW_FEATURE" in after,
              "including the line the stale diff knew nothing about")
        check("SPEED = 2" not in after,
              "and the stale change was NOT applied")


async def test_an_unchanged_file_still_applies():
    """COUNTER-TEST. A guard that refused everything would pass the above."""
    check.section("counter: an undisturbed file still takes the change")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        d, root = _workspace(td)
        f = root / "game.py"
        f.write_text(VERSION_A, encoding="utf-8")

        p = d.propose_change("game.py", PROPOSED, reason="bump speed",
                             project="proj")
        out = d.apply_proposal(p.id, confirm=True)
        after = f.read_text(encoding="utf-8")
        check(str(out.get("status")) == "applied",
              f"it applies ({out.get('status')!r})")
        check(after == PROPOSED, f"and the file is the proposed content ({after!r})")


async def test_replanning_against_the_new_version_works():
    """The allowed answer to drift: look again, and propose against what is
    there now."""
    check.section("drift: re-reading and re-proposing is the way through")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        d, root = _workspace(td)
        f = root / "game.py"
        f.write_text(VERSION_A, encoding="utf-8")
        stale = d.propose_change("game.py", PROPOSED, project="proj")
        f.write_text(VERSION_B, encoding="utf-8")

        try:
            d.apply_proposal(stale.id, confirm=True)
            refused = False
        except dm.DevModeError:
            refused = True
        check(refused, "the stale one is refused")

        # Re-read, then propose the same INTENT against the current text.
        current = f.read_text(encoding="utf-8")
        fresh_content = current.replace("SPEED = 1", "SPEED = 2")
        fresh = d.propose_change("game.py", fresh_content, project="proj")
        out = d.apply_proposal(fresh.id, confirm=True)
        after = f.read_text(encoding="utf-8")

        check(str(out.get("status")) == "applied",
              f"the fresh proposal applies ({out.get('status')!r})")
        check("SPEED = 2" in after, "carrying the intended change")
        check("NEW_FEATURE" in after and "VERSION = 'B'" in after,
              f"without losing what changed underneath ({after!r})")


async def test_a_new_file_that_appeared_underneath_is_drift_too():
    check.section("drift: a file that appeared where none was expected")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        d, root = _workspace(td)
        target = root / "brand_new.py"
        check(not target.exists(), "nothing is there when the diff is computed")

        p = d.propose_change("brand_new.py", "FRESH = 1\n", project="proj")
        # Someone creates it first, with different content.
        target.write_text("SOMEONE_ELSE = True\n", encoding="utf-8")

        refused = ""
        try:
            d.apply_proposal(p.id, confirm=True)
        except dm.DevModeError as e:
            refused = str(e)
        after = target.read_text(encoding="utf-8")
        check(bool(refused),
              f"creating over it is refused ({refused[:60]!r})")
        check("SOMEONE_ELSE" in after,
              f"and their file is untouched ({after!r})")


async def main():
    await test_a_changed_file_refuses_the_stale_diff()
    await test_an_unchanged_file_still_applies()
    await test_replanning_against_the_new_version_works()
    await test_a_new_file_that_appeared_underneath_is_drift_too()
    check.finish()


if __name__ == "__main__":
    run(main)
