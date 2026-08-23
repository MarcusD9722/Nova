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


async def test_select_then_act_actually_acts_through_chat():
    """The behavioural half of the select+action fix.

    Asserting the predicate is not enough: the defect was that selection is
    resolved BEFORE mutation in the turn path, so a sentence classified as a
    bare selection had its instruction dropped on the floor no matter what any
    regex said. Measured on 3278f39 through POST /chat — "Switch to calc-tool
    and add a memory button." switched project and edited nothing, while "Open
    calc-tool and add a memory button." worked, which is why sampling openers
    missed it.

    Every selection starter is exercised, and both halves are checked: the edit
    ran, AND it ran on the project the sentence named.
    """
    check.section("selection: 'switch to X and do Y' edits X, through /chat")

    async with boot() as nova:
        edits: list[str] = []
        nova.llm.when("You are Nova improving an existing project",
                      lambda p: edits.append(p) or "{}", label="improve-tripwire")
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.project_intent import SELECTION_STARTERS
        from core.tool_router import ToolCall

        pb = nova.runtime._project_builder
        for name in ("flappy-bird", "calc-tool"):
            await nova.runtime._router.execute(
                ToolCall("project.scaffold", {"name": name}))

        for starter in SELECTION_STARTERS:
            edits.clear()
            # Start each one from the OTHER project, so "it edited the named
            # project" cannot pass just because that project was already open.
            await pb.select("flappy-bird")
            cid = str(uuid4())
            text = (f"{starter[0].upper()}{starter[1:]} calc-tool and add a "
                    "memory button.")
            reply = await _chat(nova, cid, text)
            for _ in range(60):
                if edits:
                    break
                await asyncio.sleep(0.05)
            check(bool(edits), f"the instruction ran: {text!r} -> {reply[:50]!r}")
            check(await pb.last_active() == "calc-tool",
                  f"and on the project it named ({await pb.last_active()!r}): "
                  f"{text!r}")

        # …and the same starters with no second clause still only select.
        for starter in SELECTION_STARTERS[:6]:
            edits.clear()
            await pb.select("flappy-bird")
            cid = str(uuid4())
            text = f"{starter[0].upper()}{starter[1:]} calc-tool."
            await _chat(nova, cid, text)
            await asyncio.sleep(0.3)
            check(not edits, f"selection alone still edits nothing: {text!r}")
            check(await pb.last_active() == "calc-tool",
                  f"but does move the current project: {text!r}")


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


async def test_the_manager_and_builder_agree_in_every_case():
    """Four seeded states, and no surface may disagree about any of them.

    Review found the contract still split on the READ side after the write side
    was fixed. Measured on 3278f39 with a seeded `projects/orphan-dir/`:

        ProjectManager.list_projects()   listed it
        ProjectBuilder.list_projects()   did not
        known_slug_in_text()             could not name it
        status_text()                    "I don't have a project called that"
        select()                         refused
        last_active()                    returned it AS THE CURRENT PROJECT

    One object, six answers — and the one that decided what Nova was working on
    was the most permissive of them.
    """
    check.section("identity: the manager and the builder agree in every case")

    async with boot() as nova:
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.project_manager import ProjectManager
        from core.project_names import is_project_dir
        from core.tool_router import ToolCall

        pb = nova.runtime._project_builder
        mgr = ProjectManager(repo_root=nova.root, projects_dir=nova.projects_dir)

        # A. a real project
        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "real-one"}))
        # B. a raw directory with no identity document
        raw = nova.projects_dir / "orphan-dir"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "main.py").write_text("x = 1\n", encoding="utf-8")
        # D. a project that has been through delete/restore
        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "round-trip"}))
        info = mgr.delete_project("round-trip")
        mgr.restore_project(info["moved_to_trash"])

        for slug, is_project in (("real-one", True), ("orphan-dir", False),
                                 ("round-trip", True)):
            in_builder = slug in pb.list_projects()
            in_manager = slug in mgr.list_projects()
            by_predicate = is_project_dir(nova.projects_dir / slug)
            check(in_builder == in_manager == by_predicate == is_project,
                  f"{slug}: builder={in_builder} manager={in_manager} "
                  f"predicate={by_predicate}, expected {is_project}")

            named = pb.known_slug_in_text(f"open {slug}")
            check((named == slug) is is_project,
                  f"{slug}: conversational resolution agrees ({named!r})")

            chosen = await pb.select(slug)
            check((chosen == slug) is is_project,
                  f"{slug}: select agrees ({chosen!r})")

            unknown = "don't have a project" in pb.status_text(slug).lower()
            check(unknown is not is_project,
                  f"{slug}: status agrees ({pb.status_text(slug)[:50]!r})")

        # C. a stale pointer aimed at the identity-less directory. This is the
        #    one that used to escape: the pointer lives in memory and the
        #    project is a directory, and `last_active` only ever checked that
        #    the directory existed.
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="orphan-dir", confidence=0.95)
        current = await pb.last_active()
        check(current != "orphan-dir",
              f"a stale pointer at a non-project is not the current project "
              f"({current!r})")
        cid = str(uuid4())
        answer = await _chat(nova, cid, "What project are we working on?")
        check("orphan-dir" not in answer.lower(),
              f"and Nova does not name it either ({answer[:70]!r})")

        # …and the raw directory is still on disk, untouched.
        check((raw / "main.py").read_text(encoding="utf-8") == "x = 1\n",
              "nothing was migrated, renamed or deleted to reach agreement")


