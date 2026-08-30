"""Where the contract system stops being able to help (§11, §12, §13).

§11 is not a suite of things that work. It is a RECORD of a boundary, written
down as executable fact so it cannot quietly move. Three of these cases reach
COMPLETE on a contract that does not mean what the request meant, and that is
asserted here deliberately — a limitation nobody has measured is a limitation
nobody knows about.

What the system CAN do is refuse to seal a contract that leaves part of the
request unquoted, and it does. What it cannot do is judge whether a quoted
clause was understood. So the mitigation is not a cleverer check: it is showing
the list to a person and recording whether they agreed.

§12 and §13 then test that provenance and the endpoint that carries it.

Run:  venv\\Scripts\\python.exe tests\\test_completion_semantics_s14.py
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

from core.completion import COMPLETE, PASSED, PASSING  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def bare(td, slug):
    root = Path(td)
    projects = root / "projects"
    (projects / slug).mkdir(parents=True)
    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
    await mem.initialize()
    return mem, CompletionService(memory=mem, projects_dir=projects), projects / slug


async def drive(svc, path, slug, request, criteria, code, verdicts):
    """Contract -> code -> verdicts. Returns (state, seal_or_refusal, ids)."""
    rev = await svc.record_request(slug=slug, request_text=request)
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=criteria)
    try:
        await svc.seal_contract(slug=slug, revision=rev)
        seal = "sealed"
    except ValueError as e:
        seal = str(e)
    (path / "main.py").write_text(code, encoding="utf-8")
    for cid, v in zip(ids, verdicts):
        ctx = await svc.begin_check(slug=slug, criterion_id=cid)
        await svc.record_verdict(context=ctx, verdict=v)
    return (await svc.evaluate(slug=slug)).state, seal, ids


# ── §11: the boundary, written down ─────────────────────────────────────────

async def test_11a_a_weaker_criterion_with_a_genuine_quote():
    check.section("§11 a criterion that quotes correctly and means less")
    with _tmp() as td:
        mem, svc, path = await bare(td, "game")
        state, seal, _ = await drive(
            svc, path, "game", "score increases when a target is hit",
            [{"text": "the game has a score variable",
              "origin_quote": "score increases when a target is hit"}],
            "score = 0\n", [PASSED])
        check(seal == "sealed",
              "the contract seals: every clause of the request is quoted")
        check(state == COMPLETE,
              f"AND IT COMPLETES ({state}) — the criterion is about a variable "
              f"existing, the request was about behaviour, and nothing here "
              f"can tell the difference")
        # What IS true, and is what makes it reviewable.
        rows = await mem.list_acceptance_criteria(project_name="game")
        check(rows[0]["origin_quote"] == "score increases when a target is hit",
              "the quote is preserved, so a person reading the contract can "
              "see the mismatch themselves")


async def test_11b_the_check_proves_something_adjacent():
    check.section("§11 the criterion is right; the check tests the wrong thing")
    with _tmp() as td:
        mem, svc, path = await bare(td, "game2")
        # The criterion states the requirement correctly. The verdict recorded
        # against it came from a check that exercised something else — which
        # the service cannot see, because a verdict is a verdict.
        state, seal, _ = await drive(
            svc, path, "game2", "score increases when a target is hit",
            [{"text": "hitting a target increases the score",
              "origin_quote": "score increases when a target is hit"}],
            "score = 0\ndef hit():\n    pass\n", [PASSED])
        check(state == COMPLETE,
              f"a PASSED verdict completes it ({state}) — the system records "
              f"what a check concluded, never what it actually exercised")


async def test_11c_one_broad_criterion_swallows_three_features():
    check.section("§11 one criterion quoting a whole multi-feature request")
    with _tmp() as td:
        mem, svc, path = await bare(td, "app")
        state, seal, _ = await drive(
            svc, path, "app", "save the file, load the file and print a report",
            [{"text": "the app works",
              "origin_quote": "save the file, load the file and print a report"}],
            "def save():\n    pass\n", [PASSED])
        check(seal == "sealed",
              "coverage is satisfied — the single quote spans every clause")
        check(state == COMPLETE,
              f"and one pass completes all three features ({state}). Coverage "
              f"counts clauses that are QUOTED, not clauses that are UNDERSTOOD")


async def test_11d_what_the_system_does_catch():
    check.section("§11 a clause nobody quoted is caught, every time")
    with _tmp() as td:
        mem, svc, path = await bare(td, "app2")
        state, seal, _ = await drive(
            svc, path, "app2", "save the file and print a report",
            [{"text": "saves the file", "origin_quote": "save the file"}],
            "def save():\n    pass\n", [PASSED])
        check("does not cover everything" in seal and "print a report" in seal,
              f"sealing is refused and names the uncovered clause ({seal[:70]!r})")
        check(state != COMPLETE,
              f"and an unsealed contract cannot complete however much passes "
              f"({state})")
        check(state == PASSING,
              f"it is PASSING: the checks that ran passed, the contract is not "
              f"established ({state})")


# ── §12: the mitigation, and its provenance ────────────────────────────────

async def test_12a_a_contract_can_be_shown_and_confirmed():
    check.section("§12 auto-sealed and human-confirmed are different things")
    with _tmp() as td:
        mem, svc, path = await bare(td, "game3")
        state, seal, _ = await drive(
            svc, path, "game3", "score increases when a target is hit",
            [{"text": "the game has a score variable",
              "origin_quote": "score increases when a target is hit"}],
            "score = 0\n", [PASSED])
        v = await svc.evaluate(slug="game3")
        check(v.seal_mode == "auto",
              f"it starts auto-sealed ({v.seal_mode!r})")

        summary = await svc.contract_summary(slug="game3")
        check(summary["request"] == "score increases when a target is hit"
              and summary["criteria"][0]["origin_quote"] == summary["request"],
              "the contract can be shown, request and quotes together")

        did = await svc.ask_contract_confirmation(slug="game3")
        rev = await svc.resolve_contract_confirmation(
            decision_id=did, accepted=True, actor="marcus", channel="ui")
        v = await svc.evaluate(slug="game3")
        check(v.seal_mode == "human" and rev == 1,
              f"after a person agrees it is human-confirmed ({v.seal_mode!r})")
        check(v.state == COMPLETE,
              "the state itself does not change — confirmation is about "
              "provenance, not about proof")


async def test_12b_confirmation_does_not_survive_a_new_revision():
    check.section("§12 a confirmation applies to the contract it was shown")
    with _tmp() as td:
        mem, svc, path = await bare(td, "game4")
        await drive(svc, path, "game4", "score increases when hit",
                    [{"text": "score increases when hit",
                      "origin_quote": "score increases when hit"}],
                    "score = 0\n", [PASSED])
        did = await svc.ask_contract_confirmation(slug="game4")
        await svc.resolve_contract_confirmation(
            decision_id=did, accepted=True, actor="marcus", channel="ui")
        check((await svc.evaluate(slug="game4")).seal_mode == "human",
              "R1 is human-confirmed")

        rev2 = await svc.record_request(slug="game4",
                                        request_text="score increases when hit "
                                                     "and a sound plays")
        v = await svc.evaluate(slug="game4")
        check(v.revision == rev2 and v.seal_mode == "",
              f"a new requirement is unconfirmed and unsealed "
              f"({v.revision}, {v.seal_mode!r})")
        check(v.state != COMPLETE,
              f"and cannot be complete ({v.state})")


async def test_12c_a_sealed_contract_cannot_be_extended():
    check.section("§12 nothing can be added behind a confirmation's back")
    with _tmp() as td:
        mem, svc, path = await bare(td, "game5")
        rev = await svc.record_request(slug="game5", request_text="score increases")
        await svc.set_criteria(slug="game5", revision=rev, criteria=[
            {"text": "score increases", "origin_quote": "score increases"}])
        await svc.seal_contract(slug="game5", revision=rev)
        did = await svc.ask_contract_confirmation(slug="game5")
        await svc.resolve_contract_confirmation(
            decision_id=did, accepted=True, actor="marcus", channel="chat")

        refused = None
        try:
            await svc.set_criteria(slug="game5", revision=rev, criteria=[
                {"text": "something else", "origin_quote": "score"}])
        except ValueError as e:
            refused = str(e)
        check(refused and "already" in refused and "sealed" in refused,
              f"a criterion cannot join a confirmed contract ({str(refused)[:60]!r})")
        rows = await mem.list_acceptance_criteria(project_name="game5",
                                                  revision=rev)
        check(len(rows) == 1,
              f"and the contract is still the one that was confirmed "
              f"({len(rows)} criteria)")


# ── §13: the endpoint ───────────────────────────────────────────────────────

async def seed_over_http(nova, slug="calc"):
    svc = nova.runtime.completion
    proj = nova.projects_dir / slug
    proj.mkdir(parents=True, exist_ok=True)
    rev = await svc.record_request(slug=slug, request_text="adds two numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds two numbers", "origin_quote": "adds two numbers"}])
    await svc.seal_contract(slug=slug, revision=rev)
    (proj / "main.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
    return svc, rev, ids


async def test_13a_the_endpoint_reports_the_evaluator():
    check.section("§13 GET /completion is the evaluator, not a projection")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed_over_http(nova)
        r = await nova.http.get("/completion/calc")
        body = r.json()
        v = await svc.evaluate(slug="calc")
        check(r.status_code == 200 and body["state"] == v.state,
              f"the API state is the derived state ({body.get('state')} vs {v.state})")
        check(body["criteria"][0]["origin_quote"] == "adds two numbers",
              "and it carries the origin quote, so the contract is reviewable")
        check(body["contract"] == "auto",
              f"and the seal provenance ({body.get('contract')!r})")


async def test_13b_channel_is_decided_by_the_server():
    check.section("§13 a caller cannot claim to be the UI")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed_over_http(nova, "calc2")
        did = await svc.ask_human(slug="calc2", criterion_id=ids[0],
                                  prompt="does this look right?")
        # The body tries to dictate the channel AND the schema has no field
        # for it, so the attempt is simply not represented.
        r = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                 json={"accepted": True, "actor": "marcus",
                                       "channel": "voice"})
        check(r.status_code == 200, f"the answer is accepted ({r.status_code})")
        row = await nova.memory.get_human_decision(decision_id=did)
        check(row["channel"] != "voice",
              f"the claimed channel is not what was recorded "
              f"({row['channel']!r})")
        check(row["channel"] in ("ui", "api"),
              f"the server recorded how the request actually arrived "
              f"({row['channel']!r})")
        check(row["actor"] == "marcus",
              "the actor is recorded as claimed — there is no auth to check it "
              "against, and the record says claimed, not proven")


async def test_13c_every_refusal_says_which_mistake_it_was():
    check.section("§13 refusals are specific, and nothing mutates")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed_over_http(nova, "calc3")

        missing = await nova.http.post(
            "/completion/decisions/does-not-exist/resolve",
            json={"accepted": True, "actor": "marcus"})
        check(missing.status_code == 404
              and "was ever asked" in missing.json()["detail"],
              f"an unknown decision is 404 and says so "
              f"({missing.status_code}, {missing.json().get('detail','')[:40]!r})")

        no_actor = await nova.http.post(
            "/completion/decisions/whatever/resolve",
            json={"accepted": True, "actor": "   "})
        check(no_actor.status_code == 400,
              f"a nameless answer is 400 ({no_actor.status_code})")

        did = await svc.ask_human(slug="calc3", criterion_id=ids[0], prompt="?")
        first = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                     json={"accepted": True, "actor": "marcus"})
        again = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                     json={"accepted": True, "actor": "marcus"})
        check(first.status_code == 200 and again.status_code == 409,
              f"answering twice is a conflict, not a bad request "
              f"({first.status_code}, {again.status_code})")
        check("already answered" in again.json()["detail"],
              "and says which mistake it was")
        rows = [r for r in await nova.memory.list_acceptance_evidence(
            project_name="calc3") if r["verdict"] == "waived"]
        check(len(rows) == 1,
              f"exactly one acceptance was recorded ({len(rows)})")


async def test_13d_a_contract_question_is_answered_as_a_contract():
    check.section("§13 the endpoint routes contract answers correctly")
    async with boot(default_reply="Sure.") as nova:
        svc, rev, ids = await seed_over_http(nova, "calc4")
        asked = await nova.http.post("/completion/calc4/contract/confirm")
        did = asked.json()["decision_id"]
        seen = await nova.http.get("/completion/calc4/contract")
        check(seen.status_code == 200
              and seen.json()["criteria"][0]["text"] == "adds two numbers",
              "the contract can be fetched for a person to read")

        r = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                 json={"accepted": True, "actor": "marcus"})
        check(r.status_code == 200 and r.json()["kind"] == "contract",
              f"it is answered as a contract, not a criterion ({r.json()})")
        v = await svc.evaluate(slug="calc4")
        check(v.seal_mode == "human",
              f"and the provenance moves to human-confirmed ({v.seal_mode!r})")

        # Using the criterion path for a contract question is refused, rather
        # than silently writing evidence against a criterion that is not one.
        did2 = (await nova.http.post(
            "/completion/calc4/contract/confirm")).json().get("decision_id")
        wrong = None
        try:
            await svc.resolve_human_decision(decision_id=did2, accepted=True,
                                             actor="marcus", channel="ui")
        except ValueError as e:
            wrong = str(e)
        check(wrong and "contract" in wrong,
              f"the criterion path refuses a contract question "
              f"({str(wrong)[:60]!r})")


async def test_13e_a_pending_question_survives_a_restart():
    check.section("§13 asking, then restarting, then answering")
    # BOTH boots share an explicit root. Without it the first boot creates a
    # throwaway directory and DELETES it on exit, so the "restart" started from
    # an empty store and the test measured nothing.
    shared = Path(tempfile.mkdtemp(prefix="nova-s14-restart-"))
    async with boot(default_reply="Sure.", root=shared) as nova:
        svc, rev, ids = await seed_over_http(nova, "calc5")
        did = await svc.ask_human(slug="calc5", criterion_id=ids[0], prompt="?")
        open_now = await nova.http.get("/completion/calc5/decisions")
        check(any(d["decision_id"] == did
                  for d in open_now.json()["pending"]),
              "the question is listed as pending")

    # A genuinely new process would be better; a new runtime on the same
    # durable root is what this suite can afford, and it is the same store.
    async with boot(default_reply="Sure.", root=shared) as nova2:
        rows = await nova2.memory.list_human_decisions(project_name="calc5",
                                                       open_only=True)
        check(any(d["decision_id"] == did for d in rows),
              f"it is still pending after a restart ({len(rows)} open)")
        r = await nova2.http.post(f"/completion/decisions/{did}/resolve",
                                  json={"accepted": True, "actor": "marcus"})
        check(r.status_code == 200,
              f"and can still be answered ({r.status_code})")


async def main() -> None:
    await test_11a_a_weaker_criterion_with_a_genuine_quote()
    await test_11b_the_check_proves_something_adjacent()
    await test_11c_one_broad_criterion_swallows_three_features()
    await test_11d_what_the_system_does_catch()
    await test_12a_a_contract_can_be_shown_and_confirmed()
    await test_12b_confirmation_does_not_survive_a_new_revision()
    await test_12c_a_sealed_contract_cannot_be_extended()
    await test_13a_the_endpoint_reports_the_evaluator()
    await test_13b_channel_is_decided_by_the_server()
    await test_13c_every_refusal_says_which_mistake_it_was()
    await test_13d_a_contract_question_is_answered_as_a_contract()
    await test_13e_a_pending_question_survives_a_restart()
    check.finish()


if __name__ == "__main__":
    run(main)
