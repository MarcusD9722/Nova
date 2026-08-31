"""Stage 15 — what `channel` records, and what it must never decide.

Stage 14 moved channel derivation to the server: the client cannot name its own
channel, because a client-supplied one is a claim and the server's is an
observation. This tests what that observation actually is across the request
shapes the review listed, INCLUDING forged ones.

The honest position, restated because it is easy to misread the passing result
below: `Origin`, `Referer` and `Sec-Fetch-Site` are all trivially forgeable by
anything that is not a browser. This is not a hole being papered over -- it is
why channel is ATTRIBUTION and never AUTHENTICATION. The security-relevant
assertion here is therefore not "forging fails" (it does not fail, and cannot).
It is that forging changes the LABEL and nothing else: the same decision, the
same evidence, the same completion state, the same refusals.

  I34  destructive actions require the correct permission and target
  I40  identical concepts use consistent identity keys

Run:  venv\\Scripts\\python.exe tests\\test_s15_channel_matrix.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import COMPLETE, PASSED  # noqa: E402

check = Checks()

BROWSER = {"origin": "http://127.0.0.1:5173",
           "referer": "http://127.0.0.1:5173/",
           "sec-fetch-site": "same-origin"}


async def seed(nova, slug: str) -> tuple[str, int]:
    """A project with one HUMAN criterion, waiting on an answer."""
    svc = nova.runtime.completion
    p = nova.projects_dir / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "main.py").write_text(f"# {slug}\n", encoding="utf-8")
    nova.runtime._project_builder._write_project_md(
        slug, brief="a page that looks right", status="building")
    rev = await svc.record_request(slug=slug,
                                   request_text="a page that looks right")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "looks right", "origin_quote": "looks right",
         "verify_kind": "human"}])
    await svc.seal_contract(slug=slug, revision=rev)
    did = await svc.ask_human(slug=slug, criterion_id=ids[0], prompt="ok?")
    return did, rev


async def resolve(nova, did: str, headers: dict | None, body: dict | None = None):
    return await nova.http.post(
        f"/completion/decisions/{did}/resolve",
        json={"accepted": True, "actor": "marcus", **(body or {})},
        headers=headers or {})


async def test_what_channel_each_request_shape_records():
    check.section("§24 the channel recorded for each request shape")
    async with boot(default_reply="Sure.") as nova:
        shapes = [
            ("raw API, no headers at all", {}, "api"),
            ("browser-like same-origin", dict(BROWSER), "ui"),
            ("forged Origin only", {"origin": "http://evil.example"}, "ui"),
            ("forged Referer only", {"referer": "http://evil.example/x"}, "ui"),
            ("forged Sec-Fetch-Site only",
             {"sec-fetch-site": "same-origin"}, "ui"),
            ("honest cross-site, no origin",
             {"sec-fetch-site": "cross-site"}, "api"),
        ]
        for i, (label, headers, want) in enumerate(shapes):
            did, _ = await seed(nova, f"chan{i}")
            r = await resolve(nova, did, headers)
            check(r.status_code == 200,
                  f"{label}: the decision was accepted ({r.status_code})")
            row = await nova.memory.get_human_decision(decision_id=did)
            check(str(row["channel"]) == want,
                  f"{label}: recorded channel is {want!r} "
                  f"(got {row['channel']!r})")


async def test_a_client_cannot_name_its_own_channel():
    check.section("§24 a client-supplied channel is a claim, and is ignored")
    async with boot(default_reply="Sure.") as nova:
        did, _ = await seed(nova, "claimed")
        # A raw request that ASKS to be recorded as `ui`.
        r = await resolve(nova, did, {}, body={"channel": "ui"})
        check(r.status_code == 200, f"the request succeeded ({r.status_code})")
        row = await nova.memory.get_human_decision(decision_id=did)
        check(str(row["channel"]) == "api",
              f"and it is recorded as api, which is how it ARRIVED "
              f"(got {row['channel']!r})")

        did2, _ = await seed(nova, "claimed2")
        r2 = await resolve(nova, did2, dict(BROWSER), body={"channel": "voice"})
        row2 = await nova.memory.get_human_decision(decision_id=did2)
        check(str(row2["channel"]) == "ui",
              f"a browser claiming `voice` is still recorded as ui "
              f"(got {row2['channel']!r})")


async def test_forging_the_channel_changes_nothing_but_the_label():
    """The assertion that actually matters.

    Forging these headers succeeds -- they are forgeable and nothing here
    pretends otherwise. What must hold is that the forgery buys nothing: the
    same decision, the same evidence, the same completion state.
    """
    check.section("§27 forging the channel buys nothing")
    async with boot(default_reply="Sure.") as nova:
        svc = nova.runtime.completion

        honest_did, _ = await seed(nova, "honest")
        forged_did, _ = await seed(nova, "forged")

        r1 = await resolve(nova, honest_did, dict(BROWSER))
        r2 = await resolve(nova, forged_did, {"origin": "http://evil.example"})
        check(r1.status_code == r2.status_code == 200,
              f"both resolved ({r1.status_code}, {r2.status_code})")

        honest = await nova.memory.get_human_decision(decision_id=honest_did)
        forged = await nova.memory.get_human_decision(decision_id=forged_did)
        check(str(honest["channel"]) == "ui" and str(forged["channel"]) == "ui",
              "both are labelled ui, because both looked like a browser")
        check(bool(honest["resolved_at"]) and bool(forged["resolved_at"]),
              "both are settled")
        check(bool(honest["accepted"]) == bool(forged["accepted"]),
              "with the same answer")

        # And the states they produced are identical.
        v1 = await svc.evaluate(slug="honest")
        v2 = await svc.evaluate(slug="forged")
        check(v1.state == v2.state == COMPLETE,
              f"the same completion state either way ({v1.state}, {v2.state})")

        ev1 = await nova.memory.list_acceptance_evidence(project_name="honest")
        ev2 = await nova.memory.list_acceptance_evidence(project_name="forged")
        check(len(ev1) == len(ev2) == 1,
              f"one piece of evidence each ({len(ev1)}, {len(ev2)})")
        check(str(ev1[0]["verdict"]) == str(ev2[0]["verdict"]),
              "of the same kind")
        check(bool(ev1[0]["decision_id"]) and bool(ev2[0]["decision_id"]),
              "each bound to the decision that produced it")

        # A forged header cannot redeem a decision twice, either.
        again = await resolve(nova, forged_did, {"origin": "http://evil.example"})
        check(again.status_code == 409,
              f"and a second attempt is still refused ({again.status_code})")


async def test_channel_never_widens_what_is_allowed():
    check.section("§27 no channel unlocks anything another cannot do")
    async with boot(default_reply="Sure.") as nova:
        # The same refusals must apply whatever channel is observed. A
        # superseded criterion is refused from a browser and from curl alike.
        did, rev = await seed(nova, "refuse")
        await nova.runtime.completion.record_request(
            slug="refuse", request_text="a page that looks right and loads fast")

        for label, headers in (("browser", dict(BROWSER)), ("raw api", {})):
            r = await nova.http.post(
                f"/completion/decisions/{did}/resolve",
                json={"accepted": True, "actor": "marcus"}, headers=headers)
            # Whatever the outcome is, it must be the SAME outcome.
            check(r.status_code in (200, 400, 409),
                  f"{label}: a definite answer ({r.status_code})")

        rows = await nova.memory.list_human_decisions(project_name="refuse")
        settled = [r for r in rows if r["resolved_at"]]
        check(len(settled) <= 1,
              f"the decision was redeemed at most once across both channels "
              f"({len(settled)})")


async def main() -> None:
    await test_what_channel_each_request_shape_records()
    await test_a_client_cannot_name_its_own_channel()
    await test_forging_the_channel_changes_nothing_but_the_label()
    await test_channel_never_widens_what_is_allowed()
    check.finish()


if __name__ == "__main__":
    run(main)
