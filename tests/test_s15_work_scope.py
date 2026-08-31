"""Stage 15 — a question about "the work" is not a question about one project.

Two context builders answer the same turn:

    describe_work_state()   goals and tasks, for EVERY project
    _completion_context()   completion, for ONE project (named, else current)

They disagreed about the scope of the same question, and the disagreement was
invisible because each is correct on its own. Measured on the pre-fix code:
project `alpha` with a goal marked done and current, project `bravo` FAILING its
acceptance criteria. Asked "How is the work going?", the answer prompt
contained

    - goal 'ship the alpha feature' (alpha): done, revision 0

and nothing at all about the project that was failing.

A named project still wins outright — "is the calculator done?" is about the
calculator — because that is the distinction `_completion_context` was built to
make. What changed is that an UNNAMED question now describes the same
population `describe_work_state` already describes.

Asserts the grounding, never the prose: the scripted model ignores its prompt,
and a test that read the reply would pass on a stub's opinion.

  I31  chat answers derive from authoritative state
  I37  no subsystem silently converts partial into complete
  I39  no system-level answer is assembled from unscoped fragments

Run:  venv\\Scripts\\python.exe tests\\test_s15_work_scope.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import FAILED, PASSED  # noqa: E402

check = Checks()

#: The header `describe_for_chat` puts above the completion record.
COMPLETION_HEADER = "The completion state of the work"


class Prompts:
    """Every prompt the model was handed this turn, kept whole."""

    def __init__(self, nova):
        self.seen: list[str] = []
        nova.llm.when(lambda _p: True, self._handle, label="capture")
        nova.llm.rules.insert(0, nova.llm.rules.pop())

    def _handle(self, prompt: str) -> str:
        self.seen.append(prompt)
        return "Everything looks fine."

    def clear(self) -> None:
        self.seen.clear()

    @property
    def grounding(self) -> str:
        """The prompt carrying the completion record, or "" if none did."""
        for p in self.seen:
            if COMPLETION_HEADER in p:
                return p
        return ""


async def _project(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "main.py").write_text(f"# {name}\n", encoding="utf-8")
    nova.runtime._project_builder._write_project_md(
        name, brief=f"{name} brief", status="building")
    return p


async def _contract(nova, slug: str, *, verdict: str) -> None:
    """Give a project a real sealed contract and one recorded result."""
    svc = nova.runtime.completion
    rev = await svc.record_request(slug=slug,
                                   request_text="a tool that adds numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds numbers", "origin_quote": "adds numbers",
         "verify_kind": "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
    await svc.record_verdict(context=ctx, verdict=verdict,
                             error="boom" if verdict == FAILED else "")


async def test_a_general_question_cannot_omit_a_failing_project():
    check.section("I37/I39 'how is the work going?' sees the failing project")
    async with boot(default_reply="Sure.") as nova:
        await _project(nova, "alpha")
        await _project(nova, "bravo")

        gid = await nova.memory.create_goal(
            project_name="alpha", title="ship the alpha feature",
            objective="make alpha work")
        await nova.memory.update_goal_status(goal_id=gid, status="done")
        await _contract(nova, "bravo", verdict=FAILED)
        # alpha is the CURRENT project, and it is the one with the good news.
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="alpha", confidence=0.99)

        v = await nova.runtime.completion.evaluate(slug="bravo")
        check(v.state == "failing", f"bravo really is failing ({v.state})")

        prompts = Prompts(nova)
        await nova.brain.chat("How is the work going?",
                              conversation_id=str(uuid4()))
        g = prompts.grounding

        check(bool(g), "the answer prompt carries a completion record at all")
        check("goal 'ship the alpha feature' (alpha): done" in g,
              "the good news about alpha is there (it always was)")
        check("project 'bravo': failing" in g,
              "and the project that is FAILING is there too")
        check("adds numbers" in g and "boom" in g,
              "naming the criterion and the error, not just a state")

        # The current project has no contract, so it contributes nothing —
        # a scaffolded directory must not cost tokens on every work question.
        check("project 'alpha'" not in g,
              f"alpha has nothing recorded, so it says nothing "
              f"({'project alpha present' if 'project ' + chr(39) + 'alpha' + chr(39) in g else 'absent'})")


async def test_a_named_project_is_still_the_whole_scope():
    check.section("a named project wins, and does not drag the others in")
    async with boot(default_reply="Sure.") as nova:
        await _project(nova, "calculator")
        await _project(nova, "unrelated")
        await _contract(nova, "calculator", verdict=PASSED)
        await _contract(nova, "unrelated", verdict=FAILED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="unrelated", confidence=0.99)

        prompts = Prompts(nova)
        await nova.brain.chat("Is the calculator done?",
                              conversation_id=str(uuid4()))
        g = prompts.grounding

        check(bool(g), "a named completion question gets a completion record")
        check("project 'calculator'" in g,
              "about the project that was NAMED")
        check("project 'unrelated'" not in g,
              "and not about the one that merely happens to be current")


async def test_the_general_scope_is_bounded():
    check.section("a work question does not become a digest of everything")
    async with boot(default_reply="Sure.") as nova:
        names = [f"proj{i}" for i in range(9)]
        for n in names:
            await _project(nova, n)
            await _contract(nova, n, verdict=FAILED)

        prompts = Prompts(nova)
        await nova.brain.chat("How is the work going?",
                             conversation_id=str(uuid4()))
        g = prompts.grounding
        described = [n for n in names if f"project '{n}'" in g]
        cap = nova.runtime._COMPLETION_CONTEXT_PROJECTS
        check(bool(described), f"some projects are described ({len(described)})")
        check(len(described) <= cap,
              f"at most {cap} projects are described ({len(described)})")


async def test_a_referential_question_keeps_stage_14s_narrow_scope():
    """The boundary this fix had to respect, pinned so it cannot drift back.

    My first version of the fix made EVERY unnamed work question survey every
    contracted project. That broke two Stage 14 assertions, and Stage 14 was
    right: "is it done?" points at one thing, and answering it about a project
    the person did not mean is its own defect -- including when the pointer
    names a project that no longer exists, where the honest answer is about
    nothing rather than about whatever else is lying around.
    """
    check.section("I31 'is it done?' points at one thing, and may point at none")
    async with boot(default_reply="Sure.") as nova:
        await _project(nova, "one")
        await _project(nova, "two")
        await _contract(nova, "one", verdict=PASSED)
        await _contract(nova, "two", verdict=FAILED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="one", confidence=0.99)

        prompts = Prompts(nova)
        await nova.brain.chat("Is it done?", conversation_id=str(uuid4()))
        g = prompts.grounding
        check("project 'one'" in g, "the current project is described")
        check("project 'two'" not in g,
              "and the OTHER project is not, however it is doing")

        # Now point the pointer at something that does not exist.
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="vanished", confidence=0.99)
        prompts.clear()
        await nova.brain.chat("Is it done?", conversation_id=str(uuid4()))
        g2 = prompts.grounding
        check("project 'one'" not in g2 and "project 'two'" not in g2,
              f"a pointer at nothing answers about nothing, not about whatever "
              f"else exists ({g2[:70]!r})")

        # But the SURVEY question still finds the failing project.
        prompts.clear()
        await nova.brain.chat("What's the status?", conversation_id=str(uuid4()))
        g3 = prompts.grounding
        check("project 'two': failing" in g3,
              f"while a survey question still reports what is failing "
              f"({'found' if 'failing' in g3 else g3[:70]!r})")


async def main() -> None:
    await test_a_general_question_cannot_omit_a_failing_project()
    await test_a_named_project_is_still_the_whole_scope()
    await test_the_general_scope_is_bounded()
    await test_a_referential_question_keeps_stage_14s_narrow_scope()
    check.finish()


if __name__ == "__main__":
    run(main)
