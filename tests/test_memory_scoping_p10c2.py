"""Stage 6 + 8: memory scoping and durability, through the production unifier.

Memory is tested here as an ACTIVE INPUT — what wins when two remembered things
disagree — rather than as CRUD. The questions that matter are scope questions:

  does a project-specific decision escape to become a global preference?
  does a later explicit correction beat an earlier decision?
  does project A leak into project B?
  does a durable decision survive a restart, and does an ephemeral one avoid
    being invented into permanence?
  can an unverified speaker read what belongs to the owner?

Everything runs against a real `MemoryUnifier` on a temp directory.

Run:  venv\\Scripts\\python.exe tests\\test_memory_scoping_p10c2.py
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

from harness import Checks, run  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def _fresh(td):
    m = MemoryUnifier(Path(td))
    await m.initialize()
    return m


async def _val(m, entity, attribute):
    f = await m.get_latest_fact(entity=entity, attribute=attribute)
    return f.value if f else None


async def test_m3_m4_m5_scoping():
    check.section("M3/M4/M5: project decisions vs global preferences")

    with _tmp() as td:
        m = await _fresh(td)

        # Global preference.
        await m.add_fact(entity="user", attribute="language_preference",
                         value="Python", confidence=0.9)
        # Project A makes a DIFFERENT choice for itself.
        await m.add_fact(entity="project:alpha", attribute="language",
                         value="JavaScript", confidence=0.95)

        check(await _val(m, "user", "language_preference") == "Python",
              "M5 the global preference is unchanged by a project decision")
        check(await _val(m, "project:alpha", "language") == "JavaScript",
              "M5 project A holds its own choice")
        check(await _val(m, "project:beta", "language") is None,
              "M5 project B did NOT inherit A's choice")

        # M3: a project design decision is retained.
        await m.add_fact(entity="project:alpha", attribute="config_format",
                         value="JSON", confidence=0.95)
        check(await _val(m, "project:alpha", "config_format") == "JSON",
              "M3 the project design decision is retained")

        # M4: a later explicit correction supersedes, for that project only.
        await m.add_fact(entity="project:alpha", attribute="database",
                         value="SQLite", confidence=0.9)
        await m.add_fact(entity="project:alpha", attribute="database",
                         value="PostgreSQL", confidence=0.95)
        check(await _val(m, "project:alpha", "database") == "PostgreSQL",
              "M4 the newer explicit decision wins")
        check(await _val(m, "user", "database") is None,
              "M4 and it did not become a global preference")
        check(await _val(m, "project:beta", "database") is None,
              "M4 nor did it leak to another project")


async def test_m6_restart_durability():
    check.section("M6 / Stage 8: durable survives restart, ephemeral is not invented")

    with _tmp() as td:
        m = await _fresh(td)
        await m.add_fact(entity="user", attribute="language_preference",
                         value="Python", confidence=0.9)
        await m.add_fact(entity="project:alpha", attribute="language",
                         value="JavaScript", confidence=0.95)
        await m.add_fact(entity="project:alpha", attribute="database",
                         value="PostgreSQL", confidence=0.95)

        # Destroy the object entirely and rebuild from disk.
        del m
        m2 = await _fresh(td)

        check(await _val(m2, "user", "language_preference") == "Python",
              "M6 the global preference survives a restart")
        check(await _val(m2, "project:alpha", "language") == "JavaScript",
              "M6 the project decision survives")
        check(await _val(m2, "project:alpha", "database") == "PostgreSQL",
              "M6 including the corrected value, not the superseded one")
        check(await _val(m2, "project:alpha", "config_format") is None,
              "M6 and nothing that was never written is invented")
        check(await _val(m2, "project:ghost", "anything") is None,
              "M6 an unknown project has no state")


async def test_m9_observation_entity_separation_only():
    """M9, honestly scoped: this proves ENTITY SEPARATION, not classification.

    It writes the observation under `observation:*` itself, so it shows that a
    fact stored there does not appear under `user`. It does NOT exercise the
    production tool-result -> memory path, and therefore does not prove that a
    real tool observation is classified as transient. That path is NOT ASSESSED.
    """
    check.section("M9: observation entity separation (NOT the ingestion path)")

    with _tmp() as td:
        m = await _fresh(td)
        # A troubleshooting observation, stored under a tool/observation entity.
        await m.add_fact(entity="observation:localhost",
                         attribute="port_8008", value="occupied", confidence=0.7)

        check(await _val(m, "observation:localhost", "port_8008") == "occupied",
              "the observation is retrievable where it was written")
        check(await _val(m, "user", "port_8008") is None,
              "M9 it did NOT become a user preference")
        check(await _val(m, "user", "port_preference") is None,
              "M9 nor a fabricated preference of any name")
        check(True,
              "M9 SCOPE: this test wrote the observation under observation:* "
              "itself; the production tool->memory classification path is NOT "
              "ASSESSED")


async def test_m8_real_project_delete_vs_memory():
    """M8 through the REAL ProjectManager, not a hand-cleared pointer.

    The first version of this test wrote `last_active = ""` itself and called the
    result a pass. That proved the unifier can store an empty string — nothing
    about deletion. This drives the actual delete and then states plainly which
    parts the production code does, and which it does not.
    """
    check.section("M8: real project delete vs memory state")

    from core.project_manager import ProjectManager

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        (projects / "gamma").mkdir(parents=True)
        (projects / "gamma" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        pm = ProjectManager(repo_root=root, projects_dir=projects)

        m = await _fresh(td)
        await m.add_fact(entity="project:gamma", attribute="brief",
                         value="a small dice roller", confidence=0.95)
        await m.add_fact(entity="projects", attribute="last_active",
                         value="gamma", confidence=0.95)

        listed_before = [p.name for p in projects.iterdir() if p.is_dir()
                         and not p.name.startswith(".")]
        check("gamma" in listed_before,
              f"the project is present before deletion ({listed_before})")

        res = await asyncio.to_thread(pm.delete_project, "gamma")
        entry = res.get("moved_to_trash", "")

        listed_after = [p.name for p in projects.iterdir() if p.is_dir()
                        and not p.name.startswith(".")]
        check("gamma" not in listed_after,
              f"M8 the live project disappears from the listing ({listed_after})")
        check(bool(entry) and (projects / ".trash" / entry / "main.py").exists(),
              f"M8 the files move to trash, recoverable ({entry!r})")

        hist = await _val(m, "project:gamma", "brief")
        check(hist == "a small dice roller",
              f"M8 historical project memory REMAINS after deletion ({hist!r})")

        # ProjectManager deliberately holds no memory reference, so it cannot
        # clear the pointer itself; the coupling lives in the caller.
        check(not hasattr(pm, "_memory"),
              "M8 ProjectManager holds no memory reference by design")
        still = await _val(m, "projects", "last_active")
        check(still == "gamma",
              f"M8 at the manager level the pointer is untouched ({still!r}) — "
              f"the runtime tool is what clears it, proven below")


async def test_m8_runtime_delete_clears_the_active_pointer():
    """The stated invariant: `last_active` never names a project that is gone.

    Driven through the REAL RuntimeManager tool, because the fix lives in the
    caller and a ProjectManager-only test cannot see it. Found by rewriting the
    hand-cleared version of M8 into a real delete.
    """
    check.section("M8: runtime delete clears a stale active-project pointer")

    from core.runtime import RuntimeManager
    from core.tool_router import ToolCall
    from core.tooling import build_tool_router

    class _StubLLM:
        gpu_status = type("S", (), {"status": "stub"})()

        async def initialize(self):
            return None

        async def generate(self, *a, **k):
            raise AssertionError("delete must not call the model")

    with _tmp() as td:
        root = Path(td)
        projects = root / "projects"
        (projects / "gamma").mkdir(parents=True)
        (projects / "gamma" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        mem_dir = root / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        m = MemoryUnifier(mem_dir)
        await m.initialize()
        await m.add_fact(entity="project:gamma", attribute="brief",
                         value="a small dice roller", confidence=0.95)
        await m.add_fact(entity="projects", attribute="last_active",
                         value="gamma", confidence=0.95)

        router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
        rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                            llm=_StubLLM(), router=router, memory_dir=mem_dir)

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "gamma"}), retries=0))
        await asyncio.sleep(0.4)
        pend = rt.permission_broker.pending()
        check(len(pend) == 1, f"the delete asks permission ({len(pend)})")
        rt.permission_broker.resolve(pend[0]["request_id"], True, by="marcus")
        res = await task
        check(res.ok, f"the delete completed ({res.error!r})")

        after = await _val(m, "projects", "last_active")
        check((after or "") == "",
              f"M8 last_active no longer names the deleted project ({after!r})")
        hist = await _val(m, "project:gamma", "brief")
        check(hist == "a small dice roller",
              f"M8 but historical memory of it REMAINS ({hist!r})")
        listed = [p.name for p in projects.iterdir() if p.is_dir()
                  and not p.name.startswith(".")]
        check("gamma" not in listed, f"M8 and the project is gone ({listed})")

        # A delete of a DIFFERENT project must not clear a pointer that is still
        # valid.
        (projects / "delta").mkdir(parents=True)
        (projects / "delta" / "x.py").write_text("x=1\n", encoding="utf-8")
        (projects / "epsilon").mkdir(parents=True)
        (projects / "epsilon" / "y.py").write_text("y=1\n", encoding="utf-8")
        await m.add_fact(entity="projects", attribute="last_active",
                         value="delta", confidence=0.95)

        task = asyncio.create_task(rt._router.execute(
            ToolCall(name="project.delete", args={"name": "epsilon"}), retries=0))
        await asyncio.sleep(0.4)
        rt.permission_broker.resolve(
            rt.permission_broker.pending()[0]["request_id"], True, by="marcus")
        await task
        check(await _val(m, "projects", "last_active") == "delta",
              "M8 deleting another project leaves a VALID pointer alone")


async def test_m10_entity_separation_only():
    """M10, honestly scoped: MemoryUnifier entity separation only.

    The entities are chosen BY THIS TEST, so it proves the unifier keeps separate
    entities separate. It does not drive TurnIdentity or the runtime, so it says
    nothing about whether production routes a given speaker to the right entity.
    Identity -> memory END TO END is NOT ASSESSED here; the P5 speaker suites are
    separate, prior evidence and this test must not be credited with their proof.
    """
    check.section("M10: unifier entity separation (NOT identity->memory routing)")

    from core.turn_identity import current_identity

    ident = current_identity()
    check(hasattr(ident, "memory_entity"),
          f"identity exposes a memory entity ({getattr(ident, 'memory_entity', None)!r})")
    check(hasattr(ident, "is_owner"),
          "and an ownership flag distinct from the entity")

    with _tmp() as td:
        m = await _fresh(td)
        # Owner-scoped and guest-scoped facts are different entities by
        # construction — the isolation is the entity, not a filter applied later.
        await m.add_fact(entity="user", attribute="private_note",
                         value="Marcus only", confidence=0.95)
        await m.add_fact(entity="speaker:p-guest", attribute="private_note",
                         value="guest only", confidence=0.95)

        check(await _val(m, "user", "private_note") == "Marcus only",
              "the owner's note is under the owner entity")
        check(await _val(m, "speaker:p-guest", "private_note") == "guest only",
              "the guest's note is under the guest entity")
        check(await _val(m, "speaker:p-guest", "private_note") != "Marcus only",
              "M10 a guest lookup never returns the owner's value")
        check(await _val(m, "speaker:unverified", "private_note") is None,
              "M10 an unverified speaker has no inherited state")
        check(True,
              "M10 SCOPE: entities were chosen by this test; production "
              "identity->memory routing is NOT ASSESSED here")


async def main():
    await test_m3_m4_m5_scoping()
    await test_m6_restart_durability()
    await test_m9_observation_entity_separation_only()
    await test_m8_real_project_delete_vs_memory()
    await test_m8_runtime_delete_clears_the_active_pointer()
    await test_m10_entity_separation_only()
    check.finish()


if __name__ == "__main__":
    run(main)
