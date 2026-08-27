"""What a restart does to a permission request (Stage 13C §17).

A permission request is the one place where Nova is holding a person's answer
in her hands. Everything about it lives in memory: the pending future, who
answered, how it ended. The durable half is one append-only audit file.

So a restart asks three separate questions, and they are not the same question:

  SAFETY      can an action that was waiting for approval when the process died
              be approved - and executed - afterwards?
  PRIVILEGE   can a restart end up with MORE permission than before it?
  HONESTY     afterwards, can Nova say truthfully what happened to that
              request? "You were asked and never answered, and nothing ran" is
              a different sentence from "no such request exists", and only one
              of them is true.

Every crash here is a placed CRASH(), and every observation is made by a fresh
interpreter reading the durable audit file or the live API - never by inspecting
an object that survived in memory, because after a real restart none does.

Run:  venv\\Scripts\\python.exe tests\\test_permission_durability_s13c.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "1800")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

PROJ = "flappy-bird"
TERMINAL = {"approved", "rejected", "timeout", "cancelled", "abandoned",
            "already_settled", "denied", "allowed", "interrupted_by_restart"}


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def audit(root: Path) -> list[dict]:
    """The DURABLE trail. Not `audit_log()`, which is this process's memory."""
    p = Path(root) / "memory_data" / "permission_audit.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def for_request(rows: list[dict], rid: str) -> list[dict]:
    return [r for r in rows if str(r.get("request_id") or "") == str(rid)]


#: Make a real project, ask the real tool to delete it, and stop the moment the
#: broker is actually holding a pending request. A barrier on an observable
#: condition - not a sleep, which would either be flaky or slow.
ASK_TO_DELETE = '''
    import asyncio
    from core.tool_router import ToolCall

    proj = nova.projects_dir / "PROJECT_NAME"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "PROJECT.md").write_text("# PROJECT_NAME\\n", encoding="utf-8")
    (proj / "game.py").write_text("print('hi')\\n", encoding="utf-8")

    broker = nova.runtime.permission_broker
    task = asyncio.create_task(
        nova.runtime.router.execute(ToolCall(name="project.delete",
                                             args={"name": "PROJECT_NAME"})))
    for _ in range(500):
        if broker.pending():
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the delete never asked for permission")
    rid = broker.pending()[0]["request_id"]
'''


async def test_a_a_request_the_crash_interrupted_cannot_be_approved_later():
    check.section("§17 approving after the restart executes nothing")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, ASK_TO_DELETE.replace("PROJECT_NAME", PROJ) + '''
    emit({"rid": rid, "mode": broker.mode})
    CRASH()
''', full=True, expect_crash=True)
        # A crash emits nothing; the id has to come from the durable trail.
        rows = audit(root)
        pend = [r for r in rows if r.get("outcome") == "pending"]
        check(len(pend) == 1,
              f"the request reached the durable audit before the crash ({len(pend)})")
        rid = str(pend[0].get("request_id") or "")
        check(pend[0].get("capability") == "project.delete",
              f"and it is the deletion that is waiting ({pend[0].get('capability')})")
        check((root / "projects" / PROJ / "PROJECT.md").exists(),
              "the project is still there when the process dies")

        after = run_step(root, f'''
    broker = nova.runtime.permission_broker
    before_pending = broker.pending()
    r = await nova.http.post("/permissions/resolve",
                             json={{"request_id": "{rid}", "approved": True}})
    emit({{"pending_at_boot": before_pending, "resolve": r.json(),
           "settled_as": broker.settled_as("{rid}")}})
''', full=True)

        res = one(after, "resolve") or {}
        check(one(after, "pending_at_boot") == [],
              f"nothing is left waiting for a click ({one(after, 'pending_at_boot')})")
        check(res.get("resolved") is False and res.get("applied") is False,
              f"a late approval is refused ({res})")
        check(res.get("settled_as") == "interrupted_by_restart"
              and "restarted" in str(res.get("note")),
              f"and the person is told which 'no' this was ({res.get('note')!r})")
        check((root / "projects" / PROJ / "PROJECT.md").exists(),
              "and the project is NOT deleted by it")
        check(not (root / "projects" / ".trash").exists(),
              f"nothing was trashed either "
              f"({list((root / 'projects' / '.trash').glob('*')) if (root / 'projects' / '.trash').exists() else []})")

        # HONESTY. The durable trail is the only thing that outlived the crash,
        # so it has to carry the truth: asked, never answered, nothing ran.
        trail = for_request(audit(root), rid)
        outcomes = [str(r.get("outcome")) for r in trail]
        check(any(o in TERMINAL for o in outcomes[1:]),
              f"the interrupted request is closed out, not left looking live "
              f"({outcomes})")
        check("unknown_request" not in outcomes,
              f"and the late answer is not recorded as being about a request "
              f"that never existed ({outcomes})")
        check(one(after, "settled_as") not in ("", None),
              f"Nova can say how it ended ({one(after, 'settled_as')!r})")


