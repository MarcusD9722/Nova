"""A durable write that was abandoned must leave a trace.

Found by a repo-wide scan: 132 `except ...: pass` handlers in production, most
of them legitimate graceful degradation — and a subset that abandons a WRITE or
a DELETE with no log line anywhere. Those are invisible by construction.
Nothing fails, nothing is logged, and the data is quietly wrong or quietly
still there.

THE RULE THIS SUITE PINS, which is also the rule the fix follows:

  * a READ that falls back to a default may stay silent — the fallback IS the
    designed behaviour and logging it would be noise;
  * a handler that abandons a durable WRITE or DELETE must say so, naming the
    operation, and must still degrade exactly as before.

`warning` where user content is lost or left exposed (a fact, a lesson, a
story, a purge that leaves vectors searchable). `debug` for ambient signals and
self-healing cleanups (mood, wellbeing, a reinforcement nudge).

WHAT THIS SUITE IS NOT. It covers four representative sites across three
modules, not all nineteen that were fixed: the unifier's reinforcement and its
semantic purge, the project builder's fact writer, and the runtime's lesson
capture. The remaining sites are the same shape, and each assertion here checks
BOTH halves — that the trace appears AND that the swallow still swallows —
because a "fix" that let the exception escape would be a worse bug than the
silence it replaced.

Run:  venv\\Scripts\\python.exe tests\\test_dropped_writes.py
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

from harness import Checks, boot, run  # noqa: E402

from core.project_builder import ProjectBuilder  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


class LogRecorder:
    """Stands in for a module's structlog logger and remembers every call."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        # debug / info / warning / error — whatever the module reaches for.
        def emit(event: str = "", **fields) -> None:
            self.records.append((level, str(event), dict(fields)))
        return emit

    def saw(self, event: str, *, level: str | None = None) -> bool:
        return any(name == event and (level is None or lv == level)
                   for lv, name, _ in self.records)

    def fields_of(self, event: str) -> dict:
        for _lv, name, fields in self.records:
            if name == event:
                return fields
        return {}

    def names(self) -> list[str]:
        return [f"{lv}:{name}" for lv, name, _ in self.records]


class patched_logger:
    """Swap one module's `logger` for a recorder, and always put it back."""

    def __init__(self, module) -> None:
        self._module = module
        self._original = None

    def __enter__(self) -> LogRecorder:
        self._original = self._module.logger
        self.recorder = LogRecorder()
        self._module.logger = self.recorder
        return self.recorder

    def __exit__(self, *_exc) -> bool:
        self._module.logger = self._original
        return False


def _tmp():
    # Windows keeps the diskcache/sqlite handles briefly after close, and a
    # temp dir that cannot be unlinked is not a test failure. Same helper
    # shape the other memory suites use.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def _fresh_memory(td: str) -> MemoryUnifier:
    mem = MemoryUnifier(Path(td) / "memory_data", enable_chroma=False)
    await mem.initialize()
    return mem


# ── D1: a re-mention whose reinforcement fails ─────────────────────────────

async def test_a_dropped_reinforcement_is_recorded() -> None:
    check.section("D1 a re-mention whose reinforcement fails is not silent")
    with _tmp() as td:
        import memory.unifier as U

        mem = await _fresh_memory(td)
        entity, attribute = "user", "note"
        # PRECONDITION, asserted rather than assumed: the reinforce path lives
        # in the LIST-VALUED branch. A singleton attribute supersedes instead
        # and would never reach the handler under test.
        check(mem._is_singleton_fact(entity, attribute) is False,
              f"{attribute!r} is list-valued, so a duplicate reinforces "
              f"({mem._is_singleton_fact(entity, attribute)})")

        first = await mem.add_fact(entity=entity, attribute=attribute,
                                   value="prefers dark mode", confidence=0.9)

        async def boom(*_a, **_kw):
            raise RuntimeError("sqlite is unavailable")

        mem._sqlite.reinforce_fact = boom  # type: ignore[assignment]

        with patched_logger(U) as rec:
            again = await mem.add_fact(entity=entity, attribute=attribute,
                                       value="prefers dark mode", confidence=0.9)

        check(str(again) == str(first),
              f"the duplicate still resolves to the stored fact "
              f"({str(again)[:8]} vs {str(first)[:8]})")
        check(rec.saw("fact_reinforce_failed", level="debug"),
              f"and the dropped reinforcement is recorded ({rec.names()})")


