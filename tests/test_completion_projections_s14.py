"""Every projection, checked against the evaluator (Stage 14 §9).

`CompletionService.evaluate()` is the authority. Everything else — PROJECT.md,
the durable fact, the SQLite rows, the status the chat tool returns, the event
payload — is a picture of it, and a picture can be wrong.

THE RULE THIS SUITE FOLLOWS: no projection is ever used as the oracle for
another. Each is fetched independently and compared with the evaluator. Five
projections agreeing with each other and not with the evidence is not
consistency, it is a consensus about something false — and that is exactly
what was found here: `status_text` read `## Status` out of PROJECT.md, so with
the evaluator saying FAILING the chat tool said "Project calc: complete."

Run:  venv\\Scripts\\python.exe tests\\test_completion_projections_s14.py
"""

from __future__ import annotations

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

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, IDEA, PARTIALLY_IMPLEMENTED, PASSED, PASSING,
    PLANNED, SCAFFOLDED,
)
from core.event_bus import BUS  # noqa: E402
from core.project_builder import ProjectBuilder  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

REQUEST = "a calculator that adds and subtracts"


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class World:
    """One project, with every projection reachable independently."""

    def __init__(self, td: str, slug: str):
        self.slug = slug
        self.root = Path(td)
        self.projects = self.root / "projects"
        self.path = self.projects / slug
        self.path.mkdir(parents=True)
        self.mem = MemoryUnifier(self.root / "memory_data", enable_chroma=False)

    async def start(self):
        await self.mem.initialize()
        import asyncio
        self.pb = ProjectBuilder(projects_dir=self.projects, llm=None,
                                 llm_semaphore=asyncio.Semaphore(1),
                                 memory=self.mem)
        self.svc = self.pb.completion
        return self

    # ── the authority ───────────────────────────────────────────────────────
    async def evaluator(self):
        return await self.svc.evaluate(slug=self.slug)

    # ── the projections, each fetched on its own ────────────────────────────
    def project_md_status(self) -> str:
        md = (self.path / "PROJECT.md")
        if not md.exists():
            return "(no file)"
        m = re.search(r"## Status\n(.*?)(?:\n## |\Z)",
                      md.read_text(encoding="utf-8"), re.DOTALL)
        return m.group(1).strip() if m else "(no section)"

    def project_md(self) -> str:
        md = self.path / "PROJECT.md"
        return md.read_text(encoding="utf-8") if md.exists() else ""

    async def memory_fact(self) -> str:
        rec = await self.mem.get_latest_fact(f"project:{self.slug}", "status")
        return str(getattr(rec, "value", "") or "(no fact)")

    async def chat_tool_status(self) -> str:
        return await self.pb.status_text(self.slug)

    async def sqlite_rows(self):
        req = await self.mem.current_requirement(project_name=self.slug)
        crit = await self.mem.list_acceptance_criteria(project_name=self.slug)
        ev = await self.mem.list_acceptance_evidence(project_name=self.slug)
        return req, crit, ev

    def last_event(self, kind="project.state_changed"):
        rows = [e for e in BUS.recent(limit=800)
                if e.type == kind and (e.data or {}).get("project") == self.slug]
        return rows[-1].data if rows else None

    # ── driving state ───────────────────────────────────────────────────────
    async def request(self, text=REQUEST):
        return await self.svc.record_request(slug=self.slug, request_text=text)

    async def criteria(self, rev, specs=None, seal=True):
        ids = await self.svc.set_criteria(slug=self.slug, revision=rev, criteria=(
            specs if specs is not None else [
                {"text": "adds two numbers", "origin_quote": "adds"},
                {"text": "subtracts two numbers", "origin_quote": "subtracts"}]))
        if seal:
            await self.svc.seal_contract(slug=self.slug, revision=rev)
        return ids

    def code(self, body="def add(a, b):\n    return a + b\n"):
        (self.path / "main.py").write_text(body, encoding="utf-8")

    async def verdict_for(self, cid, verdict=PASSED, error=""):
        ctx = await self.svc.begin_check(slug=self.slug, criterion_id=cid)
        await self.svc.record_verdict(context=ctx, verdict=verdict, error=error)

    async def publish(self):
        """Write every projection from the current derived verdict."""
        v = await self.evaluator()
        self.pb._write_project_md(self.slug, brief=REQUEST, status=v.state,
                                  verdict=v, summary="a calculator")
        await self.pb._save_fact(self.slug, "status", v.state)
        await self.pb._announcer.announce(slug=self.slug, verdict=v,
                                          reason="projected")
        return v