async def test_b_an_approval_before_the_crash_does_not_run_again_after_it():
    check.section("§17 an answered request is not replayed by the restart")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, ASK_TO_DELETE.replace("PROJECT_NAME", PROJ) + '''
    broker.resolve(rid, True, by="test")
    out = await task
    emit({"rid": rid, "ok": out.ok, "result": out.result,
          "settled_as": broker.settled_as(rid)})
''', full=True)
        rid = one(made, "rid")
        check(one(made, "ok") is True,
              f"the approved delete ran ({one(made, 'result')})")
        check(one(made, "settled_as") == "approved",
              f"and is recorded as approved ({one(made, 'settled_as')})")
        gone = not (root / "projects" / PROJ / "PROJECT.md").exists()
        trashed = sorted(p.name for p in (root / "projects" / ".trash").glob("*")) \
            if (root / "projects" / ".trash").exists() else []
        check(gone and trashed,
              f"the project moved to the trash ({trashed})")

        run_step(root, "CRASH()", expect_crash=True, full=False)

        after = run_step(root, f'''
    broker = nova.runtime.permission_broker
    r = await nova.http.post("/permissions/resolve",
                             json={{"request_id": "{rid}", "approved": True}})
    emit({{"pending": broker.pending(), "resolve": r.json()}})
''', full=True)
        again = sorted(p.name for p in (root / "projects" / ".trash").glob("*"))
        check(one(after, "pending") == [],
              f"the restart re-asks nothing ({one(after, 'pending')})")
        check((one(after, "resolve") or {}).get("applied") is False,
              f"and clicking the old approval again does nothing "
              f"({one(after, 'resolve')})")
        check(again == trashed,
              f"the deletion happened exactly once ({trashed} -> {again})")


async def test_c_a_restart_never_grants_more_permission():
    check.section("§17 privilege does not rise across a restart")
    with _tmp() as td:
        root = Path(td) / "n"
        elevated = run_step(root, '''
    broker = nova.runtime.permission_broker
    a = await broker.request("computer.click", details={"what": "ok"})
    broker.set_mode("trusted")       # raised at RUNTIME, in memory only
    b = await broker.request("computer.click", details={"what": "ok"})
    emit({"mode": broker.mode, "before": a["decision"], "after": b["decision"]})
    CRASH()
''', full=True, expect_crash=True)

        back = run_step(root, '''
    broker = nova.runtime.permission_broker
    d = await broker.request("computer.click", details={"what": "ok"})
    emit({"mode": broker.mode, "decision": d["decision"]})
''', full=True)
        check(one(back, "mode") == "guarded",
              f"the mode returns to the configured one ({one(back, 'mode')})")
        check(one(back, "decision") == "needs_confirmation",
              f"so a standard action needs asking again ({one(back, 'decision')})")

        # COUNTER-TEST: it is CONFIGURATION that decides, not the restart. A
        # machine configured as trusted must still come back trusted, or this
        # would only be proving that restarts break things.
        conf = run_step(root, '''
    broker = nova.runtime.permission_broker
    d = await broker.request("computer.click", details={"what": "ok"})
    emit({"mode": broker.mode, "decision": d["decision"]})
''', full=True, env={"NOVA_PERMISSION_MODE": "trusted"})
        check(one(conf, "mode") == "trusted" and one(conf, "decision") == "allowed",
              f"a configured elevation does survive ({one(conf, 'mode')}/"
              f"{one(conf, 'decision')})")


