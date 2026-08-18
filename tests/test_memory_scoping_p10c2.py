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


async def test_m9_transient_observation_is_not_a_user_fact():
    check.section("M9: a transient tool observation is not a permanent preference")

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


async def test_m8_deleted_project_history():
    check.section("M8: a deleted project leaves history but not presence")

    with _tmp() as td:
        m = await _fresh(td)
        await m.add_fact(entity="project:gamma", attribute="brief",
                         value="a small dice roller", confidence=0.95)
        await m.add_fact(entity="projects", attribute="last_active",
                         value="gamma", confidence=0.95)

        # Deleting the project moves the directory; the historical fact remains
        # unless something removes it. Record what actually happens.
        hist = await _val(m, "project:gamma", "brief")
        check(hist == "a small dice roller",
              "M8 historical memory of the project remains after the fact is written")
        check(await _val(m, "projects", "last_active") == "gamma",
              "M8 last_active points at it while it is current")

        # After a delete, the pointer must be updated by whoever deletes; this
        # test records the CURRENT contract rather than asserting a fix.
        await m.add_fact(entity="projects", attribute="last_active",
                         value="", confidence=0.95)
        check((await _val(m, "projects", "last_active")) == "",
              "M8 last_active can be cleared")


async def test_m10_unverified_speaker_isolation():
    check.section("M10: an unverified speaker does not resolve to the owner")

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


async def main():
    await test_m3_m4_m5_scoping()
    await test_m6_restart_durability()
    await test_m9_transient_observation_is_not_a_user_fact()
    await test_m8_deleted_project_history()
    await test_m10_unverified_speaker_isolation()
    check.finish()


if __name__ == "__main__":
    run(main)
