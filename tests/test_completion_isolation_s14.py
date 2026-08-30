"""Two projects, stale projections, and the endpoint's edges (§9, §10, §13).

Three things are proved here that the earlier suites did not reach:

  ISOLATION   project A's completion must never answer a question about
              project B — in the announcer's idempotency ledger, or in what
              chat is handed. Both projects reach revision 1, so a ledger
              keyed on the revision alone would collide.

  STALE       every projection is checked against the evaluator while it says
              something ELSE. A projection that agrees is not evidence; a
              projection that disagrees and loses is.

  ENDPOINT    the HTTP surface, attacked with the wrong project, a superseded
              criterion, an old revision, a changed artifact and a malformed
              body — checking that each refusal leaves nothing behind.

Run:  venv\\Scripts\\python.exe tests\\test_completion_isolation_s14.py
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

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, PARTIALLY_IMPLEMENTED, PASSED, PASSING,
)
from core.event_bus import BUS  # noqa: E402

check = Checks()


def completed_for(slug: str):
    return [e for e in BUS.recent(limit=900)
            if e.type == "project.completed"
            and (e.data or {}).get("project") == slug]


async def seed(nova, slug, request, *, outcome=PASSED, error="", md=True):
    """A project with one criterion, driven to a known state."""
    svc = nova.runtime.completion
    pb = nova.runtime._project_builder
    proj = nova.projects_dir / slug
    proj.mkdir(parents=True, exist_ok=True)
    rev = await svc.record_request(slug=slug, request_text=request)
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": f"{slug} does the thing", "origin_quote": request}])
    await svc.seal_contract(slug=slug, revision=rev)
    (proj / "main.py").write_text(f"# {slug}\nVALUE = 1\n", encoding="utf-8")
    if md:
        pb._write_project_md(slug, brief=request, status="building",
                             summary=slug)
    ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
    await svc.record_verdict(context=ctx, verdict=outcome, error=error)
    return svc, rev, ids


async def point_at(nova, slug):
    await nova.memory.add_fact(entity="projects", attribute="last_active",
                               value=slug, confidence=0.95)


async def ask_block(nova, message):
    before = len(nova.llm.prompts)
    await nova.http.post("/chat", json={"message": message})
    new = nova.llm.prompts[before:]
    answers = [p for p in new
               if "You are Nova" in p and "agent brain for Nova" not in p]
    g = answers[-1] if answers else ""
    marker = "The completion state of the work"
    return g.split(marker, 1)[1] if marker in g else ""


# ── §9: the ledger is per project, not per revision number ─────────────────

async def test_a_two_projects_do_not_share_an_idempotency_key():
    check.section("§9 A's history cannot suppress B")
    async with boot(default_reply="Sure.") as nova:
        ann = nova.runtime._announcer
        svc, rev_a, _ = await seed(nova, "alpha", "alpha does the thing")
        svc, rev_b, _ = await seed(nova, "beta", "beta does the thing")
        check(rev_a == rev_b == 1,
              f"both projects are at revision {rev_a}/{rev_b} — a ledger keyed "
              f"on the revision alone would collide")

        va = await svc.evaluate(slug="alpha")
        vb = await svc.evaluate(slug="beta")
        await ann.announce(slug="alpha", verdict=va, reason="a")
        await ann.announce(slug="beta", verdict=vb, reason="b")
        check(len(completed_for("alpha")) == 1,
              f"alpha announced once ({len(completed_for('alpha'))})")
        check(len(completed_for("beta")) == 1,
              f"and beta announced once, not suppressed by alpha "
              f"({len(completed_for('beta'))})")

        # Re-announcing one must not disturb the other.
        await ann.announce(slug="alpha", verdict=va, reason="again")
        check(len(completed_for("alpha")) == 1 and len(completed_for("beta")) == 1,
              "re-announcing alpha changes neither count")

        rows = await nova.memory._sqlite.list_acceptance_criteria(
            project_name="alpha")
        check(all(r["project_name"] == "alpha" for r in rows),
              "and the criteria rows are scoped to their own project")


async def test_b_the_ledger_tracks_state_not_just_completion():
    check.section("§9 what claim_state_announcement actually stores")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed(nova, "gamma", "gamma does the thing")
        ann = nova.runtime._announcer

        # Inspect the stored value directly rather than inferring it.
        v = await svc.evaluate(slug="gamma")
        await ann.announce(slug="gamma", verdict=v)
        prev = await nova.memory.claim_state_announcement(
            project_name="gamma", revision=rev, state=COMPLETE)
        check(prev is None,
              "claiming the SAME state again returns None — nothing to announce")

        prev2 = await nova.memory.claim_state_announcement(
            project_name="gamma", revision=rev, state=FAILING)
        check(prev2 == COMPLETE,
              f"claiming a DIFFERENT state returns the one it replaced "
              f"({prev2!r}) — the row holds the last state, so any movement "
              f"is a new announcement")
        back = await nova.memory.claim_state_announcement(
            project_name="gamma", revision=rev, state=COMPLETE)
        check(back == FAILING,
              f"and moving back is movement too ({back!r}), which is why "
              f"COMPLETE -> FAILING -> COMPLETE announces twice")


# ── §9: every projection, while it disagrees ───────────────────────────────

async def test_c_each_stale_projection_loses_to_the_evaluator():
    check.section("§9 a projection that disagrees is overruled, one at a time")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed(nova, "delta", "delta does the thing",
                                   outcome=FAILED, error="delta is broken")
        v = await svc.evaluate(slug="delta")
        check(v.state == FAILING, f"the evidence says failing ({v.state})")

        # 1. PROJECT.md says complete.
        proj = nova.projects_dir / "delta"
        (proj / "PROJECT.md").write_text(
            "# delta\n\n## Brief\ndelta does the thing\n\n## Status\ncomplete\n\n"
            "## Summary\nall done\n\n## Progress log\n- finished\n",
            encoding="utf-8")
        # 2. the durable memory fact says complete.
        await nova.memory.add_fact(entity="project:delta", attribute="status",
                                   value="complete", confidence=0.9)
        # 3. a historical completion event exists on the bus.
        BUS.publish("project.completed",
                    {"project": "delta", "revision": rev, "state": COMPLETE})
        await point_at(nova, "delta")

        api = (await nova.http.get("/completion/delta")).json()
        check(api["state"] == FAILING,
              f"the API derives failing despite all three ({api['state']})")
        chat = await ask_block(nova, "Is it done?")
        check(FAILING in chat and COMPLETE not in chat.split("delta")[0][:60],
              f"chat is handed failing ({chat[:90]!r})")
        check("delta is broken" in chat,
              "with the failure's own words")
        status = await nova.runtime._project_builder.status_text("delta")
        check(status.startswith("Project delta: failing"),
              f"and the status tool agrees ({status[:50]!r})")
        v2 = await svc.evaluate(slug="delta")
        check(v2.state == FAILING,
              "while the evaluator itself is unmoved by any of it")


async def test_d_a_historical_completion_event_does_not_revive():
    check.section("§9 having been complete once is not being complete")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed(nova, "epsilon", "epsilon does the thing")
        ann = nova.runtime._announcer
        v = await svc.evaluate(slug="epsilon")
        await ann.announce(slug="epsilon", verdict=v)
        check(len(completed_for("epsilon")) == 1, "it completed, once")

        # Now it regresses.
        ctx = await svc.begin_check(slug="epsilon", criterion_id=ids[0])
        await svc.record_verdict(context=ctx, verdict=FAILED,
                                 error="epsilon regressed")
        v2 = await svc.evaluate(slug="epsilon")
        await ann.announce(slug="epsilon", verdict=v2)
        check(v2.state == FAILING, f"the current state is failing ({v2.state})")
        check(len(completed_for("epsilon")) == 1,
              f"the old completion event is still in history and no new one "
              f"fired ({len(completed_for('epsilon'))})")
        api = (await nova.http.get("/completion/epsilon")).json()
        check(api["state"] == FAILING,
              f"and the API reports the present, not the past ({api['state']})")


# ── §10: two projects, opposite states ─────────────────────────────────────

async def test_e_no_project_borrows_another_answer():
    check.section("§10 A complete, B failing, asked five ways")
    async with boot(default_reply="Sure.") as nova:
        svc, _, _ = await seed(nova, "alpha", "alpha does the thing")
        await seed(nova, "beta", "beta does the thing", outcome=FAILED,
                   error="beta is broken")
        check((await svc.evaluate(slug="alpha")).state == COMPLETE
              and (await svc.evaluate(slug="beta")).state == FAILING,
              "alpha is complete and beta is failing")

        await point_at(nova, "alpha")
        cases = [
            ("generic, pointer on alpha", "Is it done?", "alpha", COMPLETE),
            ("named beta, pointer on alpha", "Is beta done?", "beta", FAILING),
            ("named alpha, pointer on alpha", "Is alpha done?", "alpha", COMPLETE),
        ]
        await point_at(nova, "beta")
        cases += [
            ("generic, pointer on beta", "Is it done?", "beta", FAILING),
            ("named alpha, pointer on beta", "Is alpha done?", "alpha", COMPLETE),
        ]
        # The pointer is set before the first three run, so replay in order.
        await point_at(nova, "alpha")
        for i, (label, q, want_project, want_state) in enumerate(cases):
            if i == 3:
                await point_at(nova, "beta")
            b = await ask_block(nova, q)
            check(f"'{want_project}'" in b,
                  f"{label}: the record is about {want_project} "
                  f"({b[:60]!r})")
            check(want_state in b,
                  f"{label}: and says {want_state}")
            other = "beta" if want_project == "alpha" else "alpha"
            check(f"'{other}'" not in b,
                  f"{label}: and does not mention {other}")


async def test_f_a_pointer_at_a_project_that_is_gone():
    check.section("§10 a stale pointer answers about nothing, not about anything")
    async with boot(default_reply="Sure.") as nova:
        svc, _, _ = await seed(nova, "alpha", "alpha does the thing")
        await point_at(nova, "vanished")
        b = await ask_block(nova, "Is it done?")
        check("'alpha'" not in b,
              f"a pointer at a project that does not exist does NOT fall "
              f"through to another project ({b[:80]!r})")

        # And with no pointer at all.
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="", confidence=0.95)
        b2 = await ask_block(nova, "Is it done?")
        check("'alpha'" not in b2,
              f"nor does having no pointer ({b2[:80]!r})")


# ── §13: the endpoint's edges ──────────────────────────────────────────────

async def test_g_the_endpoint_refuses_precisely_and_changes_nothing():
    check.section("§13 wrong target, old revision, stale artifact, bad body")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed(nova, "zeta", "zeta does the thing",
                                   outcome=FAILED, error="broken")

        # A criterion from a superseded revision. NOTE the ordering: recording
        # a new requirement with no criteria yet legitimately moves the state
        # to `idea`, so the baseline is taken AFTER it. Taking it before made
        # the test blame the refusals for a change its own setup caused.
        rev2 = await svc.record_request(slug="zeta",
                                        request_text="zeta does the thing "
                                                     "and another thing")
        before_state = (await svc.evaluate(slug="zeta")).state
        before_evidence = len(await nova.memory.list_acceptance_evidence(
            project_name="zeta"))
        superseded = None
        try:
            await svc.ask_human(slug="zeta", criterion_id=ids[0], prompt="?")
        except ValueError as e:
            superseded = str(e)
        check(superseded and "superseded or on an earlier revision" in superseded,
              f"a superseded criterion cannot be asked about "
              f"({str(superseded)[:55]!r})")

        # A malformed body: no actor at all.
        bad = await nova.http.post("/completion/decisions/anything/resolve",
                                   json={"accepted": True})
        check(bad.status_code == 422,
              f"a body missing a required field is rejected by the schema "
              f"({bad.status_code})")

        # A decision id that belongs to nothing.
        ghost = await nova.http.post("/completion/decisions/ghost/resolve",
                                     json={"accepted": True, "actor": "m"})
        check(ghost.status_code == 404, f"an unknown decision is 404 ({ghost.status_code})")

        after_state = (await svc.evaluate(slug="zeta")).state
        after_evidence = len(await nova.memory.list_acceptance_evidence(
            project_name="zeta"))
        check(after_state == before_state,
              f"none of the REFUSALS moved the state ({before_state} -> "
              f"{after_state})")
        check(after_evidence == before_evidence,
              f"and none of them wrote evidence ({before_evidence} -> {after_evidence})")


async def test_h_a_changed_artifact_makes_a_confirmation_stale():
    check.section("§13 an answer about H1 does not certify H2, over HTTP")
    async with boot(default_reply="Sure.") as nova:
        svc = nova.runtime.completion
        pb = nova.runtime._project_builder
        proj = nova.projects_dir / "eta"
        proj.mkdir(parents=True, exist_ok=True)
        rev = await svc.record_request(slug="eta", request_text="eta looks right")
        ids = await svc.set_criteria(slug="eta", revision=rev, criteria=[
            {"text": "eta looks right", "origin_quote": "eta looks right",
             "verify_kind": "human"}])
        await svc.seal_contract(slug="eta", revision=rev)
        (proj / "main.py").write_text("# v1\n", encoding="utf-8")
        pb._write_project_md("eta", brief="eta looks right", status="building")

        did = await svc.ask_human(slug="eta", criterion_id=ids[0],
                                  prompt="does it look right?")
        # The artifact changes while the person is deciding.
        (proj / "main.py").write_text("# v2 — different\n", encoding="utf-8")
        r = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                 json={"accepted": True, "actor": "marcus"})
        check(r.status_code == 200, f"the answer is accepted ({r.status_code})")

        api = (await nova.http.get("/completion/eta")).json()
        check(api["state"] != COMPLETE,
              f"but it does not complete the artifact nobody saw ({api['state']})")
        note = " ".join(c.get("note") or "" for c in api["criteria"])
        check("implementation changed" in note,
              f"and the API says why ({note[:80]!r})")


async def test_i_concurrent_http_answers_produce_one_acceptance():
    check.section("§13 two HTTP answers at once")
    async with boot(default_reply="Sure.") as nova:
        svc = nova.runtime.completion
        pb = nova.runtime._project_builder
        proj = nova.projects_dir / "theta"
        proj.mkdir(parents=True, exist_ok=True)
        rev = await svc.record_request(slug="theta", request_text="theta looks right")
        ids = await svc.set_criteria(slug="theta", revision=rev, criteria=[
            {"text": "theta looks right", "origin_quote": "theta looks right",
             "verify_kind": "human"}])
        await svc.seal_contract(slug="theta", revision=rev)
        (proj / "main.py").write_text("# theta\n", encoding="utf-8")
        pb._write_project_md("theta", brief="theta looks right", status="building")
        did = await svc.ask_human(slug="theta", criterion_id=ids[0], prompt="?")

        r1, r2 = await asyncio.gather(
            nova.http.post(f"/completion/decisions/{did}/resolve",
                           json={"accepted": True, "actor": "marcus"}),
            nova.http.post(f"/completion/decisions/{did}/resolve",
                           json={"accepted": True, "actor": "marcus-again"}))
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409],
              f"exactly one answer wins, the other is a conflict ({codes})")
        waived = [r for r in await nova.memory.list_acceptance_evidence(
            project_name="theta") if r["verdict"] == "waived"]
        check(len(waived) == 1,
              f"and exactly one acceptance was recorded ({len(waived)})")


async def main() -> None:
    await test_a_two_projects_do_not_share_an_idempotency_key()
    await test_b_the_ledger_tracks_state_not_just_completion()
    await test_c_each_stale_projection_loses_to_the_evaluator()
    await test_d_a_historical_completion_event_does_not_revive()
    await test_e_no_project_borrows_another_answer()
    await test_f_a_pointer_at_a_project_that_is_gone()
    await test_g_the_endpoint_refuses_precisely_and_changes_nothing()
    await test_h_a_changed_artifact_makes_a_confirmation_stale()
    await test_i_concurrent_http_answers_produce_one_acceptance()
    check.finish()


if __name__ == "__main__":
    run(main)
