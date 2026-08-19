"""PR #59 Round 2: ONE project identity contract, and a case-blind name boundary.

Two things this file exists to prevent.

CAPITALISATION IS NOT EVIDENCE. The previous boundary rule decided title-vs-
requirement by case: "Rock and Roll Tracker" looked like a title, "Serpent and use
Python" looked like a requirement. Nova is voice-first. An STT transcript does not
preserve intentional title case, so "create a project called rock and roll tracker"
would have been truncated to "rock" purely because it was spoken. Changing ONLY
capitalisation must never change which words belong to the name.

TWO SUBSYSTEMS MUST NOT DEFINE ONE NAMESPACE. `ProjectBuilder.slugify` and
`ProjectManager._sanitize` were independent: one lowercase/ASCII/48-capped with
Win32 remapping, the other case-preserving, dot- and underscore-allowing, uncapped
and unprotected. So `project.start_build` and `project.scaffold` could create two
different directories for one human name, and only one path was safe from
`CON`/`NUL`/`COM1`.

Run:  venv\\Scripts\\python.exe tests\\test_project_identity_p10c2.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.project_builder import (  # noqa: E402
    NAME_AMBIGUOUS, NEEDS_NAME, ProjectBuilder, resolve_name_boundary, slugify,
)
from core.project_builder import _MAX_RAW_NAME as _MAX_RAW  # noqa: E402
from core.project_manager import ProjectManager  # noqa: E402
from core.project_names import (  # noqa: E402
    MAX_SLUG_LEN, WIN_RESERVED, canonical_project_slug, safe_trash_entry,
)

check = Checks()
extract = ProjectBuilder.extract_start_request


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _name(text):
    got = extract(text)
    return None if got is None else got[0]


def _variants(phrase: str) -> list[tuple[str, str]]:
    """The same words in four capitalisations."""
    return [
        ("original", phrase),
        ("lower", phrase.lower()),
        ("UPPER", phrase.upper()),
        ("Sentence", phrase[:1].upper() + phrase[1:].lower()),
    ]


# ── A. CAPITALISATION INVARIANCE ─────────────────────────────────────────────
async def test_case_invariance_of_the_name_boundary():
    check.section("A: changing ONLY case never changes the boundary decision")

    # Each entry: the name span, and how many words the boundary should keep.
    # Expectations are expressed as a WORD COUNT so they survive case changes.
    cases = [
        ("Rock and Roll Tracker", 4, "clean title"),
        ("Man with a Plan", NAME_AMBIGUOUS, "ambiguous title"),
        ("Python for Beginners", NAME_AMBIGUOUS, "ambiguous title"),
        ("Apps Using AI", NAME_AMBIGUOUS, "ambiguous title"),
        ("Serpent and use Python", 1, "requirement suffix"),
        ("Serpent and add levels", 1, "requirement suffix"),
        ("Serpent then add a scoreboard", 1, "requirement suffix"),
        ("Serpent and keep it offline", 1, "requirement suffix"),
        ("Serpent with a dark theme", NAME_AMBIGUOUS, "ambiguous requirement"),
        ("War and Peace Notes", 4, "clean title"),
        ("To Do List", 3, "clean title"),
        ("Balloon Tower Defense", 3, "clean title"),
    ]

    for phrase, expect, label in cases:
        results = {}
        for vlabel, variant in _variants(phrase):
            out = resolve_name_boundary(variant)
            results[vlabel] = (NAME_AMBIGUOUS if out == NAME_AMBIGUOUS
                               else len(out.split()))
        distinct = set(results.values())
        check(len(distinct) == 1,
              f"{label} {phrase!r}: all four capitalisations agree "
              f"({results})")
        check(results["original"] == expect,
              f"{label} {phrase!r}: boundary is {results['original']}, "
              f"expected {expect}")

    # The exact regression the review named: a lowercase legitimate title.
    lower = resolve_name_boundary("rock and roll tracker")
    check(lower == "rock and roll tracker",
          f"a lowercase legitimate title is NOT truncated ({lower!r})")
    check(resolve_name_boundary("ROCK AND ROLL TRACKER") == "ROCK AND ROLL TRACKER",
          "and neither is an uppercase one")


async def test_case_invariance_end_to_end():
    check.section("A: the same invariance through extract_start_request")

    for phrase in ("create a project called Rock and Roll Tracker",
                   "create a project called Serpent and use Python",
                   "create a project called Serpent with a dark theme",
                   "create a project called Man with a Plan"):
        outs = {}
        for vlabel, variant in _variants(phrase):
            got = _name(variant)
            # Compare shape, not literal text: case differs by construction.
            outs[vlabel] = (got if got in (None, NEEDS_NAME)
                            else len(got.split()))
        check(len(set(outs.values())) == 1,
              f"{phrase!r}: all capitalisations agree ({outs})")

    # Requirement suffixes are cut in every casing.
    for variant in ("create a project called Serpent and use Python",
                    "create a project called serpent and use python",
                    "CREATE A PROJECT CALLED SERPENT AND USE PYTHON"):
        got = _name(variant)
        check(got is not None and got != NEEDS_NAME and len(got.split()) == 1,
              f"{variant!r} -> a one-word name ({got!r})")


async def test_ambiguous_names_ask_rather_than_guess():
    check.section("A: genuinely ambiguous boundaries FAIL CLOSED")

    for text in ("create a project called Serpent with a dark theme",
                 "create a project called Man with a Plan",
                 "create a project called Python for Beginners",
                 "create a project called apps using ai",
                 "create a project called Serpent that tracks spending"):
        got = _name(text)
        check(got == NEEDS_NAME,
              f"{text!r} -> asks for clarification ({got!r})")

    # Quoting removes the ambiguity, and is honoured exactly.
    for text, want in (
        ('create a project called "Man with a Plan" and use Python',
         "Man with a Plan"),
        ('create a project called "Serpent with a dark theme"',
         "Serpent with a dark theme"),
        ("create a project called 'Python for Beginners' using Rust",
         "Python for Beginners"),
    ):
        check(_name(text) == want, f"{text!r} -> {_name(text)!r} (want {want!r})")


# ── B. SINGLE IDENTITY OWNER ─────────────────────────────────────────────────
async def test_one_canonical_identity_owner():
    check.section("B: one module owns live-project identity")

    for raw, want in [
        ("Balloon Tower Defense", "balloon-tower-defense"),
        ("  Balloon   Tower  Defense  ", "balloon-tower-defense"),
        ("BALLOON TOWER DEFENSE", "balloon-tower-defense"),
        ("balloon_tower_defense", "balloon-tower-defense"),
        ("balloon.tower.defense", "balloon-tower-defense"),
        ("Balloon---Tower---Defense", "balloon-tower-defense"),
        ("émoji café", "moji-caf"),
        ("CON", "project-con"),
        ("", "untitled"),
    ]:
        got = canonical_project_slug(raw)
        check(got == want, f"{raw!r} -> {got!r} (want {want!r})")
        check(bool(re.fullmatch(r"[a-z0-9-]+", got)),
              f"{raw!r} -> matches ^[a-z0-9-]+$ ({got!r})")
        check(len(got) <= MAX_SLUG_LEN, f"{raw!r} -> within {MAX_SLUG_LEN}")

    # slugify is now only a wrapper.
    for raw, label in [("Balloon Tower Defense", "normal"), ("CON", "reserved"),
                       ("a" * 200, "very long"), ("../escape", "traversal"),
                       ("🎈", "emoji")]:
        check(slugify(raw) == canonical_project_slug(raw),
              f"slugify delegates for the {label} case")

    # Builder and Manager must AGREE for the same human name.
    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)
        for raw in ["Balloon Tower Defense", "CON", "NUL", "COM1",
                    "My Personal Finance Dashboard 2026"]:
            builder_id = slugify(raw)
            manager_id = pm.project_path(raw).name
            check(builder_id == manager_id,
                  f"{raw!r}: Builder {builder_id!r} == Manager {manager_id!r}")

    # Trash keeps its own contract (timestamps are not slugs).
    entry = "balloon-tower-defense--20260818-062122"
    check(safe_trash_entry(entry) == entry,
          f"a trash id survives its own sanitizer ({safe_trash_entry(entry)!r})")
    check(canonical_project_slug(entry) != entry,
          "and is NOT what the live contract would produce")


async def test_legacy_directories_are_preserved():
    check.section("B: legacy non-canonical directories still resolve")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        # A directory the OLD ProjectManager._sanitize could have created.
        legacy = projects / "My_Old.Project"
        legacy.mkdir(parents=True)
        (legacy / "PROJECT.md").write_text("# legacy\n", encoding="utf-8")

        pm = ProjectManager(repo_root=root, projects_dir=projects)
        resolved = pm.project_path("My_Old.Project")
        check(resolved.name == "My_Old.Project",
              f"an existing legacy directory resolves to ITSELF ({resolved.name!r})")
        check(resolved.is_dir() and (resolved / "PROJECT.md").exists(),
              "and its contents are reachable")
        check(legacy.exists(),
              "it is NOT renamed or destroyed to satisfy the new contract")

        # A NEW name gets the canonical contract.
        fresh = pm.project_path("My New.Project")
        check(fresh.name == "my-new-project",
              f"a new name is canonicalised ({fresh.name!r})")


# ── C. REAL TOOLROUTER / WINDOWS ─────────────────────────────────────────────
class _StubLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def generate(self, *a, **k):
        raise AssertionError("scaffold/delete must not call the model")


async def _runtime(td):
    from core.runtime import RuntimeManager
    from core.tooling import build_tool_router
    from memory.unifier import MemoryUnifier

    root = Path(td)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    mem_dir = root / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    m = MemoryUnifier(mem_dir)
    await m.initialize()
    router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
    rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                        llm=_StubLLM(), router=router, memory_dir=mem_dir)
    return rt, m, projects


async def test_scaffold_through_the_real_router():
    check.section("C: project.scaffold through the real ToolRouter")

    from core.tool_router import ToolCall

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        check("project.scaffold" in rt._router.list_tools(),
              f"project.scaffold is registered ({'project.scaffold' in rt._router.list_tools()})")

        for raw in ["CON", "NUL", "COM1", "Balloon Tower Defense"]:
            res = await rt._router.execute(
                ToolCall(name="project.scaffold", args={"name": raw}), retries=0)
            check(res.ok, f"{raw!r}: scaffold succeeded ({res.error!r})")

            want = canonical_project_slug(raw)
            made = [p.name for p in projects.iterdir()
                    if p.is_dir() and not p.name.startswith(".")]
            check(want in made, f"{raw!r} -> canonical directory {want!r} ({made})")
            check(want not in WIN_RESERVED,
                  f"{raw!r} -> {want!r} is not a Win32 device name")

            d = projects / want
            (d / "PROJECT.md").write_text(f"# {raw}\n", encoding="utf-8")
            check((d / "PROJECT.md").read_text(encoding="utf-8").strip() == f"# {raw}",
                  f"{raw!r}: files write and read back")
            check(str(d.resolve()).startswith(str(projects.resolve())),
                  f"{raw!r}: stays inside the projects directory")


# ── D. NATURAL-NAME DELETE ───────────────────────────────────────────────────
async def _approve_next(rt, tries=40):
    for _ in range(tries):
        p = rt.permission_broker.pending()
        if p:
            rt.permission_broker.resolve(p[0]["request_id"], True, by="marcus")
            return True
        await asyncio.sleep(0.02)
    return False


async def test_delete_by_natural_name_clears_the_pointer():
    check.section("D: delete uses the ACTUAL identity, not the raw argument")

    from core.tool_router import ToolCall

    for spoken in ["Balloon Tower Defense", "balloon tower defense",
                   "  Balloon   Tower Defense  "]:
        with _tmp() as td:
            rt, m, projects = await _runtime(td)
            slug = canonical_project_slug("Balloon Tower Defense")
            (projects / slug).mkdir(parents=True)
            (projects / slug / "main.py").write_text("print(1)\n", encoding="utf-8")
            await m.add_fact(entity="projects", attribute="last_active",
                             value=slug, confidence=0.95)
            await m.add_fact(entity=f"project:{slug}", attribute="brief",
                             value="a tower defense game", confidence=0.95)

            task = asyncio.create_task(rt._router.execute(
                ToolCall(name="project.delete", args={"name": spoken}), retries=0))
            check(await _approve_next(rt), f"{spoken!r}: permission requested")
            res = await task

            check(res.ok, f"{spoken!r}: delete succeeded ({res.error!r})")
            check((res.result or {}).get("project") == slug,
                  f"{spoken!r}: result names the canonical identity "
                  f"({(res.result or {}).get('project')!r})")
            check(not (projects / slug).exists(),
                  f"{spoken!r}: the correct live project is gone")

            ptr = await m.get_latest_fact(entity="projects", attribute="last_active")
            check((ptr.value if ptr else "") == "",
                  f"{spoken!r}: last_active cleared ({ptr.value if ptr else None!r})")

            hist = await m.get_latest_fact(entity=f"project:{slug}", attribute="brief")
            check(hist is not None and hist.value == "a tower defense game",
                  f"{spoken!r}: historical memory retained")

            entries = [p.name for p in (projects / ".trash").glob("*")]
            check(len(entries) == 1 and entries[0].startswith(slug),
                  f"{spoken!r}: exactly one recoverable trash entry ({entries})")


# ── E. STALE POINTER ─────────────────────────────────────────────────────────
async def test_stale_pointer_is_never_returned():
    check.section("E: a stale active-project pointer is never used")

    from core.tool_router import ToolCall

    # E1: the memory clear FAILS, but the files are gone.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        slug = "balloon-tower-defense"
        (projects / slug).mkdir(parents=True)
        (projects / slug / "main.py").write_text("print(1)\n", encoding="utf-8")
        await m.add_fact(entity="projects", attribute="last_active",
                         value=slug, confidence=0.95)

        real_add = m.add_fact

        async def failing_add(**kw):
            if kw.get("entity") == "projects" and kw.get("attribute") == "last_active":
                raise RuntimeError("injected memory failure")
            return await real_add(**kw)

        m.add_fact = failing_add
        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "Balloon Tower Defense"}),
            retries=0))
        await _approve_next(rt)
        res = await task
        m.add_fact = real_add

        check(res.ok and not (projects / slug).exists(),
              "E1 the filesystem delete is still reported as successful")
        check("warning" in (res.result or {}),
              f"E1 with a non-fatal cleanup warning "
              f"({(res.result or {}).get('warning')!r})")
        ptr = await m.get_latest_fact(entity="projects", attribute="last_active")
        check((ptr.value if ptr else "") == slug,
              "E1 the stale pointer really is still stored")

        pb = rt._project_builder
        active = await pb.last_active()
        check(active is None,
              f"E1 but last_active() REFUSES to return it ({active!r})")

    # E2: a project removed behind Nova's back.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        (projects / "ghost").mkdir(parents=True)
        await m.add_fact(entity="projects", attribute="last_active",
                         value="ghost", confidence=0.95)
        pb = rt._project_builder
        check(await pb.last_active() == "ghost",
              "E2 a live project is returned normally")

        import shutil
        shutil.rmtree(projects / "ghost")
        check(await pb.last_active() is None,
              "E2 once the directory is gone, last_active() returns None")
        ptr = await m.get_latest_fact(entity="projects", attribute="last_active")
        check((ptr.value if ptr else "") == "",
              "E2 and it best-effort heals the stale pointer")


# ── F. TARGETED IDENTITY LIFECYCLE ───────────────────────────────────────────
async def test_identity_lifecycle():
    check.section("F: one human name through the whole lifecycle")

    from core.tool_router import ToolCall

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        human = "Balloon Tower Defense"
        slug = canonical_project_slug(human)
        check(slug == "balloon-tower-defense", f"1. canonical identity ({slug})")

        res = await rt._router.execute(
            ToolCall(name="project.scaffold", args={"name": human}), retries=0)
        check(res.ok, f"2. scaffold ({res.error!r})")

        listed = [p.name for p in projects.iterdir()
                  if p.is_dir() and not p.name.startswith(".")]
        check(slug in listed, f"3. listed under the canonical id ({listed})")

        await m.add_fact(entity="projects", attribute="last_active",
                         value=slug, confidence=0.95)
        await m.add_fact(entity=f"project:{slug}", attribute="brief",
                         value="a tower defense game", confidence=0.95)
        check(await rt._project_builder.last_active() == slug,
              "4. last_active established and verified live")

        check((projects / slug).is_dir(), "5. readable on disk")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "BALLOON tower defense"}),
            retries=0))
        await _approve_next(rt)
        res = await task
        entry = (res.result or {}).get("moved_to_trash", "")
        check(res.ok and (res.result or {}).get("project") == slug,
              f"6. deleted by a casing variant, canonical identity reported "
              f"({(res.result or {}).get('project')!r})")
        check(bool(entry) and (projects / ".trash" / entry).is_dir(),
              f"7. trash entry exists ({entry!r})")
        check(await rt._project_builder.last_active() is None,
              "8. active pointer invalidated")
        hist = await m.get_latest_fact(entity=f"project:{slug}", attribute="brief")
        check(hist is not None and hist.value == "a tower defense game",
              "9. historical memory retained")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.restore", args={"entry": entry}), retries=0))
        await _approve_next(rt)
        res = await task
        check(res.ok, f"10. restored ({res.error!r})")
        check((projects / slug).is_dir(),
              f"11. same canonical live identity restored ({slug})")


# ── ROUND 3 ─────────────────────────────────────────────────────────────────
async def test_active_build_guard_uses_resolved_identity():
    """BUG A: the guard compared the RAW argument to canonical builder slugs.

    `active_projects()` holds `balloon-tower-defense`; a delete request says
    "Balloon Tower Defense". The two never matched, so a delete could proceed
    while the builder was still writing files.
    """
    check.section("Round 3 A: delete cannot race an active build")

    from core.tool_router import ToolCall

    for spoken in ["Balloon Tower Defense", "balloon tower defense",
                   "BALLOON TOWER DEFENSE"]:
        with _tmp() as td:
            rt, m, projects = await _runtime(td)
            slug = canonical_project_slug("Balloon Tower Defense")
            (projects / slug).mkdir(parents=True)
            (projects / slug / "main.py").write_text("print(1)\n", encoding="utf-8")

            # Mark it actively building, exactly as the builder would.
            pb = rt._project_builder
            done = asyncio.Event()

            async def _never():
                await done.wait()

            pb._active[slug] = asyncio.create_task(_never())
            try:
                check(slug in pb.active_projects(),
                      f"{spoken!r}: {slug} is registered as building")

                before = len(rt.permission_broker.audit_log(limit=50))
                res = await rt._router.execute(
                    ToolCall(name="project.delete", args={"name": spoken}),
                    retries=0)

                check((res.result or {}).get("error") == "build_in_progress",
                      f"{spoken!r}: refused with build_in_progress "
                      f"({(res.result or {}).get('error')!r})")
                check(rt.permission_broker.pending() == []
                      and len(rt.permission_broker.audit_log(limit=50)) == before,
                      f"{spoken!r}: NO permission was even requested")
                check((projects / slug / "main.py").exists(),
                      f"{spoken!r}: nothing was moved")
                trash = projects / ".trash"
                check(not trash.exists() or not any(trash.iterdir()),
                      f"{spoken!r}: no trash entry created")
                check(not pb._active[slug].done(),
                      f"{spoken!r}: the builder task is untouched")
            finally:
                done.set()
                pb._active.pop(slug, None)

    # An INACTIVE project still reaches the normal permission path.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        (projects / "other-project").mkdir(parents=True)
        (projects / "other-project" / "x.py").write_text("x=1\n", encoding="utf-8")
        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "Other Project"}),
            retries=0))
        check(await _approve_next(rt),
              "an inactive project still asks permission normally")
        res = await task
        check(res.ok and not (projects / "other-project").exists(),
              f"and deletes on approval ({res.error!r})")


async def test_legacy_delete_restore_keeps_the_exact_name():
    """BUG B: restore re-canonicalised, silently RENAMING a legacy project."""
    check.section("Round 3 B: legacy delete/restore preserves the exact identity")

    from core.tool_router import ToolCall

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        legacy = projects / "My_Old.Project"
        legacy.mkdir(parents=True)
        (legacy / "PROJECT.md").write_text("# legacy\n", encoding="utf-8")
        (legacy / "app.py").write_text("print('legacy')\n", encoding="utf-8")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "My_Old.Project"}),
            retries=0))
        await _approve_next(rt)
        res = await task
        entry = (res.result or {}).get("moved_to_trash", "")
        check(res.ok and entry.startswith("My_Old.Project--"),
              f"the trash entry keeps the legacy original ({entry!r})")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.restore", args={"entry": entry}), retries=0))
        await _approve_next(rt)
        res = await task

        check(res.ok, f"restore succeeded ({res.error!r})")
        check((projects / "My_Old.Project").is_dir(),
              "the EXACT legacy directory is back")
        check(not (projects / "my-old-project").exists(),
              "and no canonicalised sibling was created")
        check((res.result or {}).get("restored") == "My_Old.Project",
              f"the reported identity is the legacy one "
              f"({(res.result or {}).get('restored')!r})")
        check((legacy / "PROJECT.md").read_text(encoding="utf-8") == "# legacy\n"
              and (legacy / "app.py").read_text(encoding="utf-8") == "print('legacy')\n",
              "with contents intact")


async def test_legacy_last_active_and_sibling_collision():
    """BUG C: a STORED identity was re-canonicalised before the existence check."""
    check.section("Round 3 C: stored identities resolve to themselves")

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        legacy = projects / "My_Old.Project"
        legacy.mkdir(parents=True)
        (legacy / "PROJECT.md").write_text("# legacy one\n", encoding="utf-8")
        await m.add_fact(entity="projects", attribute="last_active",
                         value="My_Old.Project", confidence=0.95)

        pb = rt._project_builder
        check(await pb.last_active() == "My_Old.Project",
              f"a legacy pointer is honoured ({await pb.last_active()!r})")
        check(pb._project_path("My_Old.Project").name == "My_Old.Project",
              "and resolves to the legacy directory, not a canonical sibling")

        import shutil
        shutil.rmtree(legacy)
        check(await pb.last_active() is None,
              "once really gone, the pointer is stale and cleared")

    # Both siblings present: neither may resolve to the other.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        (projects / "My_Old.Project").mkdir(parents=True)
        (projects / "My_Old.Project" / "PROJECT.md").write_text(
            "# LEGACY\n", encoding="utf-8")
        (projects / "my-old-project").mkdir(parents=True)
        (projects / "my-old-project" / "PROJECT.md").write_text(
            "# CANONICAL\n", encoding="utf-8")

        pb = rt._project_builder
        check(pb._project_path("My_Old.Project").name == "My_Old.Project",
              "the legacy identity resolves to the legacy directory")
        check(pb._project_path("my-old-project").name == "my-old-project",
              "the canonical identity resolves to the canonical directory")

        for stored, marker in (("My_Old.Project", "# LEGACY\n"),
                               ("my-old-project", "# CANONICAL\n")):
            await m.add_fact(entity="projects", attribute="last_active",
                             value=stored, confidence=0.95)
            got = await pb.last_active()
            check(got == stored, f"a pointer to {stored!r} stays {got!r}")
            body = (pb._project_path(got) / "PROJECT.md").read_text(encoding="utf-8")
            check(body == marker,
                  f"and reads ITS OWN content ({body.strip()!r})")

        pm = ProjectManager(repo_root=Path(td), projects_dir=projects)
        check(pm.project_path("My_Old.Project").name == "My_Old.Project"
              and pm.project_path("my-old-project").name == "my-old-project",
              "the manager keeps them apart too")


async def test_degenerate_names_agree_across_surfaces():
    """BUG D: the manager RAISED where the builder returned a project."""
    check.section("Round 3 D: degenerate input, same answer everywhere")

    from core.tool_router import ToolCall

    degenerate = ["", "   ", "...", "\U0001f388", "\u65e5\u672c\u8a9e",
                  "___", "---"]
    labels = ["empty", "spaces", "dots", "emoji", "cjk", "underscores", "hyphens"]

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        projects.mkdir(parents=True)
        pm = ProjectManager(repo_root=root, projects_dir=projects)
        for raw, label in zip(degenerate, labels):
            builder = slugify(raw)
            try:
                manager = pm.project_path(raw).name
            except Exception as e:  # noqa: BLE001
                manager = f"RAISE {type(e).__name__}"
            check(builder == manager,
                  f"{label}: builder {builder!r} == manager {manager!r}")
            check(builder == "untitled",
                  f"{label}: the agreed answer is the canonical fallback")

    # And through the real scaffold tool.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        res = await rt._router.execute(
            ToolCall(name="project.scaffold", args={"name": "..."}), retries=0)
        check(res.ok, f"scaffold accepts a degenerate name ({res.error!r})")
        made = [p.name for p in projects.iterdir()
                if p.is_dir() and not p.name.startswith(".")]
        check(made == ["untitled"], f"creating the same fallback ({made})")


async def test_is_canonical_slug_matches_its_docstring():
    check.section("Round 3 E: is_canonical_slug is a true fixed point")

    from core.project_names import is_canonical_slug

    for v in ["balloon-tower-defense", "project-con", "untitled", "a"]:
        check(is_canonical_slug(v), f"{v!r} is canonical")
        check(canonical_project_slug(v) == v, f"{v!r} is genuinely a fixed point")

    for v in ["Balloon-Tower-Defense", "foo--bar", "-foo", "foo-", "con",
              "COM1", "foo_bar", "foo.bar", "x" * 60, "", "   "]:
        check(not is_canonical_slug(v), f"{v[:24]!r} is NOT canonical")


async def test_quoted_and_long_names_are_exact():
    """BUG F: quoted titles were silently truncated to a 41-character prefix."""
    check.section("Round 3 F: quoted names are exact, prefixes are refused")

    title47 = "Abcdefghij Klmnopqrst Uvwxyz Abcdefghijklmn"
    check(len(title47) > _MAX_RAW, f"the fixture exceeds the old cap ({len(title47)})")

    for text, want in [
        (f'create a project called "{title47}"', title47),
        ('create a project called "Rock & Roll Tracker"', "Rock & Roll Tracker"),
        ('create a project called "My.Project_Name"', "My.Project_Name"),
        ("create a project called 'Serpent and I Want Python'",
         "Serpent and I Want Python"),
    ]:
        got = _name(text)
        check(got == want, f"{text[:46]!r}… -> {got!r}")

    # A quoted title longer than the directory limit: the human title is whole,
    # the SLUG is bounded by the identity layer.
    long_title = "Z" * 60
    got = _name(f'create a project called "{long_title}"')
    check(got == long_title, f"a 60-character quoted title survives whole ({len(got or '')})")
    check(len(canonical_project_slug(got)) <= MAX_SLUG_LEN,
          f"and the slug is bounded ({len(canonical_project_slug(got))})")

    # An UNQUOTED capture that hits the regex limit is a prefix -> ask.
    got = _name("create a project called " + "Y" * 60)
    check(got == NEEDS_NAME,
          f"an unquoted over-long name asks rather than using a prefix ({got!r})")


async def test_requirement_clauses_with_pronouns():
    """BUG G: "and I want …" is the grammar of the ORIGINAL live failure."""
    check.section("Round 3 G: first-person requirement clauses are cut")

    cases = [
        "create a project called Serpent and I want Python",
        "create a project called Serpent and I need levels",
        "create a project called Serpent and it should run offline",
        "create a project called Serpent and we should add multiplayer",
        "create a project called Serpent but I want it simple",
        "create a project called Serpent so I can run it offline",
    ]
    for text in cases:
        for label, variant in _variants(text):
            got = _name(variant)
            check(got is not None and got != NEEDS_NAME
                  and len(got.split()) == 1,
                  f"{label}: {text[24:52]!r}… -> a one-word title ({got!r})")

    # Quoting still means exactly the quoted title.
    check(_name('create a project called "Serpent and I Want Python"')
          == "Serpent and I Want Python",
          "a quoted first-person title is kept whole")


async def test_every_existing_identity_operation_resolves_to_itself():
    """Self-review finding: three more sites canonicalised a STORED identity.

    `is_building`, `improve` and `status_text` all took an identity Nova already
    had and ran it back through the NEW-name canonicaliser, so a legacy project
    was invisible to the build guard, improved the wrong directory, and reported
    status for a project that may not exist. `start()` is the one that correctly
    takes a human name.
    """
    check.section("Round 3 self-review: existing identities never re-canonicalise")

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        pb = rt._project_builder
        legacy = projects / "My_Old.Project"
        legacy.mkdir(parents=True)
        (legacy / "PROJECT.md").write_text("# legacy status\n", encoding="utf-8")

        # status_text must read the LEGACY project, not a canonical sibling.
        text = pb.status_text("My_Old.Project")
        check("don't have a project" not in text,
              f"status_text finds the legacy project ({text[:60]!r})")

        # is_building must see a build registered under the legacy identity.
        done = asyncio.Event()

        async def _never():
            await done.wait()

        pb._active["My_Old.Project"] = asyncio.create_task(_never())
        try:
            check(pb.is_building("My_Old.Project"),
                  "is_building sees a legacy project's active build")
            check("My_Old.Project" in pb.active_projects(),
                  "and it is listed as active")
        finally:
            done.set()
            pb._active.pop("My_Old.Project", None)

        # improve must not report a legacy project as unknown.
        res = await pb.improve(slug="My_Old.Project", instructions="tidy it")
        check(res.get("reason") != "unknown project",
              f"improve resolves the legacy project ({res.get('reason')!r})")


# ── ROUND 4 ─────────────────────────────────────────────────────────────────
LONG_LEGACY = "Legacy_Project_" + ("X" * 95)          # 110 chars
PREFIX_96 = LONG_LEGACY[:96]                           # the truncated sibling


async def test_existing_identity_is_never_truncated():
    """BUG 1: `safe_live_component` shortened a stored identity at 96 chars.

    The helper exists to PRESERVE legacy identities, and the old ProjectManager
    had no length cap — so a directory longer than any bound invented here can
    genuinely exist. Truncating it made Nova check, read, delete and status a
    DIFFERENT path, and with a shorter sibling present, the wrong project.
    """
    check.section("Round 4.1: a stored identity is never silently shortened")

    from core.project_names import MAX_COMPONENT_LEN, safe_live_component
    from core.tool_router import ToolCall

    check(len(LONG_LEGACY) > MAX_SLUG_LEN * 2,
          f"the fixture exceeds the old 96-char cap ({len(LONG_LEGACY)})")
    check(safe_live_component(LONG_LEGACY) == LONG_LEGACY,
          f"the full identity survives ({len(safe_live_component(LONG_LEGACY))} chars)")

    # Beyond a REAL filesystem limit it fails explicitly rather than renaming.
    raised = ""
    try:
        safe_live_component("Y" * (MAX_COMPONENT_LEN + 5))
    except ValueError as e:
        raised = str(e)
    check(bool(raised), f"past {MAX_COMPONENT_LEN} chars it FAILS ({raised[:70]})")
    check("shortened" in raised or "cannot" in raised,
          "and says why rather than guessing")

    # Full lifecycle on a genuinely long legacy directory.
    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        legacy = projects / LONG_LEGACY
        legacy.mkdir(parents=True)
        (legacy / "PROJECT.md").write_text("# long legacy\n", encoding="utf-8")

        pb = rt._project_builder
        pm = ProjectManager(repo_root=Path(td), projects_dir=projects)

        check(LONG_LEGACY in pm.list_projects(),
              "list_projects returns the exact long name")
        check(pb._project_path(LONG_LEGACY).name == LONG_LEGACY,
              "_project_path resolves the exact directory")

        await m.add_fact(entity="projects", attribute="last_active",
                         value=LONG_LEGACY, confidence=0.95)
        check(await pb.last_active() == LONG_LEGACY,
              "last_active honours the exact long identity")
        check((pb._project_path(LONG_LEGACY) / "PROJECT.md").read_text(
              encoding="utf-8") == "# long legacy\n",
              "status/read reaches the exact contents")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": LONG_LEGACY}), retries=0))
        await _approve_next(rt)
        res = await task
        entry = (res.result or {}).get("moved_to_trash", "")
        check(res.ok and (res.result or {}).get("project") == LONG_LEGACY,
              f"delete moves the exact directory ({(res.result or {}).get('project')!r})")
        check(entry.startswith(LONG_LEGACY + "--"),
              "and the trash id keeps the full original")

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.restore", args={"entry": entry}), retries=0))
        await _approve_next(rt)
        res = await task
        check(res.ok and (projects / LONG_LEGACY).is_dir(),
              "restore brings the exact directory back")
        check((res.result or {}).get("restored") == LONG_LEGACY,
              "reporting the full identity")
        names = [x.name for x in projects.iterdir()
                 if x.is_dir() and not x.name.startswith(".")]
        check(names == [LONG_LEGACY],
              f"and NO truncated sibling was created ({[n[:20] for n in names]})")


async def test_long_and_truncated_siblings_stay_apart():
    check.section("Round 4.1: a 96-char sibling is a different project")

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        for name, body in ((LONG_LEGACY, "# LONG\n"), (PREFIX_96, "# PREFIX\n")):
            d = projects / name
            d.mkdir(parents=True)
            (d / "PROJECT.md").write_text(body, encoding="utf-8")

        pb = rt._project_builder
        for stored, marker in ((LONG_LEGACY, "# LONG\n"), (PREFIX_96, "# PREFIX\n")):
            resolved = pb._project_path(stored)
            check(resolved.name == stored,
                  f"{len(stored)}-char identity resolves to itself")
            check((resolved / "PROJECT.md").read_text(encoding="utf-8") == marker,
                  f"{len(stored)}-char identity reads ITS OWN content")


async def test_case_variant_resolves_to_the_actual_disk_identity():
    """BUG 2: a case-insensitive match returned the CALLER's spelling.

    On Windows `(projects/"my_old.project").is_dir()` is True when the real
    directory is `My_Old.Project`. Handing back the caller's spelling produces an
    identity that does not exist as written, and every exact comparison then fails
    — `active_projects()` membership among them, which is what stops a delete
    racing a build.

    `Path.resolve()` happens to repair this on Windows today; that is a platform
    side effect, not a contract, so the identity is now recovered explicitly.
    """
    check.section("Round 4.2: case variants resolve to the real disk identity")

    from core.project_names import resolve_existing_identity
    from core.tool_router import ToolCall

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        actual = "My_Old.Project"
        (projects / actual).mkdir(parents=True)
        (projects / actual / "PROJECT.md").write_text("# legacy\n", encoding="utf-8")
        pm = ProjectManager(repo_root=Path(td), projects_dir=projects)

        for req in [actual, "my_old.project", "MY_OLD.PROJECT"]:
            got = resolve_existing_identity(projects, req)
            check(got == actual,
                  f"resolver: {req!r} -> {got!r} (want {actual!r})")
            check(pm.project_path(req).name == actual,
                  f"manager:  {req!r} -> {pm.project_path(req).name!r}")
            # Assert the SANITIZER's own return, not just the resolved path.
            # `Path.resolve()` repairs casing on Windows, so going only through
            # `project_path()` hides whether the identity string itself is right —
            # and that string is what `active_projects()` membership compares.
            check(pm._sanitize(req) == actual,
                  f"_sanitize: {req!r} -> {pm._sanitize(req)!r} (want {actual!r})")

        # A name that does not exist stays unresolved.
        check(resolve_existing_identity(projects, "not-there") is None,
              "an unknown name resolves to None")

    # AMBIGUITY. Two entries whose names differ only under `.lower()` must never
    # resolve to a guess. On NTFS this is constructible with the Kelvin sign
    # (U+212A), which is a distinct character on disk but lowercases to plain
    # "k" — so the case-insensitive branch sees two candidates. A platform
    # `if` would have skipped this branch entirely on Windows and left the guard
    # unfalsifiable here.
    with _tmp() as td:
        projects = Path(td) / "projects"
        projects.mkdir(parents=True)
        kelvin = "Kelvin".replace("K", "K")
        (projects / kelvin).mkdir()
        (projects / "kelvin").mkdir()
        entries = sorted(x.name for x in projects.iterdir())
        check(len(entries) == 2,
              f"both case-folding siblings exist on this filesystem ({len(entries)})")
        check(kelvin.lower() == "kelvin",
              "and they collide under .lower()")
        check(resolve_existing_identity(projects, "KELVIN") is None,
              f"an ambiguous match returns None, never a guess "
              f"({resolve_existing_identity(projects, 'KELVIN')!r})")
        # An EXACT match still wins over the ambiguity.
        check(resolve_existing_identity(projects, "kelvin") == "kelvin",
              "while an exact match resolves unambiguously")
        check(resolve_existing_identity(projects, kelvin) == kelvin,
              "for either sibling")


async def test_case_variant_delete_respects_the_active_build():
    check.section("Round 4.2: a case-variant delete cannot race an active build")

    from core.tool_router import ToolCall

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        actual = "My_Old.Project"
        (projects / actual).mkdir(parents=True)
        (projects / actual / "main.py").write_text("print(1)\n", encoding="utf-8")
        await m.add_fact(entity="projects", attribute="last_active",
                         value=actual, confidence=0.95)

        pb = rt._project_builder
        done = asyncio.Event()

        async def _never():
            await done.wait()

        pb._active[actual] = asyncio.create_task(_never())
        try:
            before = len(rt.permission_broker.audit_log(limit=50))
            res = await rt._router.execute(
                ToolCall(name="project.delete", args={"name": "my_old.project"}),
                retries=0)
            check((res.result or {}).get("error") == "build_in_progress",
                  f"a lowercase request hits the guard "
                  f"({(res.result or {}).get('error')!r})")
            check(rt.permission_broker.pending() == []
                  and len(rt.permission_broker.audit_log(limit=50)) == before,
                  "no permission was requested")
            trash = projects / ".trash"
            check(not trash.exists() or not any(trash.iterdir()),
                  "and nothing was moved to trash")
        finally:
            done.set()
            pb._active.pop(actual, None)

        # Build finished: a case-variant delete now works and clears the pointer
        # that memory stored under the ACTUAL identity.
        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "MY_OLD.PROJECT"}),
            retries=0))
        await _approve_next(rt)
        res = await task
        check(res.ok and (res.result or {}).get("project") == actual,
              f"the result reports the actual identity "
              f"({(res.result or {}).get('project')!r})")
        ptr = await m.get_latest_fact(entity="projects", attribute="last_active")
        check((ptr.value if ptr else "") == "",
              f"and last_active cleared despite the casing difference "
              f"({ptr.value if ptr else None!r})")

        entry = (res.result or {}).get("moved_to_trash", "")
        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.restore", args={"entry": entry}), retries=0))
        await _approve_next(rt)
        res = await task
        check(res.ok and (res.result or {}).get("restored") == actual,
              f"restore returns the exact identity "
              f"({(res.result or {}).get('restored')!r})")


async def test_quoted_names_handle_both_delimiters():
    """BUG 3: the body class excluded BOTH quote characters."""
    check.section("Round 4.3: only the matching quote closes a name")

    cases = [
        ('create a project called "Marcus\'s Game"', "Marcus's Game"),
        ('create a project called "John\'s Rock & Roll Tracker"',
         "John's Rock & Roll Tracker"),
        ("""create a project called 'The "Best" Game'""", 'The "Best" Game'),
        ("""create a project named 'Nova "Mini" Assistant'""",
         'Nova "Mini" Assistant'),
        ('create a project called "Marcus\'s Game" and use Python', "Marcus's Game"),
        ('create a project called "Serpent and I Want Python"',
         "Serpent and I Want Python"),
    ]
    for text, want in cases:
        got = _name(text)
        check(got == want, f"{text[24:60]!r} -> {got!r} (want {want!r})")

    # The slug is still sanitised afterwards — a separate step.
    marcus_slug = canonical_project_slug("Marcus's Game")
    check(marcus_slug == "marcus-s-game",
          f"the slug sanitises punctuation later ({marcus_slug!r})")

    # An unmatched quote must fail closed, never accept a prefix.
    for text in ['create a project called "Unclosed Name',
                 "create a project called 'Also unclosed",
                 'create a project named "Half quoted and use Python']:
        got = _name(text)
        check(got == NEEDS_NAME,
              f"unmatched quote asks rather than taking a prefix ({got!r})")


async def main():
    await test_case_invariance_of_the_name_boundary()
    await test_case_invariance_end_to_end()
    await test_ambiguous_names_ask_rather_than_guess()
    await test_one_canonical_identity_owner()
    await test_legacy_directories_are_preserved()
    await test_scaffold_through_the_real_router()
    await test_delete_by_natural_name_clears_the_pointer()
    await test_stale_pointer_is_never_returned()
    await test_identity_lifecycle()
    await test_active_build_guard_uses_resolved_identity()
    await test_legacy_delete_restore_keeps_the_exact_name()
    await test_legacy_last_active_and_sibling_collision()
    await test_degenerate_names_agree_across_surfaces()
    await test_is_canonical_slug_matches_its_docstring()
    await test_quoted_and_long_names_are_exact()
    await test_requirement_clauses_with_pronouns()
    await test_every_existing_identity_operation_resolves_to_itself()
    await test_existing_identity_is_never_truncated()
    await test_long_and_truncated_siblings_stay_apart()
    await test_case_variant_resolves_to_the_actual_disk_identity()
    await test_case_variant_delete_respects_the_active_build()
    await test_quoted_names_handle_both_delimiters()
    check.finish()


if __name__ == "__main__":
    run(main)