# ── D2: a purge that leaves vectors behind ─────────────────────────────────

async def test_a_failed_semantic_purge_is_recorded() -> None:
    check.section("D2 a purge that leaves vectors behind says so")
    with _tmp() as td:
        import memory.unifier as U

        mem = await _fresh_memory(td)
        fid = await mem.add_fact(entity="user", attribute="note",
                                 value="an old value", confidence=0.9)

        class StubChroma:
            """Only what `supersede_facts` reaches for."""

            def __init__(self) -> None:
                self.calls = 0

            async def delete_ids(self, ids) -> None:
                self.calls += 1
                raise RuntimeError("chroma refused the delete")

        stub = StubChroma()
        mem._chroma = stub  # type: ignore[assignment]

        with patched_logger(U) as rec:
            removed = await mem.supersede_facts(old_ids=[str(fid)])

        check(removed == 1,
              f"the durable row is still deleted ({removed})")
        check(stub.calls == 1,
              f"and the semantic delete really was attempted ({stub.calls})")
        rows = await mem.get_facts(entity="user", attribute="note", limit=10)
        check(not rows,
              f"the fact is gone from sqlite ({len(rows)} row(s) left)")
        check(rec.saw("semantic_delete_failed", level="warning"),
              f"and the vectors left behind are reported ({rec.names()})")


# ── D3: project metadata that never reached memory ─────────────────────────

async def test_a_dropped_project_fact_is_recorded() -> None:
    check.section("D3 project metadata that never reached memory says so")
    with _tmp() as td:
        import core.project_builder as PB

        class BoomMemory:
            """Every write fails; nothing else is reached by this path."""

            async def add_fact(self, **_kw):
                raise RuntimeError("memory is unavailable")

        pb = ProjectBuilder(projects_dir=Path(td), llm=None,
                            llm_semaphore=asyncio.Semaphore(1),
                            memory=BoomMemory())

        with patched_logger(PB) as rec:
            # Must not raise: every caller of `_save_fact` treats it as
            # best-effort bookkeeping beside the real work.
            await pb._save_fact("alpha", "status", "complete")

        check(rec.saw("project_fact_write_failed", level="warning"),
              f"the lost project fact is reported ({rec.names()})")
        fields = rec.fields_of("project_fact_write_failed")
        check(fields.get("attribute") == "status",
              f"naming WHICH write was lost ({fields})")
        check(str(fields.get("project")) == "alpha",
              f"and which project it belonged to ({fields})")


# ── D4: a stated preference that was never stored ──────────────────────────

async def test_a_dropped_lesson_is_recorded() -> None:
    check.section("D4 a stated preference that was not stored says so")
    async with boot(default_reply="Sure.") as nova:
        import core.runtime as R

        async def boom(*_a, **_kw):
            raise RuntimeError("memory is unavailable")

        nova.memory.add_lesson = boom  # type: ignore[assignment]

        with patched_logger(R) as rec:
            # A real lesson phrasing: matches _LESSON_PATTERNS, clears
            # _LESSON_SKIP, inside the length bounds.
            await nova.runtime._capture_lessons(
                "from now on, keep your replies short")

        check(rec.saw("lesson_write_failed", level="warning"),
              f"the lesson Nova failed to store is reported ({rec.names()})")


async def main() -> None:
    await test_a_dropped_reinforcement_is_recorded()
    await test_a_failed_semantic_purge_is_recorded()
    await test_a_dropped_project_fact_is_recorded()
    await test_a_dropped_lesson_is_recorded()
    check.finish()


if __name__ == "__main__":
    run(main)
