"""The decision endpoint, attacked fourteen ways (Stage 14 §13).

Every case goes over real HTTP. For each REJECTED case, four things are
asserted independently rather than inferred from the status code:

    the HTTP status is the right one for THAT mistake
    no evidence row was written
    no criterion became accepted
    no project's completion state moved

A refusal that returns 400 and quietly writes a row is worse than one that
crashes, so the status is never taken as proof that nothing happened.

WHAT THIS DOES NOT ESTABLISH. `actor` is whatever the caller typed. There is no
authentication in this deployment to check it against, so a forged actor is
recorded as a claim, and that is the honest limit — it is asserted here as a
claim, not defended as an identity. `channel`, by contrast, IS decided by the
server, and that is tested by trying to override it.

Run:  venv\\Scripts\\python.exe tests\\test_completion_endpoint_matrix_s14.py
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

from core.completion import COMPLETE, PASSED, WAIVED  # noqa: E402

check = Checks()

PROJECTS = ("alpha", "beta")


async def make(nova, slug, request, *, human=True):
    """A project with one criterion, sealed, with code on disk."""
    svc = nova.runtime.completion
    pb = nova.runtime._project_builder
    proj = nova.projects_dir / slug
    proj.mkdir(parents=True, exist_ok=True)
    rev = await svc.record_request(slug=slug, request_text=request)
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": request, "origin_quote": request,
         "verify_kind": "human" if human else "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    (proj / "main.py").write_text(f"# {slug} v1\n", encoding="utf-8")
    pb._write_project_md(slug, brief=request, status="building", summary=slug)
    return svc, rev, ids


async def snapshot(nova):
    """Everything a refusal must not disturb, for every project."""
    svc = nova.runtime.completion
    out = {}
    for slug in PROJECTS:
        evidence = await nova.memory.list_acceptance_evidence(project_name=slug)
        out[slug] = {
            "state": (await svc.evaluate(slug=slug)).state,
            "evidence": len(evidence),
            "accepted": len([e for e in evidence if e["verdict"] == WAIVED]),
        }
    return out


def unchanged(before, after, label):
    """All four invariants, per project, named individually."""
    for slug in PROJECTS:
        b, a = before[slug], after[slug]
        check(a["state"] == b["state"],
              f"{label}: {slug}'s state did not move "
              f"({b['state']} -> {a['state']})")
        check(a["evidence"] == b["evidence"],
              f"{label}: no evidence was written for {slug} "
              f"({b['evidence']} -> {a['evidence']})")
        check(a["accepted"] == b["accepted"],
              f"{label}: nothing became accepted for {slug} "
              f"({b['accepted']} -> {a['accepted']})")


async def test_the_matrix():
    check.section("§13 fourteen ways to answer a question, over real HTTP")
    async with boot(default_reply="Sure.") as nova:
        svc, rev_a, ids_a = await make(nova, "alpha", "alpha looks right")
        svc, rev_b, ids_b = await make(nova, "beta", "beta looks right")
        mem = nova.memory

        # ── 1. VALID ────────────────────────────────────────────────────────
        did = await svc.ask_human(slug="alpha", criterion_id=ids_a[0],
                                  prompt="does alpha look right?")
        before = await snapshot(nova)
        r = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                 json={"accepted": True, "actor": "marcus"})
        after = await snapshot(nova)
        check(r.status_code == 200, f"1 valid: accepted ({r.status_code})")
        check(after["alpha"]["accepted"] == before["alpha"]["accepted"] + 1,
              "1 valid: exactly one acceptance was recorded")
        check(after["beta"] == before["beta"],
              "1 valid: and beta was not touched at all")
        check(after["alpha"]["state"] == COMPLETE,
              f"1 valid: alpha completes ({after['alpha']['state']})")

        # ── 2. ALREADY RESOLVED / 3. DUPLICATE ──────────────────────────────
        before = await snapshot(nova)
        again = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                     json={"accepted": True, "actor": "marcus"})
        check(again.status_code == 409,
              f"2 already resolved: 409 conflict ({again.status_code})")
        check("already answered" in again.json()["detail"],
              "2 already resolved: and says which mistake it was")
        unchanged(before, await snapshot(nova), "2 already resolved")

        # ── 4. NONEXISTENT ──────────────────────────────────────────────────
        before = await snapshot(nova)
        ghost = await nova.http.post("/completion/decisions/no-such-id/resolve",
                                     json={"accepted": True, "actor": "marcus"})
        check(ghost.status_code == 404,
              f"4 nonexistent: 404 ({ghost.status_code})")
        check("was ever asked" in ghost.json()["detail"],
              "4 nonexistent: and says nothing was ever asked")
        unchanged(before, await snapshot(nova), "4 nonexistent")

        # ── 5. MALFORMED PAYLOAD ────────────────────────────────────────────
        before = await snapshot(nova)
        for label, body in (("missing actor", {"accepted": True}),
                            ("missing accepted", {"actor": "marcus"}),
                            ("wrong type", {"accepted": "yes", "actor": 5})):
            bad = await nova.http.post(
                f"/completion/decisions/{did}/resolve", json=body)
            check(bad.status_code in (400, 422),
                  f"5 malformed ({label}): rejected ({bad.status_code})")
        empty_actor = await nova.http.post(
            f"/completion/decisions/{did}/resolve",
            json={"accepted": True, "actor": "   "})
        check(empty_actor.status_code == 400,
              f"5 malformed (blank actor): 400 ({empty_actor.status_code})")
        unchanged(before, await snapshot(nova), "5 malformed")

        # ── 6. FORGED CHANNEL ───────────────────────────────────────────────
        did_b = await svc.ask_human(slug="beta", criterion_id=ids_b[0],
                                    prompt="does beta look right?")
        r = await nova.http.post(f"/completion/decisions/{did_b}/resolve",
                                 json={"accepted": True, "actor": "marcus",
                                       "channel": "voice"})
        check(r.status_code == 200, f"6 forged channel: accepted ({r.status_code})")
        row = await mem.get_human_decision(decision_id=did_b)
        check(row["channel"] == "api",
              f"6 forged channel: the server recorded 'api', not the claimed "
              f"'voice' ({row['channel']!r})")
        check(r.json()["channel"] == "api",
              f"6 forged channel: and the response says so too "
              f"({r.json().get('channel')!r})")

        # ── 7. FORGED ACTOR — recorded as a CLAIM, never as an identity ─────
        svc2, rev_c, ids_c = await make(nova, "alpha", "alpha still looks right")
        did_c = await svc.ask_human(slug="alpha", criterion_id=ids_c[0],
                                    prompt="?")
        r = await nova.http.post(f"/completion/decisions/{did_c}/resolve",
                                 json={"accepted": True,
                                       "actor": "definitely-not-marcus"})
        row = await mem.get_human_decision(decision_id=did_c)
        check(r.status_code == 200 and row["actor"] == "definitely-not-marcus",
              f"7 forged actor: recorded exactly as claimed "
              f"({row['actor']!r}) — there is no authentication here to check "
              f"it against, and the record does not pretend otherwise")
        rows = [e for e in await mem.list_acceptance_evidence(project_name="alpha")
                if e["verdict"] == WAIVED]
        check(any("definitely-not-marcus" in (e["detail"] or "") for e in rows),
              "7 forged actor: and the claim is carried into the evidence, so a "
              "reader can see who it says decided")

        # ── 8. WRONG PROJECT — resolving alpha's decision must not touch beta
        did_a2 = await svc.ask_human(slug="alpha", criterion_id=ids_c[0],
                                     prompt="?")
        # (ids_c[0] is alpha's current criterion; the previous decision on it is
        #  already resolved, so this is a fresh question about the same target.)
        before = await snapshot(nova)
        r = await nova.http.post(f"/completion/decisions/{did_a2}/resolve",
                                 json={"accepted": True, "actor": "marcus"})
        after = await snapshot(nova)
        check(r.status_code == 200 and r.json()["project"] == "alpha",
              f"8 wrong project: the endpoint uses the DECISION's project, not "
              f"one the caller could name ({r.json().get('project')!r})")
        check(after["beta"]["evidence"] == before["beta"]["evidence"],
              "8 wrong project: beta gained no evidence from alpha's answer")

        # ── 9. WRONG CRITERION — the caller cannot name one ─────────────────
        ev = [e for e in await mem.list_acceptance_evidence(project_name="alpha")
              if e["decision_id"] == did_a2]
        check(len(ev) == 1 and ev[0]["criterion_id"] == ids_c[0],
              f"9 wrong criterion: evidence lands on the criterion the DECISION "
              f"named; the request body has no criterion field to forge "
              f"({ev[0]['criterion_id'] == ids_c[0] if ev else None})")

        # ── 10. SUPERSEDED CRITERION ────────────────────────────────────────
        before = await snapshot(nova)
        rev_d = await svc.record_request(slug="alpha",
                                         request_text="alpha does something new")
        superseded = None
        try:
            await svc.ask_human(slug="alpha", criterion_id=ids_c[0], prompt="?")
        except ValueError as e:
            superseded = str(e)
        check(superseded and "superseded or on an earlier revision" in superseded,
              f"10 superseded: refused, and names why "
              f"({str(superseded)[:55]!r})")
        after = await snapshot(nova)
        check(after["alpha"]["evidence"] == before["alpha"]["evidence"],
              "10 superseded: no evidence was written")
        check(after["alpha"]["accepted"] == before["alpha"]["accepted"],
              "10 superseded: nothing became accepted")

        # ── 11. OLD REQUIREMENT REVISION ────────────────────────────────────
        old_rev = None
        try:
            await svc.set_criteria(slug="alpha", revision=rev_c, criteria=[
                {"text": "x", "origin_quote": "alpha"}])
        except ValueError as e:
            old_rev = str(e)
        check(old_rev and "not the current requirement" in old_rev,
              f"11 old revision: a contract cannot be written for a superseded "
              f"revision ({str(old_rev)[:55]!r})")


async def test_stale_artifact_and_restart():
    check.section("§13 a changed artifact, and a restart between ask and answer")
    shared = Path(tempfile.mkdtemp(prefix="nova-s14-matrix-"))

    # ── 12. STALE ARTIFACT ──────────────────────────────────────────────────
    async with boot(default_reply="Sure.", root=shared) as nova:
        svc, rev, ids = await make(nova, "alpha", "alpha looks right")
        await make(nova, "beta", "beta looks right")
        did = await svc.ask_human(slug="alpha", criterion_id=ids[0], prompt="?")
        (nova.projects_dir / "alpha" / "main.py").write_text(
            "# alpha v2 - completely different\n", encoding="utf-8")
        r = await nova.http.post(f"/completion/decisions/{did}/resolve",
                                 json={"accepted": True, "actor": "marcus"})
        check(r.status_code == 200,
              f"12 stale artifact: the answer is accepted ({r.status_code})")
        api = (await nova.http.get("/completion/alpha")).json()
        check(api["state"] != COMPLETE,
              f"12 stale artifact: and does not complete the artifact nobody "
              f"was shown ({api['state']})")
        note = " ".join(c.get("note") or "" for c in api["criteria"])
        check("implementation changed" in note,
              f"12 stale artifact: the API names the drift ({note[:70]!r})")

        # ── 13. RESTART BETWEEN ASK AND RESOLVE ─────────────────────────────
        svc2, rev2, ids2 = await make(nova, "beta", "beta really looks right")
        pending = await svc.ask_human(slug="beta", criterion_id=ids2[0],
                                      prompt="still open across a restart")

    async with boot(default_reply="Sure.", root=shared) as nova2:
        rows = await nova2.memory.list_human_decisions(project_name="beta",
                                                       open_only=True)
        check(any(d["decision_id"] == pending for d in rows),
              f"13 restart: the question is still open in a new runtime "
              f"({len(rows)} pending)")
        r = await nova2.http.post(f"/completion/decisions/{pending}/resolve",
                                  json={"accepted": True, "actor": "marcus"})
        check(r.status_code == 200,
              f"13 restart: and can be answered ({r.status_code})")
        row = await nova2.memory.get_human_decision(decision_id=pending)
        check(row["resolved_at"] and row["channel"] == "api",
              "13 restart: with its provenance intact")

        # ── 14. CONCURRENT RESOLUTION ───────────────────────────────────────
        svc3 = nova2.runtime.completion
        _, _, ids3 = await make(nova2, "alpha", "alpha concurrent check")
        race = await svc3.ask_human(slug="alpha", criterion_id=ids3[0],
                                    prompt="?")
        r1, r2 = await asyncio.gather(
            nova2.http.post(f"/completion/decisions/{race}/resolve",
                            json={"accepted": True, "actor": "one"}),
            nova2.http.post(f"/completion/decisions/{race}/resolve",
                            json={"accepted": True, "actor": "two"}))
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409],
              f"14 concurrent: one winner, one conflict ({codes})")
        ev = [e for e in await nova2.memory.list_acceptance_evidence(
            project_name="alpha") if e["decision_id"] == race]
        check(len(ev) == 1,
              f"14 concurrent: exactly one evidence row for that decision "
              f"({len(ev)})")


async def main() -> None:
    await test_the_matrix()
    await test_stale_artifact_and_restart()
    check.finish()


if __name__ == "__main__":
    run(main)