async def test_the_legacy_policy_is_explicit():
    """A directory that predates the identity document has ONE answer.

    Decided rather than left implicit: it is not a project on any surface, it is
    reported under its own name by `list_unadopted()`, and `adopt_project()`
    turns it into one by WRITING the marker. Adoption is additive and never
    happens as a side effect of a read — a listing that writes is exactly how
    "do not silently migrate Marcus's projects" gets violated by accident.
    """
    check.section("identity: legacy directories are named, not guessed at")

    async with boot() as nova:
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.project_manager import ProjectManager
        from core.tool_router import ToolCall

        pb = nova.runtime._project_builder
        mgr = ProjectManager(repo_root=nova.root, projects_dir=nova.projects_dir)
        await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": "real-one"}))
        legacy = nova.projects_dir / "ancient"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "notes.txt").write_text("years of work\n", encoding="utf-8")

        check(mgr.list_unadopted() == ["ancient"],
              f"the legacy directory is reported, under its own name "
              f"({mgr.list_unadopted()})")
        check("ancient" not in mgr.list_projects(),
              "and is not counted as a project")
        check("ancient" not in pb.list_projects(), "on either surface")

        # Listing is a READ. It must not have adopted anything.
        check(not (legacy / "PROJECT.md").exists(),
              "listing did not silently write an identity document")

        res = mgr.adopt_project("ancient")
        check(res["adopted"] is True, f"adoption reports what it did ({res})")
        check((legacy / "PROJECT.md").exists(), "and wrote the marker")
        check((legacy / "notes.txt").read_text(encoding="utf-8")
              == "years of work\n", "without touching the existing contents")
        check(legacy.name == "ancient", "and without renaming the directory")

        check("ancient" in pb.list_projects() and "ancient" in mgr.list_projects(),
              "now it is a project on both surfaces")
        check(mgr.list_unadopted() == [],
              f"and no longer unadopted ({mgr.list_unadopted()})")

        cid = str(uuid4())
        await _chat(nova, cid, "Open ancient.")
        check(await pb.last_active() == "ancient",
              f"and conversation can select it ({await pb.last_active()!r})")

        # Idempotent, and never clobbers a real history.
        (legacy / "PROJECT.md").write_text(
            "# ancient\n\n## Progress log\n- real\n", encoding="utf-8")
        again = mgr.adopt_project("ancient")
        check(again["adopted"] is False, f"adopting twice is a no-op ({again})")
        check("real" in (legacy / "PROJECT.md").read_text(encoding="utf-8"),
              "and the existing identity document survives")


async def test_selection_never_claims_success_it_did_not_achieve():
    """A swallowed pointer write turned into a confident lie.

    `_set_last_active` caught every persistence exception and `select()`
    returned the slug anyway, so Nova answered "Okay — we're on calc-tool now."
    while the authoritative pointer still said flappy-bird. The next turn then
    acted on the OLD project, which is what makes this more than cosmetic.
    """
    check.section("selection: a failed write is reported, not announced as success")

    async with boot() as nova:
        edits: list[str] = []
        nova.llm.when("You are Nova improving an existing project",
                      lambda p: edits.append(p) or "{}", label="improve-tripwire")
        nova.llm.when(lambda _p: True, lambda _p: "sure.", label="flat")
        from core.project_builder import ProjectStateError
        from core.tool_router import ToolCall

        pb = nova.runtime._project_builder
        for name in ("flappy-bird", "calc-tool"):
            await nova.runtime._router.execute(
                ToolCall("project.scaffold", {"name": name}))
        before_a, before_b = _tree(nova, "flappy-bird"), _tree(nova, "calc-tool")

        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        check(await pb.last_active() == "flappy-bird", "A is current")

        original = nova.memory.add_fact

        async def refuse_pointer_writes(*a, **k):
            if k.get("attribute") == "last_active":
                raise RuntimeError("simulated storage failure")
            return await original(*a, **k)

        nova.memory.add_fact = refuse_pointer_writes
        try:
            # The builder tells the two failures apart, so the turn path can too.
            raised = False
            try:
                await pb.select("calc-tool")
            except ProjectStateError:
                raised = True
            check(raised, "select() raises rather than reporting a phantom switch")
            check(await pb.select("no-such-project") is None,
                  "and still returns None for a project that does not exist")

            reply = await _chat(nova, cid, "Switch to calc-tool.")
        finally:
            nova.memory.add_fact = original

        low = reply.lower()
        check("couldn't switch" in low or "could not switch" in low,
              f"Nova says the switch failed ({reply[:80]!r})")
        check("we're on calc-tool now" not in low,
              f"and does not announce it as done ({reply[:80]!r})")
        check("flappy-bird" in low,
              f"and says where we actually still are ({reply[:80]!r})")
        check(await pb.last_active() == "flappy-bird",
              f"the old project is still authoritative "
              f"({await pb.last_active()!r})")
        check(_tree(nova, "flappy-bird") == before_a
              and _tree(nova, "calc-tool") == before_b,
              "and no project files changed")
        check(not edits, f"no edit was started either ({len(edits)})")


async def main():
    await test_selection_is_recognised()
    await test_selection_is_not_mutation()
    await test_a_mention_is_not_a_selection()
    await test_the_current_project_question_is_recognised()
    await test_select_then_act_actually_acts_through_chat()
    await test_switching_is_authoritative_end_to_end()
    await test_selecting_never_starts_an_edit()
    await test_naming_an_unknown_project_never_edits_the_open_one()
    await test_the_scaffolded_project_is_addressable_all_the_way_through()
    await test_both_creation_paths_agree()
    await test_the_manager_and_builder_agree_in_every_case()
    await test_the_legacy_policy_is_explicit()
    await test_selection_never_claims_success_it_did_not_achieve()
    check.finish()


if __name__ == "__main__":
    run(main)
