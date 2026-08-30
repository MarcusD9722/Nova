"""The three original Stage 14 reproductions, rerun (§2, §11 C-G, O).

Each of these was measured on the untouched base and reached COMPLETE, or
published a completion event, or contradicted itself inside one file. They are
driven here through the real `ProjectBuilder`, against a real database, with a
scripted model — the same paths, the same entry points, a different answer.

  S14-1  A + B + C requested; B skipped, C compile-reverted. Was: complete,
         with a summary claiming all three and the failures discarded.
  S14-2  "a calculator that can add and subtract"; only add implemented.
         Was: complete, four lines below the request that says otherwise.
  S14-3  a program that crashes on every run. Was: "Build complete." in the
         log of a project needing attention, and project.completed published.

Run:  venv\\Scripts\\python.exe tests\\test_completion_truth_s14.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_PROJECT_RUN_CHECK", "1")
os.environ.setdefault("NOVA_PROJECT_LOGIC_TESTS", "0")

from harness import Checks, run  # noqa: E402

from core.completion import COMPLETE, FAILING, PARTIALLY_IMPLEMENTED  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from core.project_builder import ProjectBuilder  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

CALC_REQUEST = "a calculator that can add and subtract two numbers"


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def fence(body: str) -> str:
    return "```python\n" + body + "\n```"


class Script:
    """A model that answers by what it is being asked for.

    Deliberately explicit: every branch is a decision the real model would
    make, and the test says which one it is making.
    """

    def __init__(self, *, plan=None, criteria=None, files=None, checks=None,
                 default_file=None):
        self.plan = plan
        self.criteria = criteria
        self.files = files or {}
        self.checks = checks or {}
        self.default_file = default_file
        self.seen: list[str] = []

    async def chat(self, messages, **kw):
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        self.seen.append(prompt[:80])

        if "ACCEPTANCE CRITERIA" in prompt:
            return self.criteria or '{"criteria": []}'
        if '"files"' in prompt and "Plan a small" in prompt:
            return self.plan or '{"files": []}'
        if '"changes"' in prompt:
            return self.plan or '{"changes": []}'
        if "Suggest exactly 3" in prompt:
            return '{"suggestions": ["a", "b", "c"]}'
        if "Write a test file" in prompt:
            return "NO_TESTS"
        if "decides ONE question about a program" in prompt:
            # Match on the criterion line only. The prompt also quotes the
            # whole request, so matching the prompt made every criterion look
            # like the first one that shared a word with it.
            after = prompt.split("acceptance criterion — is:", 1)
            criterion_line = (after[1].split("It comes from", 1)[0]
                              if len(after) > 1 else "")
            for needle, reply in self.checks.items():
                if needle in criterion_line:
                    return reply
            return "CANNOT_CHECK"
        for needle, reply in self.files.items():
            if needle in prompt:
                return reply
            if f"`{needle}`" in prompt:
                return reply
        return self.default_file or fence("pass")


async def build(td, slug, brief, script):
    root = Path(td)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
    await mem.initialize()
    pb = ProjectBuilder(projects_dir=projects, llm=script,
                        llm_semaphore=asyncio.Semaphore(1), memory=mem)
    await pb._build(slug, brief)
    return mem, pb, projects / slug


def completion_events(slug: str):
    """Completion events FOR THIS PROJECT.

    BUS.recent() is process-wide. Counting it wholesale attributed one test's
    events to the next one, which is the same identity mistake this campaign
    keeps finding: never count what is in the buffer, count what belongs to
    the thing under test.
    """
    return [e for e in BUS.recent(limit=600)
            if e.type == "project.completed"
            and (e.data or {}).get("project") == slug]


async def test_s14_2_a_calculator_that_cannot_subtract():
    check.section("S14-2 rerun: the calculator that could only add")
    with _tmp() as td:
        script = Script(
            plan=('{"summary": "a calculator", "language": "python",'
                  ' "files": [{"path": "main.py", "purpose": "the calculator"}],'
                  ' "run": "python main.py"}'),
            criteria=('{"criteria": ['
                      '{"text": "adds two numbers and returns their sum",'
                      ' "origin_quote": "add", "verify_kind": "machine"},'
                      '{"text": "subtracts two numbers and returns the difference",'
                      ' "origin_quote": "subtract", "verify_kind": "machine"}]}'),
            default_file=fence('def add(a, b):\n    return a + b\n\n\n'
                               'if __name__ == "__main__":\n    print(add(2, 3))'),
            checks={
                "adds two numbers": fence(
                    'from main import add\nassert add(2, 3) == 5\n'
                    'print("add works")'),
                "subtracts two numbers": fence(
                    'from main import subtract\n'
                    'assert subtract(5, 3) == 2\nprint("subtract works")'),
            })
        mem, pb, path = await build(td, "calculator", CALC_REQUEST, script)

        v = await pb.completion.evaluate(slug="calculator")
        code = (path / "main.py").read_text(encoding="utf-8")
        check("def subtract" not in code, "the program still cannot subtract")
        check(v.state != COMPLETE,
              f"and the project is NOT complete ({v.state})")
        check(v.state in (FAILING, PARTIALLY_IMPLEMENTED),
              f"it is failing or partially implemented ({v.state})")

        named = [s.criterion.text for s in (v.failing + v.outstanding)]
        check(any("subtract" in t for t in named),
              f"subtraction is named as what is wrong ({named})")
        passed = [s.criterion.text for s in v.criteria if s.verdict == "passed"]
        check(any("add" in t for t in passed),
              f"while addition is credited ({passed})")

        md = (path / "PROJECT.md").read_text(encoding="utf-8")
        check("## Status\ncomplete" not in md,
              "PROJECT.md does not say complete")
        check(v.state in md, f"it says {v.state}")
        rec = await mem.get_latest_fact("project:calculator", "status")
        check(getattr(rec, "value", None) == v.state,
              f"the durable fact agrees ({getattr(rec, 'value', None)})")
        check(not completion_events("calculator"),
              f"and project.completed did not fire "
              f"({len(completion_events('calculator'))})")


async def test_s14_2b_then_subtraction_is_implemented_and_proven():
    check.section("S14-2 rerun: and only then may it complete")
    with _tmp() as td:
        both = ('def add(a, b):\n    return a + b\n\n\n'
                'def subtract(a, b):\n    return a - b\n\n\n'
                'if __name__ == "__main__":\n    print(add(2, 3))')
        script = Script(
            plan=('{"summary": "a calculator", "language": "python",'
                  ' "files": [{"path": "main.py", "purpose": "the calculator"}],'
                  ' "run": "python main.py"}'),
            criteria=('{"criteria": ['
                      '{"text": "adds two numbers and returns their sum",'
                      ' "origin_quote": "add", "verify_kind": "machine"},'
                      '{"text": "subtracts two numbers and returns the difference",'
                      ' "origin_quote": "subtract", "verify_kind": "machine"}]}'),
            default_file=fence(both),
            checks={
                "adds two numbers": fence(
                    'from main import add\nassert add(2, 3) == 5\nprint("ok")'),
                "subtracts two numbers": fence(
                    'from main import subtract\n'
                    'assert subtract(5, 3) == 2\nprint("ok")'),
            })
        mem, pb, path = await build(td, "calculator", CALC_REQUEST, script)
        v = await pb.completion.evaluate(slug="calculator")
        check(v.state == COMPLETE,
              f"with both criteria demonstrated it is COMPLETE ({v.state})")
        check(v.seal_mode == "auto",
              f"on an automatically sealed contract ({v.seal_mode!r})")
        evs = completion_events("calculator")
        check(len(evs) == 1,
              f"project.completed fires exactly once ({len(evs)})")
        check(evs and evs[0].data.get("state") == COMPLETE,
              "and carries the state it means")


async def test_s14_3_a_program_that_crashes_every_run():
    check.section("S14-3 rerun: no 'Build complete.' on a broken build")
    with _tmp() as td:
        script = Script(
            plan=('{"summary": "a timer", "language": "python",'
                  ' "files": [{"path": "main.py", "purpose": "the timer"}],'
                  ' "run": "python main.py"}'),
            criteria=('{"criteria": [{"text": "counts down from a number",'
                      ' "origin_quote": "a countdown timer",'
                      ' "verify_kind": "machine"}]}'),
            default_file=fence(
                'def countdown(n):\n'
                '    raise RuntimeError("broken on purpose")\n\n\n'
                'if __name__ == "__main__":\n    countdown(10)'),
            checks={"counts down": fence(
                'from main import countdown\ncountdown(3)\nprint("ok")')})
        mem, pb, path = await build(td, "timer", "a countdown timer", script)

        v = await pb.completion.evaluate(slug="timer")
        md = (path / "PROJECT.md").read_text(encoding="utf-8")
        check("Build complete." not in md,
              "the log never claims the build completed")
        check(v.state != COMPLETE, f"the state reflects the failure ({v.state})")
        check(v.state in md, "and PROJECT.md shows that same state")
        check(not completion_events("timer"),
              f"project.completed did not fire "
              f"({len(completion_events('timer'))})")
        rec = await mem.get_latest_fact("project:timer", "status")
        check(getattr(rec, "value", None) == v.state,
              f"the durable fact agrees ({getattr(rec, 'value', None)})")
        check(any(e.type == "project.state_changed" for e in BUS.recent(limit=400)),
              "a state_changed event says what actually happened")


async def test_s14_1_partial_improvement():
    check.section("S14-1 rerun: A succeeds, B skipped, C reverted")
    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        proj = projects / "widget"
        proj.mkdir(parents=True)
        (proj / "PROJECT.md").write_text(
            "# widget\n\n## Brief\nA widget.\n\n## Status\nidea\n",
            encoding="utf-8")
        for n in ("a.py", "b.py", "c.py"):
            (proj / n).write_text(
                f"# {n} — the original file, before any improvement\n"
                f"DONE = False\n", encoding="utf-8")

        mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
        await mem.initialize()

        script = Script(
            plan=('{"changes": [{"path": "a.py", "what": "A"},'
                  ' {"path": "b.py", "what": "B"}, {"path": "c.py", "what": "C"}],'
                  ' "summary": "add features A, B and C"}'),
            criteria=('{"criteria": ['
                      '{"text": "feature A works", "origin_quote": "feature A"},'
                      '{"text": "feature B works", "origin_quote": "feature B"},'
                      '{"text": "feature C works", "origin_quote": "feature C"}]}'),
            files={
                # Comfortably over the failed-generation floor, so this really
                # is written and the OTHER two really do take the paths named.
                "a.py": fence('"""Feature A, implemented properly."""\n\n'
                              'DONE = True\n\n\n'
                              'def a():\n    return "A"\n'),
                "b.py": "",                        # empty -> SKIPPED
                "c.py": fence('"""Feature C, generated with a syntax error."""\n\n'
                              'DONE = True\n\n\n'
                              'def c(:\n    return "broken"\n'),
            },
            checks={
                "feature A": fence('from a import a\nassert a() == "A"\nprint("ok")'),
                "feature B": fence('from b import b\nassert b() == "B"\nprint("ok")'),
                "feature C": fence('from c import c\nassert c() == "C"\nprint("ok")'),
            })
        pb = ProjectBuilder(projects_dir=projects, llm=script,
                            llm_semaphore=asyncio.Semaphore(1), memory=mem)
        await pb._improve("widget", "add feature A, feature B and feature C")

        v = await pb.completion.evaluate(slug="widget")
        b_body = (proj / "b.py").read_text(encoding="utf-8")
        c_body = (proj / "c.py").read_text(encoding="utf-8")
        check("DONE = False" in b_body and "DONE = False" in c_body,
              "B and C really were not implemented")
        check(v.state != COMPLETE,
              f"so the project is NOT complete ({v.state})")

        named = [s.criterion.text for s in (v.failing + v.outstanding)]
        check(any("B" in t for t in named) and any("C" in t for t in named),
              f"B and C are named as outstanding or failing ({named})")

        md = (proj / "PROJECT.md").read_text(encoding="utf-8")
        check("## Status\ncomplete" not in md, "PROJECT.md does not say complete")
        check("NOT changed" in md,
              "and the log records what was NOT changed")
        summary_block = md.split("## Summary", 1)[1][:120] if "## Summary" in md else ""
        check(summary_block and "requested:" in summary_block,
              f"the summary is framed as what was REQUESTED, not achieved "
              f"({summary_block.strip()[:60]!r})")
        check("Changed: a.py" in md,
              "and the log names only the file that actually changed")
        check(not completion_events("widget"),
              f"project.completed did not fire "
              f"({len(completion_events('widget'))})")


async def main() -> None:
    await test_s14_2_a_calculator_that_cannot_subtract()
    await test_s14_2b_then_subtraction_is_implemented_and_proven()
    await test_s14_3_a_program_that_crashes_every_run()
    await test_s14_1_partial_improvement()
    check.finish()


if __name__ == "__main__":
    run(main)
