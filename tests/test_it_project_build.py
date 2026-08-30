"""INTEGRATION (U10): a project build writes real files and reports honestly.

Drives the whole build the way a chat message does: pre-pass -> ProjectBuilder
-> plan -> generate -> compile -> run check -> generated logic tests ->
suggestions -> PROJECT.md. Real subprocesses run the generated code; only the
model's text is scripted.

The honesty invariant is the point of the second half. A build that fails must
say so — "complete" on a project that never compiled is the single worst thing
this codebase can do, and it is invisible to any test that stubs the builder.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, boot, run

check = Checks()

PLAN_JSON = """{"summary": "A countdown timer with testable step logic.",
 "language": "python",
 "files": [{"path": "main.py", "purpose": "countdown logic and entry point"}],
 "run": "python main.py"}"""

MAIN_PY = '''```python
def next_value(current):
    """One countdown step; never goes below zero."""
    return max(0, current - 1)


def countdown(start):
    values = []
    current = start
    while current > 0:
        current = next_value(current)
        values.append(current)
    return values


if __name__ == "__main__":
    print(countdown(3))
```'''

TEST_PY = '''```python
from main import countdown, next_value

assert next_value(3) == 2
assert next_value(0) == 0
assert countdown(3) == [2, 1, 0]
print("ok")
```'''

SUGGESTIONS_JSON = ('{"suggestions": ["Add a pause command", "Support counting up", '
                    '"Print a friendly finish message"]}')


def build_rules():
    """Script only the model's side of a normal, successful build."""
    return [
        ("Plan a small, complete, WORKING project", PLAN_JSON),
        ("Write the COMPLETE contents of `main.py`", MAIN_PY),
        ("Write a test file `test_main.py`", TEST_PY),
        ("TRACE each assertion", TEST_PY),
        ("A project was just built", SUGGESTIONS_JSON),
    ]


def md_section(doc: str, heading: str) -> str:
    """Pull one '## <heading>' block out of PROJECT.md."""
    import re

    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=\n## |\Z)", doc, re.DOTALL | re.MULTILINE)
    return (m.group(1).strip() if m else "")


async def wait_for_build(nova, slug: str, timeout_s: float = 120.0) -> bool:
    builder = nova.runtime._project_builder
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not builder.is_building(slug):
            return True
        await asyncio.sleep(0.25)
    return False


async def main() -> None:
    async with boot(rules=build_rules(), default_reply="Working on it.") as nova:

        check.section("A build request starts a real build")
        # Comma-terminated name on purpose: NAME_RE is greedy across spaces, so
        # "called Countdown that counts down from ten" would become the project
        # NAME. Ugly, not dishonest — tracked separately, not U10's business.
        res = await nova.say("build a python script called Countdown, counting down from ten",
                             conversation_id=uuid4())
        check("Countdown" in res.assistant_text or "countdown" in res.assistant_text,
              f"Nova confirms the project by name ({res.assistant_text[:56]!r})")
        check(await wait_for_build(nova, "countdown"), "the build finishes within the timeout")

        check.section("Files actually exist on disk")
        project = nova.projects_dir / "countdown"
        main_py = project / "main.py"
        check(project.is_dir(), f"project directory created ({project.name})")
        check(main_py.is_file(), "main.py was written")
        check("def next_value" in main_py.read_text(encoding="utf-8"), "main.py holds the generated code")
        check((project / "test_main.py").is_file(), "the generated logic test was kept")

        check.section("PROJECT.md reports what really happened")
        doc = (project / "PROJECT.md").read_text(encoding="utf-8")
        # This assertion used to read `== "complete"`, and it was the belief
        # Stage 14 exists to remove: the build wrote files, the run check
        # passed, the logic tests passed, therefore complete. None of that says
        # anything about whether what the PERSON ASKED FOR was delivered.
        #
        # This test's scripted model has no rule for the acceptance-criteria
        # prompt, so it answers "Working on it.", no criteria are agreed, and
        # the contract is never sealed. A build with no agreed definition of
        # done cannot be done. It is SCAFFOLDED: the files are real, and
        # nothing about them has been demonstrated.
        status = md_section(doc, "Status")
        check(status == "scaffolded",
              f"a build with no agreed criteria is scaffolded, not complete "
              f"({status!r})")
        check("complete" not in status.lower(),
              "and PROJECT.md does not claim completion for it")
        log = md_section(doc, "Progress log")
        check("Run check passed" in log, "the run check result is recorded")
        check("Logic tests passed" in log, "the logic-test result is recorded")
        check("Add a pause command" in md_section(doc, "Next steps / suggestions"),
              "the suggestions were written back")

        check.section("Status is queryable in chat afterwards")
        res = await nova.say("where did we leave off on countdown", conversation_id=uuid4())
        check("countdown" in res.assistant_text.lower(), "status answer names the project")

    # ── The honesty half: a build that cannot produce code must SAY so ──
    broken_rules = [
        ("Plan a small, complete, WORKING project", PLAN_JSON),
        # The model returns nothing usable — the exact shape of a truncated or
        # refused generation.
        ("Write the COMPLETE contents of", ""),
    ]
    async with boot(rules=broken_rules, default_reply="Working on it.") as nova:
        check.section("A failed build is reported as failed")
        await nova.say("build a python script called Doomed, something simple",
                       conversation_id=uuid4())
        check(await wait_for_build(nova, "doomed"), "the failed build terminates")

        project = nova.projects_dir / "doomed"
        doc = (project / "PROJECT.md").read_text(encoding="utf-8") if (project / "PROJECT.md").exists() else ""
        status = md_section(doc, "Status")
        log = md_section(doc, "Progress log")
        # There is no "error" status any more: the state is derived from what
        # is recorded, and for a build whose generation came back empty there
        # is a requirement, no criteria, and no files. That is an idea. The
        # failure itself is in the log, which is asserted below.
        check(status == "idea",
              f"a build that produced nothing is an idea, not an achievement "
              f"({status!r})")
        check("complete" not in status.lower(), "it never claims to be complete")
        check("came back empty" in log,
              f"the log says what actually failed ({log.splitlines()[-1][:90] if log else ''!r})")

        res = await nova.say("what's the status of doomed", conversation_id=uuid4())
        check("error" in res.assistant_text.lower() or "failed" in res.assistant_text.lower(),
              f"chat status is honest too ({res.assistant_text[:70]!r})")

    check.finish()


run(main)