async def agree(w: World, label: str):
    """Every projection, compared with the evaluator. Independently."""
    v = await w.evaluator()
    md = w.project_md_status()
    fact = await w.memory_fact()
    chat = await w.chat_tool_status()
    ev = w.last_event()

    check(md == v.state,
          f"{label}: PROJECT.md says what the evaluator says "
          f"(md={md!r} eval={v.state!r})")
    check(fact == v.state,
          f"{label}: the durable fact agrees (fact={fact!r})")
    check(v.state in chat,
          f"{label}: the chat status names the state ({chat[:70]!r})")
    check(ev and ev.get("current") == v.state,
          f"{label}: the last event carries it "
          f"({(ev or {}).get('current')!r})")
    if v.state != COMPLETE:
        check("complete" not in md.lower(),
              f"{label}: and nothing says complete ({md!r})")
        check(not chat.lower().startswith(f"project {w.slug}: complete"),
              f"{label}: chat does not claim complete ({chat[:60]!r})")
    return v


async def test_a_every_state_agrees_everywhere():
    check.section("§9 all seven states, every projection")
    with _tmp() as td:
        # IDEA — a request, no criteria.
        w = await World(td, "p-idea").start()
        await w.request()
        await w.publish()
        v = await agree(w, "IDEA")
        check(v.state == IDEA, f"state is idea ({v.state})")
        check("no acceptance criteria" in " ".join(v.reasons),
              f"and the absence of criteria is said out loud ({v.reasons})")

    with _tmp() as td:
        # PLANNED — criteria, no code.
        w = await World(td, "p-planned").start()
        rev = await w.request()
        await w.criteria(rev)
        await w.publish()
        v = await agree(w, "PLANNED")
        check(v.state == PLANNED, f"state is planned ({v.state})")

    with _tmp() as td:
        # SCAFFOLDED — code, nothing demonstrated.
        w = await World(td, "p-scaffolded").start()
        rev = await w.request()
        await w.criteria(rev)
        w.code()
        await w.publish()
        v = await agree(w, "SCAFFOLDED")
        check(v.state == SCAFFOLDED, f"state is scaffolded ({v.state})")
        md = w.project_md()
        check(all(f"[ ] {s.criterion.text}" in md for s in v.outstanding),
              "every unproven criterion is visible in PROJECT.md")

    with _tmp() as td:
        # PARTIALLY_IMPLEMENTED — one of two proven.
        w = await World(td, "p-partial").start()
        rev = await w.request()
        ids = await w.criteria(rev)
        w.code()
        await w.verdict_for(ids[0])
        await w.publish()
        v = await agree(w, "PARTIAL")
        check(v.state == PARTIALLY_IMPLEMENTED, f"state is partial ({v.state})")
        md = w.project_md()
        check("[x] adds two numbers" in md and "[ ] subtracts two numbers" in md,
              "PROJECT.md shows which is proven and which is not")
        chat = await w.chat_tool_status()
        check("subtracts" in chat,
              f"and chat names what remains ({chat[:80]!r})")

    with _tmp() as td:
        # FAILING — a required criterion refuted.
        w = await World(td, "p-failing").start()
        rev = await w.request()
        ids = await w.criteria(rev)
        w.code()
        await w.verdict_for(ids[0])
        await w.verdict_for(ids[1], verdict=FAILED, error="there is no subtract")
        await w.publish()
        v = await agree(w, "FAILING")
        check(v.state == FAILING, f"state is failing ({v.state})")
        check("there is no subtract" in w.project_md(),
              "PROJECT.md carries the failure's own words")
        chat = await w.chat_tool_status()
        check("Failing:" in chat and "subtracts" in chat,
              f"and chat names the failing requirement ({chat[:80]!r})")

    with _tmp() as td:
        # PASSING — machine work done, a person still owes an answer.
        w = await World(td, "p-passing").start()
        rev = await w.request()
        ids = await w.criteria(rev, specs=[
            {"text": "adds two numbers", "origin_quote": "adds"},
            {"text": "subtracts two numbers", "origin_quote": "subtracts",
             "verify_kind": "human"}])
        w.code()
        await w.verdict_for(ids[0])
        await w.publish()
        v = await agree(w, "PASSING")
        check(v.state == PASSING, f"state is passing ({v.state})")
        chat = await w.chat_tool_status()
        check("final acceptance is still outstanding" in chat,
              f"chat distinguishes passing from complete ({chat[:100]!r})")

    with _tmp() as td:
        # COMPLETE — everything demonstrated.
        w = await World(td, "p-complete").start()
        rev = await w.request()
        ids = await w.criteria(rev)
        w.code("def add(a,b): return a+b\ndef subtract(a,b): return a-b\n")
        for cid in ids:
            await w.verdict_for(cid)
        await w.publish()
        v = await agree(w, "COMPLETE")
        check(v.state == COMPLETE, f"state is complete ({v.state})")
        done = [e for e in BUS.recent(limit=800)
                if e.type == "project.completed"
                and (e.data or {}).get("project") == "p-complete"]
        check(len(done) == 1, f"and exactly one completion event ({len(done)})")


