"""Stage 15 — what the UI is owed when a build stops.

Stage 14 narrowed `project.completed` so it fires only on a real transition
into COMPLETE. That is right. But `frontend/src/hooks/useNovaBusEffects.js`
listened for exactly `project.completed` and `project.error` to post the
"I worked on X" message, so the narrowing removed the only thing the UI heard
when a build finished badly. Measured on a real build whose criteria failed:

    backend completion state : failing
    events published         : project.progress x9
                               project.state_changed x1
                               project.validation_failed x2
    events the UI listens for: project.progress only

The build ended, the backend knew it had failed, and the user was told nothing.
Neither change was wrong on its own, which is why no suite caught it.

This asserts the BACKEND half of the contract the UI now depends on. The other
half lives in `frontend/src/projectEvents.test.mjs`; each is useless alone,
because the defect was in the gap between them.

  I19  foreground success cannot conceal background failure
  I30  event payloads identify their true origin
  I32  frontend cannot claim a success the backend does not have

Run:  venv\\Scripts\\python.exe tests\\test_s15_build_report_contract.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_PROJECT_RUN_CHECK", "1")
os.environ.setdefault("NOVA_PROJECT_LOGIC_TESTS", "0")

from harness import Checks, boot, run  # noqa: E402

from core.completion import COMPLETE  # noqa: E402
from core.event_bus import BUS  # noqa: E402

check = Checks()

#: Exactly what frontend/src/projectEvents.js subscribes to.
UI_REPORT_TYPES = {"project.completed", "project.error", "project.state_changed"}

PLAN = ('{"summary": "a calculator", "language": "python",'
        ' "files": [{"path": "main.py", "purpose": "the calculator"}],'
        ' "run": "python main.py"}')
CRITERIA = ('{"criteria": ['
            '{"text": "adds two numbers", "origin_quote": "add",'
            ' "verify_kind": "machine"},'
            '{"text": "subtracts two numbers", "origin_quote": "subtract",'
            ' "verify_kind": "machine"}]}')
ONLY_ADD = ('```python\ndef add(a, b):\n    return a + b\n\n\n'
            'if __name__ == "__main__":\n    print(add(2, 3))\n```')
BOTH = ('```python\ndef add(a, b):\n    return a + b\n\n\n'
        'def subtract(a, b):\n    return a - b\n\n\n'
        'if __name__ == "__main__":\n    print(add(2, 3), subtract(5, 3))\n```')
CHECK_ADD = ('```python\nfrom main import add\nassert add(2, 3) == 5\n'
             'print("ok")\n```')
CHECK_SUB = ('```python\nfrom main import subtract\n'
             'assert subtract(5, 3) == 2\nprint("ok")\n```')


def script(nova, code: str) -> None:
    def route(prompt: str) -> str:
        if "ACCEPTANCE CRITERIA" in prompt:
            return CRITERIA
        if "Plan a small" in prompt:
            return PLAN
        if "Write a Python script that decides ONE question" in prompt:
            return CHECK_SUB if "subtract" in prompt else CHECK_ADD
        if "Suggest exactly 3" in prompt:
            return '{"suggestions": ["a", "b", "c"]}'
        return code

    nova.llm.when(lambda _p: True, route, label="build")
    nova.llm.rules.insert(0, nova.llm.rules.pop())


def events_for(new, slug: str, etype: str) -> list:
    return [e for e in new
            if e.type == etype and str(e.data.get("project") or "") == slug]


async def test_a_failed_build_tells_the_ui_something():
    check.section("I19/I32 a build that fails does not finish in silence")
    async with boot(default_reply="Sure.") as nova:
        script(nova, ONLY_ADD)
        before = len(BUS.recent(900))
        await nova.runtime._project_builder._build(
            "calc", "a calculator that can add and subtract")
        new = BUS.recent(900)[before:]

        verdict = await nova.runtime.completion.evaluate(slug="calc")
        check(verdict.state != COMPLETE,
              f"the build really did not complete ({verdict.state})")

        completed = events_for(new, "calc", "project.completed")
        check(not completed,
              f"project.completed did NOT fire, as Stage 14 intends "
              f"({len(completed)})")

        changed = events_for(new, "calc", "project.state_changed")
        check(len(changed) == 1,
              f"exactly one state_changed announced the finish ({len(changed)})")

        d = changed[0].data if changed else {}
        check(str(d.get("mode")) == "build",
              f"it carries mode=build, which is how the UI knows a RUN "
              f"finished rather than a step happening ({d.get('mode')!r})")
        check(str(d.get("current")) == verdict.state,
              f"it carries the state the evaluator holds "
              f"({d.get('current')!r} vs {verdict.state!r})")
        # `reason` is the OCCASION ("build finished"); `state_reason` is the
        # explanation. The UI prints the explanation, so the event has to carry
        # it separately or the two get confused.
        check(str(d.get("reason") or "") == "build finished",
              f"reason says why it was announced ({d.get('reason')!r})")
        check("failing" in str(d.get("state_reason") or ""),
              f"and state_reason says why the STATE is what it is "
              f"({str(d.get('state_reason'))[:60]!r})")
        named = list(d.get("failing") or []) + list(d.get("outstanding") or [])
        check(any("subtract" in t for t in named),
              f"naming the criterion that is not satisfied ({named})")

        # The event the UI listens for exists, and carries enough to write an
        # honest sentence without asking the backend anything else.
        reportable = [e for e in new if e.type in UI_REPORT_TYPES
                      and str(e.data.get("project") or "") == "calc"
                      and e.type != "project.progress"]
        check(bool(reportable),
              f"so the UI has something to report ({[e.type for e in reportable]})")


async def test_a_successful_build_still_uses_project_completed():
    check.section("the good path is unchanged")
    async with boot(default_reply="Sure.") as nova:
        script(nova, BOTH)
        before = len(BUS.recent(900))
        await nova.runtime._project_builder._build(
            "calc2", "a calculator that can add and subtract")
        new = BUS.recent(900)[before:]

        verdict = await nova.runtime.completion.evaluate(slug="calc2")
        check(verdict.state == COMPLETE,
              f"this one completed ({verdict.state})")
        completed = events_for(new, "calc2", "project.completed")
        check(len(completed) == 1,
              f"project.completed fired exactly once ({len(completed)})")

        changed = events_for(new, "calc2", "project.state_changed")
        check(len(changed) == 1 and str(changed[0].data.get("current")) == COMPLETE,
              "and the state_changed alongside it says complete, which the UI "
              "deliberately does NOT turn into a warning")


async def main() -> None:
    await test_a_failed_build_tells_the_ui_something()
    await test_a_successful_build_still_uses_project_completed()
    check.finish()


if __name__ == "__main__":
    run(main)
