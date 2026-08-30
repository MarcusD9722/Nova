"""Completion state across real process boundaries (§16).

Every phase below runs in a SEPARATE interpreter, spawned as a subprocess and
allowed to exit before the next one starts. Nothing is shared but the files on
disk. The journeys suite restarts by rebinding a new service inside one
process, which proves the state is not held in that object; this proves it is
not held in the process either — no module-level cache, no warm connection, no
lru_cache primed by an earlier call.

What is asserted here:

  * each of the seven states is established in one process and READ BACK in
    the next, with the same reason
  * a human question asked in one process is answerable in a later one, and
    the project stays incomplete in every process in between
  * evidence recorded before a restart does not certify code edited after it,
    including edits made while NO process was running
  * the completion announcement is claimed once across processes, which is
    the defect that made the ledger durable in the first place

Run:  venv\\Scripts\\python.exe tests\\test_completion_restart_s14.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, IDEA, PASSED, PASSING, PARTIALLY_IMPLEMENTED,
    PLANNED, SCAFFOLDED,
)

check = Checks()

PY = REPO / "venv" / "Scripts" / "python.exe"
WORKER = REPO / "tests" / "_completion_worker_s14.py"
PIDS: set[int] = set()

REQUEST = "a service that starts up and answers a health check"


def phase(root: Path, ops: list[dict], scratch: Path) -> dict:
    """Run one batch in a brand-new process and return what it saw."""
    batch = scratch / "batch.json"
    batch.write_text(json.dumps({"root": str(root), "ops": ops}),
                     encoding="utf-8")
    proc = subprocess.run([str(PY), str(WORKER), str(batch)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    line = next((l for l in (proc.stdout or "").splitlines()
                 if l.startswith("RESULT_JSON ")), "")
    if not line:
        raise AssertionError(
            f"worker produced no result (exit {proc.returncode})\n"
            f"stdout: {(proc.stdout or '')[-800:]}\n"
            f"stderr: {(proc.stderr or '')[-800:]}")
    out = json.loads(line[len("RESULT_JSON "):])
    PIDS.add(out["pid"])
    for r in out["results"]:
        if r.get("error"):
            raise AssertionError(f"worker op {r['op']} failed: {r['error']}")
    return out


def last_state(out: dict) -> dict:
    return next(r for r in reversed(out["results"]) if r["op"] == "state")


async def test_every_state_survives_a_restart():
    check.section("§16 each of the seven states, established then re-read")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        scratch = root / "scratch"
        scratch.mkdir(parents=True)
        (root / "projects" / "svc").mkdir(parents=True)

        seen: list[tuple[str, str, str]] = []

        def observe(label: str, ops: list[dict]) -> dict:
            """Do the work in one process; read the state back in another."""
            phase(root, ops, scratch)
            out = phase(root, [{"op": "state", "slug": "svc"}], scratch)
            st = last_state(out)
            seen.append((label, st["state"], st["reason"][:60]))
            return st

        st = observe("nothing recorded", [])
        check(st["state"] == IDEA, f"IDEA survives ({st['state']})")

        st = observe("a request", [
            {"op": "request", "slug": "svc", "text": REQUEST}])
        check(st["state"] == IDEA,
              f"a request with no criteria is still IDEA ({st['state']})")

        st = observe("criteria", [
            {"op": "criteria", "slug": "svc", "revision": 1, "criteria": [
                {"text": "starts up", "origin_quote": "starts up",
                 "verify_kind": "machine"},
                {"text": "answers a health check",
                 "origin_quote": "answers a health check",
                 "verify_kind": "machine"}]}])
        check(st["state"] == PLANNED, f"PLANNED survives ({st['state']})")

        ids = phase(root, [{"op": "criteria_ids", "slug": "svc"}],
                    scratch)["results"][0]["ids"]

        st = observe("code", [
            {"op": "seal", "slug": "svc", "revision": 1},
            {"op": "write", "slug": "svc", "body": "def main():\n    return 1\n"}])
        check(st["state"] == SCAFFOLDED, f"SCAFFOLDED survives ({st['state']})")

        st = observe("one criterion proven", [
            {"op": "prove", "slug": "svc", "criterion_id": ids[0],
             "verdict": PASSED}])
        check(st["state"] == PARTIALLY_IMPLEMENTED,
              f"PARTIALLY_IMPLEMENTED survives ({st['state']})")

        st = observe("the other refuted", [
            {"op": "prove", "slug": "svc", "criterion_id": ids[1],
             "verdict": FAILED}])
        check(st["state"] == FAILING, f"FAILING survives ({st['state']})")
        check(any("health check" in t for t in st["failing"]),
              f"and names what is failing ({st['failing']})")

        st = observe("both proven", [
            {"op": "prove", "slug": "svc", "criterion_id": ids[0],
             "verdict": PASSED},
            {"op": "prove", "slug": "svc", "criterion_id": ids[1],
             "verdict": PASSED}])
        check(st["state"] == COMPLETE, f"COMPLETE survives ({st['state']})")

        # PASSING needs an outstanding criterion that only a person can settle.
        (root / "projects" / "hp").mkdir(parents=True, exist_ok=True)
        phase(root, [
            {"op": "request", "slug": "hp", "text": REQUEST},
            {"op": "criteria", "slug": "hp", "revision": 1, "criteria": [
                {"text": "starts up", "origin_quote": "starts up",
                 "verify_kind": "machine"},
                {"text": "answers a health check",
                 "origin_quote": "answers a health check",
                 "verify_kind": "human"}]},
            {"op": "seal", "slug": "hp", "revision": 1},
            {"op": "write", "slug": "hp", "body": "def main():\n    return 1\n"},
        ], scratch)
        hp_ids = phase(root, [{"op": "criteria_ids", "slug": "hp"}],
                       scratch)["results"][0]["ids"]
        phase(root, [{"op": "prove", "slug": "hp", "criterion_id": hp_ids[0],
                      "verdict": PASSED}], scratch)
        hp = last_state(phase(root, [{"op": "state", "slug": "hp"}], scratch))
        seen.append(("awaiting a person", hp["state"], hp["reason"][:60]))
        check(hp["state"] == PASSING, f"PASSING survives ({hp['state']})")

        for label, state, reason in seen:
            print(f"    {label:<28} {state:<24} {reason}")
        states = {s for _, s, _ in seen}
        check(states >= {IDEA, PLANNED, SCAFFOLDED, PARTIALLY_IMPLEMENTED,
                         FAILING, PASSING, COMPLETE},
              f"all seven states were read back from a cold process ({len(states)})")
        check(len(PIDS) >= 15,
              f"across {len(PIDS)} distinct processes")


async def test_a_question_asked_in_one_process_is_answered_in_another():
    check.section("§16 a pending human decision spans processes")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        scratch = root / "scratch"
        scratch.mkdir(parents=True)
        (root / "projects" / "ask").mkdir(parents=True)

        phase(root, [
            {"op": "request", "slug": "ask", "text": REQUEST},
            {"op": "criteria", "slug": "ask", "revision": 1, "criteria": [
                {"text": "starts up", "origin_quote": "starts up",
                 "verify_kind": "machine"},
                {"text": "answers a health check",
                 "origin_quote": "answers a health check",
                 "verify_kind": "human"}]},
            {"op": "seal", "slug": "ask", "revision": 1},
            {"op": "write", "slug": "ask", "body": "def main():\n    return 1\n"},
        ], scratch)
        ids = phase(root, [{"op": "criteria_ids", "slug": "ask"}],
                    scratch)["results"][0]["ids"]
        phase(root, [{"op": "prove", "slug": "ask", "criterion_id": ids[0],
                      "verdict": PASSED}], scratch)

        asked = phase(root, [{"op": "ask", "slug": "ask",
                              "criterion_id": ids[1],
                              "prompt": "Does the health check answer?"}],
                      scratch)
        decision_id = asked["results"][0]["decision_id"]

        # Three processes come and go with the question unanswered.
        for i in range(3):
            out = phase(root, [{"op": "open_decisions", "slug": "ask"},
                               {"op": "state", "slug": "ask"}], scratch)
            open_rows = out["results"][0]["open"]
            st = last_state(out)
            check(len(open_rows) == 1 and open_rows[0]["decision_id"] == decision_id,
                  f"process {i}: the question is still open and findable")
            check(st["state"] != COMPLETE,
                  f"process {i}: and the project is not complete ({st['state']})")

        phase(root, [{"op": "resolve", "decision_id": decision_id,
                      "accepted": True}], scratch)
        st = last_state(phase(root, [{"op": "state", "slug": "ask"}], scratch))
        check(st["state"] == COMPLETE,
              f"answered in a later process, it completes ({st['state']})")

        out = phase(root, [{"op": "open_decisions", "slug": "ask"}], scratch)
        check(not out["results"][0]["open"],
              "and the question is no longer open in any process")


async def test_edits_made_while_nothing_is_running():
    check.section("§16 the file changed while no process existed")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        scratch = root / "scratch"
        scratch.mkdir(parents=True)
        proj = root / "projects" / "drift"
        proj.mkdir(parents=True)

        phase(root, [
            {"op": "request", "slug": "drift", "text": "a script that prints a total"},
            {"op": "criteria", "slug": "drift", "revision": 1, "criteria": [
                {"text": "prints a total", "origin_quote": "prints a total",
                 "verify_kind": "machine"}]},
            {"op": "seal", "slug": "drift", "revision": 1},
            {"op": "write", "slug": "drift", "body": "print(1)\n"},
        ], scratch)
        ids = phase(root, [{"op": "criteria_ids", "slug": "drift"}],
                    scratch)["results"][0]["ids"]
        phase(root, [{"op": "prove", "slug": "drift", "criterion_id": ids[0],
                      "verdict": PASSED}], scratch)
        st = last_state(phase(root, [{"op": "state", "slug": "drift"}], scratch))
        check(st["state"] == COMPLETE, f"complete ({st['state']})")

        # No Nova process is alive at this moment. Something else edits the file.
        (proj / "main.py").write_text("print('something else entirely')\n",
                                      encoding="utf-8")

        st = last_state(phase(root, [{"op": "state", "slug": "drift"}], scratch))
        check(st["state"] != COMPLETE,
              f"the next process does not trust the old pass ({st['state']})")
        check(any("artifact" in s or "digest" in s or "changed" in s
                  for s in st["stale"]),
              f"and says why it is stale ({st['stale']})")

        # A new file appearing counts too.
        (proj / "helper.py").write_text("X = 1\n", encoding="utf-8")
        phase(root, [{"op": "prove", "slug": "drift", "criterion_id": ids[0],
                      "verdict": PASSED}], scratch)
        st = last_state(phase(root, [{"op": "state", "slug": "drift"}], scratch))
        check(st["state"] == COMPLETE,
              f"re-proven against what is there now, it completes ({st['state']})")
        (proj / "helper.py").unlink()
        st = last_state(phase(root, [{"op": "state", "slug": "drift"}], scratch))
        check(st["state"] != COMPLETE,
              f"and DELETING a file invalidates it too ({st['state']})")


async def test_the_announcement_is_claimed_once_across_processes():
    check.section("§16 announced once, however many processes ask")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        scratch = root / "scratch"
        scratch.mkdir(parents=True)
        (root / "projects" / "once").mkdir(parents=True)

        phase(root, [
            {"op": "request", "slug": "once", "text": "a script that prints a total"},
            {"op": "criteria", "slug": "once", "revision": 1, "criteria": [
                {"text": "prints a total", "origin_quote": "prints a total",
                 "verify_kind": "machine"}]},
            {"op": "seal", "slug": "once", "revision": 1},
            {"op": "write", "slug": "once", "body": "print(1)\n"},
        ], scratch)
        ids = phase(root, [{"op": "criteria_ids", "slug": "once"}],
                    scratch)["results"][0]["ids"]
        phase(root, [{"op": "prove", "slug": "once", "criterion_id": ids[0],
                      "verdict": PASSED}], scratch)

        # Six separate processes each evaluate and announce. This is the exact
        # shape of the defect: a fresh process has an empty in-memory ledger.
        announcements = 0
        for i in range(6):
            out = phase(root, [{"op": "announce", "slug": "once"}], scratch)
            announcements += len(out["published_completed"])
        check(announcements == 1,
              f"six processes announced the same transition {announcements} "
              f"time(s)")


async def main() -> None:
    await test_every_state_survives_a_restart()
    await test_a_question_asked_in_one_process_is_answered_in_another()
    await test_edits_made_while_nothing_is_running()
    await test_the_announcement_is_claimed_once_across_processes()
    check.section("processes used")
    check(len(PIDS) >= 30, f"{len(PIDS)} distinct interpreters, none reused")
    check.finish()


if __name__ == "__main__":
    run(main)