async def test_b_a_stale_project_md_cannot_overrule_the_evidence():
    check.section("§9 the projection is not allowed to be the authority")
    with _tmp() as td:
        w = await World(td, "p-stale").start()
        rev = await w.request()
        ids = await w.criteria(rev)
        w.code()
        await w.verdict_for(ids[0])
        await w.verdict_for(ids[1], verdict=FAILED, error="no subtract")
        await w.publish()

        # Whatever put it there — an older build, a hand edit, a half-finished
        # write — PROJECT.md now disagrees with the evidence.
        (w.path / "PROJECT.md").write_text(
            "# p-stale\n\n## Brief\n" + REQUEST + "\n\n## Status\ncomplete\n\n"
            "## Summary\nall done\n\n## Progress log\n- finished\n",
            encoding="utf-8")

        v = await w.evaluator()
        chat = await w.chat_tool_status()
        check(v.state == FAILING, f"the evidence still says failing ({v.state})")
        check(not chat.lower().startswith("project p-stale: complete"),
              f"and chat does NOT repeat the file's claim ({chat[:70]!r})")
        check(v.state in chat, f"it reports the derived state ({chat[:70]!r})")


async def test_c_non_monotonic_transitions_reach_every_projection():
    check.section("§9 going backwards is projected too")
    with _tmp() as td:
        w = await World(td, "p-moves").start()
        rev1 = await w.request()
        ids = await w.criteria(rev1)
        w.code("def add(a,b): return a+b\ndef subtract(a,b): return a-b\n")
        for cid in ids:
            await w.verdict_for(cid)
        v = await w.publish()
        check(v.state == COMPLETE, f"COMPLETE to begin with ({v.state})")

        # 1. a new requirement
        rev2 = await w.request(REQUEST + " and multiplies")
        await w.svc.carry_forward(slug=w.slug, from_revision=rev1,
                                  to_revision=rev2)
        await w.criteria(rev2, specs=[{"text": "multiplies two numbers",
                                       "origin_quote": "multiplies"}])
        v = await w.publish()
        await agree(w, "after a new requirement")
        check(v.state != COMPLETE,
              f"a new requirement leaves COMPLETE ({v.state})")

        # 2. prove everything again -> COMPLETE
        rows = await w.mem.list_acceptance_criteria(project_name=w.slug,
                                                    revision=rev2)
        w.code("def add(a,b): return a+b\ndef subtract(a,b): return a-b\n"
               "def multiply(a,b): return a*b\n")
        for r in rows:
            await w.verdict_for(r["criterion_id"])
        v = await w.publish()
        check(v.state == COMPLETE, f"complete again ({v.state})")
        await agree(w, "complete again")

        # 3. a regression
        await w.verdict_for(rows[0]["criterion_id"], verdict=FAILED,
                            error="a regression")
        v = await w.publish()
        check(v.state == FAILING, f"a refuted criterion is FAILING ({v.state})")
        await agree(w, "after a regression")

        # 4. artifact drift takes COMPLETE away without any new verdict
        for r in rows:
            await w.verdict_for(r["criterion_id"])
        check((await w.evaluator()).state == COMPLETE, "repaired to complete")
        w.code("def add(a,b): return a+b\n")     # subtract and multiply gone
        v = await w.publish()
        check(v.state != COMPLETE,
              f"editing the code invalidates the evidence ({v.state})")
        check(any("implementation changed" in s.stale_reason
                  for s in v.criteria),
              "and the drift is the stated reason")
        await agree(w, "after drift")


async def main() -> None:
    await test_a_every_state_agrees_everywhere()
    await test_b_a_stale_project_md_cannot_overrule_the_evidence()
    await test_c_non_monotonic_transitions_reach_every_projection()
    check.finish()


if __name__ == "__main__":
    run(main)