async def test_d_a_refusal_survives_the_restart_as_a_refusal():
    check.section("§17 'no' stays no, and a late 'yes' cannot overturn it")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, ASK_TO_DELETE.replace("PROJECT_NAME", PROJ) + '''
    broker.resolve(rid, False, by="test")
    out = await task
    emit({"rid": rid, "ok": out.ok,
          "status": (out.result or {}).get("status"),
          "outcome": (out.result or {}).get("outcome")})
''', full=True)
        rid = one(made, "rid")
        check(one(made, "ok") is False and one(made, "status") == "not_approved",
              f"the refused delete did not run ({one(made, 'status')})")
        check(one(made, "outcome") == "rejected",
              f"and Nova knows it was refused, not ignored ({one(made, 'outcome')})")

        run_step(root, "CRASH()", expect_crash=True, full=False)

        after = run_step(root, f'''
    r = await nova.http.post("/permissions/resolve",
                             json={{"request_id": "{rid}", "approved": True}})
    emit({{"resolve": r.json(),
           "settled_as": nova.runtime.permission_broker.settled_as("{rid}")}})
''', full=True)
        res = one(after, "resolve") or {}
        check(res.get("applied") is False,
              f"a 'yes' after the restart does not overturn the 'no' ({res})")
        check(res.get("settled_as") == "rejected"
              and "declined" in str(res.get("note")),
              f"and it says so: the refusal, not a shrug ({res.get('note')!r})")
        check((root / "projects" / PROJ / "PROJECT.md").exists(),
              "the project the user refused to delete is still there")
        check(one(after, "settled_as") == "rejected",
              f"and the refusal is still the answer of record "
              f"({one(after, 'settled_as')!r})")


async def test_e_the_audit_trail_is_append_only_across_restarts():
    check.section("§17 three lives, one trail")
    with _tmp() as td:
        root = Path(td) / "n"
        for i in range(3):
            run_step(root, f'''
    await nova.runtime.permission_broker.request(
        "computer.click", details={{"life": {i}}})
    emit({{"life": {i}}})
''', full=True)
        rows = audit(root)
        asked = [r for r in rows if r.get("outcome") == "pending"]
        lives = [((r.get("details") or {}).get("life")) for r in asked]
        check(lives == [0, 1, 2],
              f"every life appended to the same file in order ({lives})")
        check(len({str(r.get("request_id")) for r in asked}) == 3,
              "with three distinct request ids")
        # Nobody ever answered any of them, so each life should have closed out
        # the one before it - and the LAST one is still open, because the
        # process that asked it is the one that just ended.
        closed = [str(r.get("request_id")) for r in rows
                  if r.get("outcome") == "interrupted_by_restart"]
        check(closed == [str(r.get("request_id")) for r in asked[:2]],
              f"each restart closed out exactly the request it inherited "
              f"({len(closed)} of 3)")
        check(len(closed) == len(set(closed)),
              f"and closing out is done once, not once per boot ({closed})")

        # And the endpoint that exists to SHOW the trail can see across the
        # restarts, rather than reporting an empty history beside a file that
        # has three requests in it.
        seen = run_step(root, '''
    r = await nova.http.get("/permissions/audit", params={"limit": 200})
    body = r.json()
    emit({"lives": [(e.get("details") or {}).get("life") for e in body["audit"]
                    if e.get("outcome") == "pending"],
          "pending": body["pending"]})
''', full=True)
        check(sorted(x for x in (one(seen, "lives") or []) if x is not None)
              == [0, 1, 2],
              f"the audit endpoint shows every life, not just this one "
              f"({one(seen, 'lives')})")
        check(one(seen, "pending") == [],
              f"while offering nothing to click ({one(seen, 'pending')})")

        # A fifth life closes out the fourth, and re-reading the same history
        # adds nothing for the ones already closed.
        run_step(root, 'emit({"life": 3})', full=True)
        closed_now = [str(r.get("request_id")) for r in audit(root)
                      if r.get("outcome") == "interrupted_by_restart"]
        check(closed_now == [str(r.get("request_id")) for r in asked],
              f"reading the trail again is idempotent ({len(closed_now)} of 3)")
        check(len(closed_now) == len(set(closed_now)),
              f"after five boots over the same history ({closed_now})")


async def main() -> None:
    await test_a_a_request_the_crash_interrupted_cannot_be_approved_later()
    await test_b_an_approval_before_the_crash_does_not_run_again_after_it()
    await test_c_a_restart_never_grants_more_permission()
    await test_d_a_refusal_survives_the_restart_as_a_refusal()
    await test_e_the_audit_trail_is_append_only_across_restarts()
    check.finish()


if __name__ == "__main__":
    run(main)
