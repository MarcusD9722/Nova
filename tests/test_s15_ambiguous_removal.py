"""Stage 15 — a destructive request that could mean another project asks first.

Measured, with `cat-tracker` current and `bug-tracker` also on disk:

    "delete tracker"     -> "Got it — working on those improvements to
                             cat-tracker now."   project.started fired

A destructive request became an autonomous EDIT of a project the person may
well have been asking to delete. Nothing was deleted and nothing was permission
-gated, because the turn never reached the tool loop at all: the pre-pass read
it as an inside-project removal and handed it to the builder.

WHY THE CLASSIFIER WAS RIGHT AND STILL PRODUCED THIS. `classify_removal` is
given ONE slug and knows nothing about the rest of the machine. By its own
documented rule -- "a component of a hyphenated slug never identifies the whole
project", written in Stage 13B because "bird" must not mean flappy-bird -- the
object "tracker" is a thing inside the current project. That rule is correct
and stays. What it cannot see is that "tracker" is also a component of a
DIFFERENT project's name, and that is the whole ambiguity.

So the check lives at the call site, which does know the project list, and it
is deliberately narrow: it fires only when the removed thing's tokens are a
SUBSET of some other project's name. "tracker" against `bug-tracker` is; "login
button" against `login-page` is not. Subset rather than substring, so the guard
cannot repeat the defect it guards against.

  I4   explicit project names override current-project context
  I15  denied destructive operations cannot silently reappear
  I34  destructive actions require the correct permission and target

Run:  venv\\Scripts\\python.exe tests\\test_s15_ambiguous_removal.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.event_bus import BUS  # noqa: E402

check = Checks()

ASKED = "rather ask than guess"


def seed(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


async def turn(nova, text: str) -> tuple[str, list, list]:
    """One real turn. Returns (reply, project.started, permission.requested)."""
    n0 = len(BUS.recent(800))
    res = await nova.brain.chat(text, conversation_id=str(uuid4()))
    new = BUS.recent(800)[n0:]
    for _ in range(80):
        pb = nova.runtime._project_builder
        if not any(pb.is_building(p) for p in pb.list_projects()):
            break
        await asyncio.sleep(0.05)
    return (str(res.assistant_text),
            [e for e in new if e.type == "project.started"],
            [e for e in new if e.type == "permission.requested"])


async def test_a_removal_that_could_mean_another_project_asks():
    check.section("I34 'delete tracker' with two *-tracker projects")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "cat-tracker")
        seed(nova, "bug-tracker")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="cat-tracker", confidence=0.99)

        for text in ("delete tracker", "delete the tracker", "remove tracker"):
            said, started, asked = await turn(nova, text)
            check(ASKED in said, f"{text!r}: Nova asks which ({said[:60]!r})")
            check("bug-tracker" in said,
                  f"{text!r}: naming the project it might have meant")
            check(not started,
                  f"{text!r}: and starts NO build ({len(started)})")
            check(not asked,
                  f"{text!r}: and raises no destructive permission "
                  f"({len(asked)})")

        # Nothing on disk moved.
        for n in ("cat-tracker", "bug-tracker"):
            check((nova.projects_dir / n / "PROJECT.md").exists(),
                  f"{n} is untouched")


async def test_stage_13bs_rule_is_untouched():
    """"bird" is not flappy-bird. That rule predates this guard and stays."""
    check.section("13B a component of the CURRENT project is still an edit")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        seed(nova, "blog")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)

        for text in ("delete bird", "delete the bird", "remove the bird"):
            said, started, _ = await turn(nova, text)
            check(ASKED not in said,
                  f"{text!r}: does NOT ask -- it is an edit ({said[:50]!r})")
            check(len(started) == 1,
                  f"{text!r}: and the improvement starts ({len(started)})")


async def test_an_ordinary_inside_project_removal_is_unaffected():
    check.section("an ordinary edit does not become a question")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "blog")
        seed(nova, "login-page")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="blog", confidence=0.99)

        # "login button" is not a subset of {login, page}: two tokens, one
        # shared. The guard must not fire on a partial overlap.
        for text in ("delete the login button", "delete the sidebar",
                     "remove the footer"):
            said, started, _ = await turn(nova, text)
            check(ASKED not in said,
                  f"{text!r}: no question ({said[:50]!r})")
            check(len(started) == 1,
                  f"{text!r}: the edit proceeds ({len(started)})")


async def test_the_object_only_matches_another_project():
    check.section("the removed thing names only a DIFFERENT project")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "blog")
        seed(nova, "bug-tracker")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="blog", confidence=0.99)

        said, started, asked = await turn(nova, "delete tracker")
        check(ASKED in said, f"Nova asks ({said[:60]!r})")
        check("bug-tracker" in said, "and names the project it might mean")
        check(not started and not asked,
              f"with no build and no permission request "
              f"({len(started)}, {len(asked)})")


async def test_naming_the_whole_project_still_defers_to_the_tool():
    """An unambiguous whole-project delete is the tool loop's job, not the
    pre-pass's. The guard must not swallow it."""
    check.section("an unambiguous delete still reaches the gated tool")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "cat-tracker")
        seed(nova, "bug-tracker")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="cat-tracker", confidence=0.99)

        reply = await nova.runtime._project_prepass("delete bug-tracker",
                                                    uuid4())
        check(reply is None,
              f"the pre-pass hands the turn to the tool loop ({reply!r})")
        reply2 = await nova.runtime._project_prepass("delete cat-tracker",
                                                     uuid4())
        check(reply2 is None,
              f"including for the current project ({reply2!r})")


async def main() -> None:
    await test_a_removal_that_could_mean_another_project_asks()
    await test_stage_13bs_rule_is_untouched()
    await test_an_ordinary_inside_project_removal_is_unaffected()
    await test_the_object_only_matches_another_project()
    await test_naming_the_whole_project_still_defers_to_the_tool()
    check.finish()


if __name__ == "__main__":
    run(main)
