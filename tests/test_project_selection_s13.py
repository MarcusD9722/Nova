"""Current project: selection, switching, and ONE definition of "a project".

TWO Stage 13 requirements meet here, because they are the same seam.

1. WHICH PROJECT ARE WE ON. Nova could resolve a project named inside a single
   message, but "Let's work on the calculator project." left no trace: there was
   no authoritative current project, so `projects/last_active` stayed empty and
   the next turn had nothing to resume. Selection is now its own intent —
   NON-MUTATING, deliberately not gated by `authorize_project_mutation`, because
   choosing what to look at is not permission to change it.

2. WHAT COUNTS AS A PROJECT. There were two disagreeing answers.
   `ProjectManager` treated any workspace directory as a project;
   `ProjectBuilder.list_projects()` counted only directories containing
   PROJECT.md. So a project created by `project.scaffold` existed on the tool
   surface and did not exist to conversation — unnameable, unstatusable,
   unselectable, unresumable. That is an integration seam, not a test
   inconvenience, and it is closed by making PROJECT.md the identity document
   that EVERY creation path writes. No third registry was introduced and no
   existing directory is renamed or migrated.

WHY THESE ARE ASSERTED TOGETHER: a selection test that scaffolds its fixture
would have silently exercised only the half of the contract that already
worked.

Run:  venv\\Scripts\\python.exe tests\\test_project_selection_s13.py
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

from core.project_intent import (  # noqa: E402
    asks_current_project,
    authorize_project_mutation,
    is_project_selection,
)

check = Checks()


def _allowed(text: str) -> bool:
    return bool(authorize_project_mutation(text, complaint=False).allowed)


# ── the predicate, on its own ────────────────────────────────────────────────

async def test_selection_is_recognised():
    check.section("selection: the forms people actually use")

    for text in (
        "Let's work on the calculator project.",
        "Let's work on flappy-bird.",
        "Switch to calc-tool.",
        "Switch over to flappy-bird.",
        "Go back to Flappy Bird.",
        "Open Flappy Bird.",
        "Pull up the calculator.",
        "Bring up flappy-bird.",
        "Load the calculator project.",
        "Back to flappy-bird.",
        "Okay, let's go back to the calculator.",
        "Now switch to flappy-bird.",
        "Jump back into flappy-bird.",
        "Let us look at the calculator.",
    ):
        check(is_project_selection(text), f"selection: {text!r}")


async def test_selection_is_not_mutation():
    """The point of a separate intent.

    If selection were routed through `authorize_project_mutation`, either it
    would never work (the gate refuses most of these, correctly — they instruct
    no change) or the gate would have to be loosened, which is the defect class
    this program spent four rounds closing. So the gate is left alone and
    selection is decided separately.

    "Let's work on X" is the interesting one and is called out rather than
    smoothed over: `work on` IS in the action vocabulary, so the message-level
    gate allows it. What makes it non-mutating is ORDER — the prepass resolves
    selection before it considers mutation, so a bare "let's work on <project>"
    opens the project and waits. `test_selecting_never_starts_an_edit` proves
    that behaviourally, because a claim about ordering is worth nothing as an
    assertion about a regex.
    """
    check.section("selection: choosing a project is not permission to change it")

    for text in ("Switch to calc-tool.",
                 "Go back to Flappy Bird.",
                 "Open Flappy Bird.",
                 "Pull up the calculator.",
                 "Back to flappy-bird."):
        check(is_project_selection(text), f"is a selection: {text!r}")
        check(not _allowed(text), f"and authorises no mutation: {text!r}")

    check(is_project_selection("Let's work on the calculator project."),
          "'let's work on X' is a selection")
    check(_allowed("Let's work on the calculator project."),
          "and the message-level gate does allow it — order is what protects it")


async def test_a_mention_is_not_a_selection():
    """Naming a project is not asking to move to it."""
    check.section("selection: a casual mention selects nothing")

    for text in (
        "The calculator was fiddly to get right, honestly.",
        "I think my Flappy Bird project is finally starting to look decent.",
        "I've been thinking about improving the Flappy Bird project.",
        "Flappy Bird is harder than it looks.",
        "I don't want to go back to flappy-bird.",
        "Should we switch to the calculator?",
        "I wasn't asking you to open flappy-bird.",
        "Don't open the calculator.",
        "Open the flappy-bird project and add a pause button.",
    ):
        check(not is_project_selection(text), f"not a selection: {text!r}")


async def test_the_current_project_question_is_recognised():
    check.section("selection: asking which project we are on")

    for text in ("What project are we working on?",
                 "Which project are we on?",
                 "What project am I working on?",
                 "What's the current project?",
                 "What is the active project?",
                 "Which one are we working on?",
                 "Which project were we on?"):
        check(asks_current_project(text), f"is the question: {text!r}")

    for text in ("What project should I start next?",
                 "Tell me about the calculator project.",
                 "Open Flappy Bird."):
        check(not asks_current_project(text), f"is not the question: {text!r}")


# ── the same thing, through the real turn path ──────────────────────────────

async def _chat(nova, cid: str, text: str) -> str:
    r = await nova.http.post("/chat", json={"message": text,
                                            "conversation_id": cid})
    assert r.status_code == 200, f"/chat {r.status_code}: {r.text[:200]}"
    return str(r.json().get("assistant") or "")


def _tree(nova, slug: str) -> dict[str, str]:
    root = nova.projects_dir / slug
    if not root.exists():
        return {}
    return {str(p.relative_to(root)).replace("\\", "/"):
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*")) if p.is_file()}


async def test_switching_is_authoritative_end_to_end():
    """A -> B -> A, across unrelated conversation, asserted on STORED state.

    Not on the reply text: prose saying "sure, calculator" while nothing was
    recorded is exactly the failure this closes.
    """
    check.section("selection: A -> B -> A survives unrelated conversation")

    async with boot() as nova:
        # A tripwire on the edit orchestration itself. "Nothing changed on disk"
        # is not the same claim as "no edit was attempted": a stubbed plan that
        # fails to parse also leaves the disk clean, so the disk alone would let
        # a selection-starts-an-edit regression through.
        edits: list[str] = []
        nova.llm.when("You are Nova improving an existing project",
                      lambda p: edits.append(p) or "{}", label="improve-tripwire")
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.tool_router import ToolCall
        pb = nova.runtime._project_builder
        for name in ("flappy-bird", "calc-tool"):
            res = await nova.runtime._router.execute(
                ToolCall("project.scaffold", {"name": name}))
            check(res.ok, f"scaffolded {name} ({res.error})")

        before_a, before_b = _tree(nova, "flappy-bird"), _tree(nova, "calc-tool")
        cid = str(uuid4())

        check(await pb.last_active() in (None, ""),
              "nothing is current before anything is chosen")

        await _chat(nova, cid, "Open Flappy Bird.")
        check(await pb.last_active() == "flappy-bird",
              f"A is current ({await pb.last_active()!r})")

        for filler in ("What's the weather like where you are?",
                       "I had a long day.",
                       "Do you ever get tired of me asking things?"):
            await _chat(nova, cid, filler)
        check(await pb.last_active() == "flappy-bird",
              "and unrelated conversation does not move it")

        # A nickname is not a slug. `known_slug_in_text` is literal, so
        # "the calculator project" names nothing when the project is
        # `calc-tool`. Nickname resolution is a real gap (Class G) — what
        # matters here is that the unresolved name changes nothing and,
        # crucially, redirects nothing. See
        # `test_naming_an_unknown_project_never_edits_the_open_one`.
        unknown = await _chat(nova, cid, "Let's work on the calculator project.")
        check(await pb.last_active() == "flappy-bird",
              f"an unresolvable name changes nothing "
              f"({await pb.last_active()!r})")
        check("calc-tool" in unknown.lower(),
              f"and Nova says what she does have ({unknown[:70]!r})")

        await _chat(nova, cid, "Switch to calc-tool.")
        check(await pb.last_active() == "calc-tool",
              f"B is current ({await pb.last_active()!r})")

        await _chat(nova, cid, "Anyway, my coffee machine died this morning.")
        mention = await _chat(nova, cid,
                              "Flappy Bird was fiddly to get right, honestly.")
        check(await pb.last_active() == "calc-tool",
              f"naming A in passing did not switch to it "
              f"({await pb.last_active()!r}) — reply was {mention[:40]!r}")

        await _chat(nova, cid, "Go back to Flappy Bird.")
        check(await pb.last_active() == "flappy-bird",
              f"and back to A ({await pb.last_active()!r})")

        answer = await _chat(nova, cid, "What project are we working on?")
        check("flappy-bird" in answer.lower(),
              f"the question resolves to the authoritative project "
              f"({answer[:70]!r})")
        check("calc-tool" not in answer.lower(),
              f"and not to the other one ({answer[:70]!r})")

        check(_tree(nova, "flappy-bird") == before_a,
              "selecting A four times changed nothing inside it")
        check(_tree(nova, "calc-tool") == before_b,
              "and nothing inside B either")
        check(not edits,
              f"and no edit was ever ATTEMPTED, not merely none completed "
              f"({len(edits)} improve calls)")


async def test_selecting_never_starts_an_edit():
    """"Let's work on X" opens the project. It does not start rewriting it.

    This is the behavioural half of `test_selection_is_not_mutation`: the phrase
    passes the message-level mutation gate, so the only thing standing between
    it and an unrequested edit is the prepass resolving selection first.
    """
    check.section("selection: 'let's work on X' opens, it does not edit")

    async with boot() as nova:
        edits: list[str] = []
        nova.llm.when("You are Nova improving an existing project",
                      lambda p: edits.append(p) or "{}", label="improve-tripwire")
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.tool_router import ToolCall

        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "calc-tool"}))
        pb = nova.runtime._project_builder
        before = _tree(nova, "calc-tool")

        cid = str(uuid4())
        reply = await _chat(nova, cid, "Let's work on calc-tool.")
        check(await pb.last_active() == "calc-tool",
              f"it became the current project ({await pb.last_active()!r})")
        check(not edits, f"and started no edit ({len(edits)} improve calls)")
        check(_tree(nova, "calc-tool") == before,
              f"and wrote nothing ({reply[:50]!r})")

        # …and once a change is actually instructed, the edit DOES start, so
        # the assertion above is about this sentence and not about a path that
        # is dead for everyone.
        await _chat(nova, cid, "Add a memory button to calc-tool.")
        for _ in range(200):
            if edits:
                break
            await __import__("asyncio").sleep(0.05)
        check(bool(edits),
              "an actual instruction still reaches the edit orchestration")


async def test_naming_an_unknown_project_never_edits_the_open_one():
    """A Class A defect this suite found, pinned.

    Measured before the fix, with flappy-bird open:

        "Let's work on the calculator project."
            -> "Got it — working on those improvements to flappy-bird now."

    No project resolved from the message, so the turn path substituted the
    LAST-ACTIVE project as the mutation target and started an autonomous
    improve of it. The user named one project and a different one was edited —
    the worst possible reading of an ambiguous sentence.

    The substitution itself is right for "continue where we left off", which is
    why it is not simply removed. What is now distinguished is whether the
    message NAMED a project: "the project" may borrow the current one, "the
    calculator project" may not.
    """
    check.section("selection: naming an unknown project edits nothing")

    async with boot() as nova:
        edits: list[str] = []
        nova.llm.when("You are Nova improving an existing project",
                      lambda p: edits.append(p) or "{}", label="improve-tripwire")
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.tool_router import ToolCall

        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "flappy-bird"}))
        pb = nova.runtime._project_builder
        before = _tree(nova, "flappy-bird")
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        check(await pb.last_active() == "flappy-bird", "flappy-bird is open")

        for text in ("Let's work on the calculator project.",
                     "Improve the calculator project.",
                     "Switch to the calculator project.",
                     "Add a memory button to the calculator project."):
            reply = await _chat(nova, cid, text)
            check(not edits,
                  f"no edit started by {text!r} ({len(edits)} improve calls)")
            check(_tree(nova, "flappy-bird") == before,
                  f"and the open project is untouched by {text!r}")
            check("calculator" in reply.lower(),
                  f"and Nova names what she could not find ({reply[:60]!r})")

        # The generic form still means "the one we're on", so the fix did not
        # simply disable the fall-back it narrows.
        await _chat(nova, cid, "Add a pause button to the project.")
        for _ in range(200):
            if edits:
                break
            await __import__("asyncio").sleep(0.05)
        check(bool(edits),
              '"the project" still resolves to the open one and edits it')


async def test_the_scaffolded_project_is_addressable_all_the_way_through():
    """`project.scaffold` -> resolve -> status -> select -> resume -> delete/restore.

    Every one of these consumers used to disagree with the tool that created
    the project. This walks the whole chain on ONE project.
    """
    check.section("identity: one contract from scaffold to restore")

    async with boot() as nova:
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.tool_router import ToolCall
        pb = nova.runtime._project_builder
        pm = nova.runtime._router  # scaffold goes through the same router

        res = await pm.execute(ToolCall("project.scaffold", {"name": "calc-tool"}))
        check(res.ok, f"scaffold ok ({res.error})")

        # 1. the identity document exists, written by the creation path itself
        md = nova.projects_dir / "calc-tool" / "PROJECT.md"
        check(md.exists(), "scaffold wrote PROJECT.md")
        body = md.read_text(encoding="utf-8")
        for section in ("## Brief", "## Status", "## Summary", "## Files",
                        "## Progress log"):
            check(section in body, f"PROJECT.md has {section}")

        # 2. conversation's universe contains it
        check("calc-tool" in pb.list_projects(),
              f"ProjectBuilder can see it ({pb.list_projects()})")
        check(pb.known_slug_in_text("bring up calc-tool") == "calc-tool",
              "and chat can resolve its name")

        # 3. status reports it honestly rather than as unknown
        st = await pm.execute(ToolCall("project.status", {"name": "calc-tool"}))
        check(st.ok, f"project.status ok ({st.error})")
        text = str(st.result.get("status", "")).lower()
        check("unknown" not in text,
              f"status is not 'unknown' for a real project ({text[:80]!r})")
        check("scaffolded" in text, f"it reports its real state ({text[:80]!r})")

        # 4. it can be selected in conversation, and resumed
        cid = str(uuid4())
        await _chat(nova, cid, "Open calc-tool.")
        check(await pb.last_active() == "calc-tool",
              f"selectable in chat ({await pb.last_active()!r})")
        resume = await _chat(nova, cid, "Where were we on calc-tool?")
        check(bool(resume.strip()), "and resumable")

        # 5. delete/restore preserves the identity, document and all.
        #    Called on ProjectManager directly: `project.delete` is ADMIN-gated
        #    and the gate has its own coverage — what is under test here is
        #    whether the round trip keeps the project addressable.
        from core.project_manager import ProjectManager
        mgr = ProjectManager(repo_root=nova.root, projects_dir=nova.projects_dir)
        info = mgr.delete_project("calc-tool")
        check(info["project"] == "calc-tool", f"deleted ({info['project']!r})")
        check("calc-tool" not in pb.list_projects(),
              f"and conversation no longer sees it ({pb.list_projects()})")

        back = mgr.restore_project(info["moved_to_trash"])
        check(back["restored"] == "calc-tool",
              f"restored under its own name ({back['restored']!r})")
        check("calc-tool" in pb.list_projects(),
              f"and conversation sees it again ({pb.list_projects()})")
        check((nova.projects_dir / "calc-tool" / "PROJECT.md").read_text(
            encoding="utf-8") == body,
            "with the identity document byte-identical")

        cid2 = str(uuid4())
        await _chat(nova, cid2, "Open calc-tool.")
        check(await pb.last_active() == "calc-tool",
              "and it is selectable again after the round trip")


async def test_both_creation_paths_agree():
    """The regression test the split contract needed and never had.

    A project can be created by the scaffolding tool or by any code path that
    calls `ensure_workspace`. Before the fix these produced directories that
    ProjectBuilder disagreed about. Both are checked, because fixing only the
    one the tests happened to use is how the split survived in the first place.
    """
    check.section("identity: every creation path produces a real project")

    async with boot() as nova:
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.project_manager import ProjectManager
        from core.tool_router import ToolCall

        mgr = ProjectManager(repo_root=nova.root, projects_dir=nova.projects_dir)
        pb = nova.runtime._project_builder

        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "via-scaffold"}))
        mgr.ensure_workspace("via-workspace")

        for slug in ("via-scaffold", "via-workspace"):
            check((nova.projects_dir / slug / "PROJECT.md").exists(),
                  f"{slug}: has the identity document")
            check(slug in pb.list_projects(),
                  f"{slug}: visible to conversation ({pb.list_projects()})")
            check(slug in mgr.list_projects(),
                  f"{slug}: visible to the manager ({mgr.list_projects()})")
            check(pb.known_slug_in_text(f"open {slug}") == slug,
                  f"{slug}: resolvable by name")

        check(sorted(pb.list_projects()) == sorted(mgr.list_projects()),
              f"the two views are the SAME universe "
              f"({pb.list_projects()} vs {mgr.list_projects()})")

        # An existing PROJECT.md is never overwritten — a real one carries the
        # build history, and clobbering it to satisfy a contract would destroy
        # exactly what the contract exists to protect.
        real = nova.projects_dir / "via-scaffold" / "PROJECT.md"
        real.write_text("# via-scaffold\n\n## Progress log\n- real history\n",
                        encoding="utf-8")
        mgr.ensure_workspace("via-scaffold")
        check("real history" in real.read_text(encoding="utf-8"),
              "ensure_workspace did not clobber an existing PROJECT.md")


async def main():
    await test_selection_is_recognised()
    await test_selection_is_not_mutation()
    await test_a_mention_is_not_a_selection()
    await test_the_current_project_question_is_recognised()
    await test_switching_is_authoritative_end_to_end()
    await test_selecting_never_starts_an_edit()
    await test_naming_an_unknown_project_never_edits_the_open_one()
    await test_the_scaffolded_project_is_addressable_all_the_way_through()
    await test_both_creation_paths_agree()
    check.finish()


if __name__ == "__main__":
    run(main)
