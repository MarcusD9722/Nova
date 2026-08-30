"""Generated completion sequences, checked against an independent oracle (§14).

The oracle here does NOT re-implement `derive_state`. A second copy of the
thing under test agrees with it by construction, including where both are
wrong. What this tracks instead is the history of actions, and from that
history it checks properties that must hold whatever order things happened in:

  * a required criterion whose latest ADMISSIBLE evidence is a failure means
    the project is FAILING — never complete, never merely partial;
  * COMPLETE requires a sealed contract AND admissible satisfying evidence for
    every required criterion. Not "no failures": every one of them, positively;
  * evidence gathered under an earlier requirement revision, or against a
    different implementation, is not admissible and cannot satisfy anything;
  * a criterion nobody ever checked is never satisfied;
  * a waiver with no human decision behind it never satisfies;
  * one project's actions never move another project's state.

Admissibility is computed from what the SEQUENCE did — which revision was
current when a verdict was recorded, and which digest the file had — rather
than by asking the evaluator what it thinks. That is what makes it an oracle
rather than a mirror.

Failures print the seed, the step, the whole action history, both states, the
criteria, the evidence and the violated invariant, so any failure is replayable
exactly.

Run:      venv\\Scripts\\python.exe tests\\test_completion_generated_s14.py
Soak:     NOVA_S14_SEQS=2000 venv\\Scripts\\python.exe tests\\...
One seed: NOVA_S14_SEED=41 NOVA_S14_SEQS=1 venv\\Scripts\\python.exe tests\\...
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, HUMAN_PENDING, IDEA, INCONCLUSIVE, PASSED,
    PASSING, PLANNED, SCAFFOLDED, WAIVED,
)
from core.completion_artifacts import implementation_digest  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

SEQUENCES = int(os.getenv("NOVA_S14_SEQS", "500"))
# The harness arms a 180s faulthandler watchdog that exits the process. At
# roughly two seconds a sequence, 500 of them outrun it and the run dies with
# exit 139 and a thread dump that looks like a hang and is not one. Scale the
# budget with the workload; an explicit setting still wins.
os.environ.setdefault("NOVA_IT_WATCHDOG_S", str(max(180, SEQUENCES * 8)))
BASE_SEED = int(os.getenv("NOVA_S14_SEED", "0"))
PROJECTS = ("alpha", "beta")

REQUEST_PARTS = ["adds numbers", "subtracts numbers", "saves the file",
                 "prints a report", "shows a chart"]

ACTIONS = [
    "ADD_CRITERION", "ADD_CRITERION", "SEAL", "IMPLEMENT", "IMPLEMENT",
    "PASS", "PASS", "FAIL", "INCONCLUSIVE", "ASK_HUMAN", "HUMAN_ACCEPT",
    "CORRECTION", "DRIFT", "STALE_PASS", "DUPLICATE", "RESTART", "REPAIR",
    "SWITCH_PROJECT", "CARRY_FORWARD", "FORGED_WAIVER",
]


#: What the sequences actually reached. A suite that never leaves IDEA passes
#: every invariant here while proving nothing, so the run reports this and
#: fails if the interesting states were not exercised.
COVERAGE: dict[str, int] = {}


def seen(key: str) -> None:
    COVERAGE[key] = COVERAGE.get(key, 0) + 1


def _pool(oracle: "Oracle", slug: str) -> list[str]:
    """The base noise, plus weight on whatever this project still lacks.

    Without this the walk reached COMPLETE zero times in twenty sequences and
    every invariant held vacuously. The bias decides which action is more
    LIKELY, never which is legal -- corrections, drift, stale results and
    restarts stay in the pool throughout.
    """
    st = oracle.p[slug]
    rev = st["revision"]
    live = st["criteria"].get(rev, [])
    pool = list(ACTIONS)
    if st.get("carryable"):
        return pool + ["CARRY_FORWARD"] * 4
    if not live:
        return pool + ["ADD_CRITERION"] * 5
    if _uncovered(st):
        return pool + ["ADD_CRITERION"] * 4
    if not st["sealed"].get(rev):
        return pool + ["SEAL"] * 5
    if not st["implemented"]:
        return pool + ["IMPLEMENT"] * 5
    verdicts = oracle.admissible(slug)
    if any(v == HUMAN_PENDING for v in verdicts.values()):
        return pool + ["ASK_HUMAN"] * 3 + ["HUMAN_ACCEPT"] * 4
    return pool + ["PASS"] * 4 + ["REPAIR"] * 2


def _clauses(st: dict) -> list[str]:
    return [c.strip() for c in st["request"].split(" and ") if c.strip()]


def _uncovered(st: dict) -> list[str]:
    """Clauses of the current request that no live criterion quotes."""
    quotes = st.setdefault("quotes", {}).get(st["revision"], [])
    return [c for c in _clauses(st)
            if not any(c in q or q in c for q in quotes)]


class Oracle:
    """What the SEQUENCE did, and what that requires to be true."""

    def __init__(self):
        # project -> {revision, criteria:{rev:[cid...]}, kinds:{cid:kind},
        #             sealed:{rev:bool}, evidence:[...], digest:str,
        #             implemented:bool}
        self.p: dict[str, dict] = {
            s: {"revision": 0, "criteria": {}, "kinds": {}, "sealed": {},
                "evidence": [], "digest": "", "implemented": False}
            for s in PROJECTS
        }

    def admissible(self, slug: str) -> dict[str, str]:
        """criterion_id -> the verdict that currently applies to it."""
        st = self.p[slug]
        rev, digest = st["revision"], st["digest"]
        live = st["criteria"].get(rev, [])
        out: dict[str, str] = {}
        for cid in live:
            latest = None
            for ev in st["evidence"]:
                if ev["criterion"] != cid:
                    continue
                if ev["revision"] != rev:
                    continue
                if ev["digest"] != digest:
                    continue
                latest = ev
            if latest is None:
                out[cid] = (HUMAN_PENDING if st["kinds"].get(cid) == "human"
                            else "pending")
                continue
            verdict = latest["verdict"]
            if st["kinds"].get(cid) == "human" and verdict == PASSED:
                verdict = HUMAN_PENDING
            if verdict == WAIVED and not latest.get("decision"):
                verdict = HUMAN_PENDING
            out[cid] = verdict
        return out

    def expectations(self, slug: str) -> list[tuple[str, str]]:
        """(invariant name, requirement) pairs the real state must satisfy."""
        st = self.p[slug]
        rev = st["revision"]
        live = st["criteria"].get(rev, [])
        verdicts = self.admissible(slug)
        sealed = st["sealed"].get(rev, False)
        out = []

        if rev == 0:
            out.append(("no requirement", "must be idea"))
            return out
        if not live:
            # Files with no agreed criteria are SCAFFOLDED, not IDEA: nothing
            # is demonstrated, but claiming nothing was built is its own lie.
            out.append(("no criteria",
                        "must be scaffolded" if st["implemented"]
                        else "must be idea"))
            return out
        if any(v == FAILED for v in verdicts.values()):
            out.append(("a required criterion is refuted", "must be failing"))
            return out
        satisfied = [c for c, v in verdicts.items() if v in (PASSED, WAIVED)]
        if len(satisfied) == len(live) and sealed and st["implemented"]:
            out.append(("everything is demonstrated on a sealed contract",
                        "must be complete"))
            return out
        out.append(("something is unproven or the contract is unsealed",
                    "must not be complete"))
        return out


async def one_sequence(seed: int) -> tuple[bool, str]:
    rng = random.Random(seed)
    oracle = Oracle()
    history: list[str] = []
    current = "alpha"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        projects = root / "projects"
        for s in PROJECTS:
            (projects / s).mkdir(parents=True)
        mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
        await mem.initialize()
        svc = CompletionService(memory=mem, projects_dir=projects)

        # Every project starts with a request, so a sequence has something to
        # be about.
        for s in PROJECTS:
            text = " and ".join(rng.sample(REQUEST_PARTS, 2))
            rev = await svc.record_request(slug=s, request_text=text)
            oracle.p[s]["revision"] = rev
            oracle.p[s]["request"] = text
            history.append(f"REQUEST({s}, {text!r}) -> rev {rev}")

        n = rng.randint(14, 34)
        for step in range(n):
            action = rng.choice(_pool(oracle, current))
            st = oracle.p[current]
            rev = st["revision"]
            live = st["criteria"].get(rev, [])
            path = projects / current

            try:
                if action == "ADD_CRITERION" and not st["sealed"].get(rev):
                    gaps = _uncovered(st)
                    part = rng.choice(gaps or _clauses(st))
                    kind = "human" if rng.random() < 0.25 else "machine"
                    ids = await svc.set_criteria(
                        slug=current, revision=rev,
                        criteria=[{"text": f"does {part}",
                                   "origin_quote": part, "verify_kind": kind}])
                    st["criteria"].setdefault(rev, []).extend(ids)
                    st.setdefault("quotes", {}).setdefault(rev, []).append(part)
                    for cid in ids:
                        st["kinds"][cid] = kind
                    history.append(f"ADD_CRITERION({current}, {part!r}, {kind})")

                elif action == "SEAL" and live and not st["sealed"].get(rev):
                    quotes = [q for q in st["request"].split(" and ")]
                    covered = all(
                        any(q in c for c in
                            [x for x in st["request"].split(" and ")])
                        for q in quotes)
                    try:
                        await svc.seal_contract(slug=current, revision=rev)
                        st["sealed"][rev] = True
                        history.append(f"SEAL({current}) -> sealed")
                    except ValueError:
                        history.append(f"SEAL({current}) -> refused (uncovered)")

                elif action == "IMPLEMENT":
                    body = f"# {current} step {step}\nVALUE = {step}\n"
                    (path / "main.py").write_text(body, encoding="utf-8")
                    st["digest"] = implementation_digest(path)
                    st["implemented"] = True
                    history.append(f"IMPLEMENT({current}) -> {st['digest'][:8]}")

                elif action in ("PASS", "FAIL", "INCONCLUSIVE") and live:
                    done = oracle.admissible(current)
                    unmet = [c for c in live
                             if done.get(c) not in (PASSED, WAIVED)]
                    cid = rng.choice(unmet or live)
                    verdict = {"PASS": PASSED, "FAIL": FAILED,
                               "INCONCLUSIVE": INCONCLUSIVE}[action]
                    ctx = await svc.begin_check(slug=current, criterion_id=cid)
                    await svc.record_verdict(context=ctx, verdict=verdict,
                                             error="generated" if verdict == FAILED else "")
                    st["evidence"].append(
                        {"criterion": cid, "revision": ctx.revision,
                         "digest": ctx.artifact_digest, "verdict": verdict,
                         "decision": None})
                    history.append(f"{action}({current}, {cid[:6]})")

                elif action == "ASK_HUMAN" and live:
                    done = oracle.admissible(current)
                    unmet = [c for c in live
                             if st["kinds"].get(c) == "human"
                             and done.get(c) != WAIVED]
                    cid = rng.choice(unmet or live)
                    did = await svc.ask_human(slug=current, criterion_id=cid,
                                              prompt="?")
                    st.setdefault("open", []).append((did, cid))
                    history.append(f"ASK_HUMAN({current}, {cid[:6]})")

                elif action == "HUMAN_ACCEPT" and st.get("open"):
                    did, cid = st["open"].pop()
                    await svc.resolve_human_decision(
                        decision_id=did, accepted=True, actor="marcus",
                        channel="ui")
                    row = await mem.get_human_decision(decision_id=did)
                    st["evidence"].append(
                        {"criterion": cid, "revision": row["revision"],
                         "digest": row["artifact_digest"], "verdict": WAIVED,
                         "decision": did})
                    history.append(f"HUMAN_ACCEPT({current}, {cid[:6]})")

                elif action == "CORRECTION":
                    text = st["request"] + " and " + rng.choice(REQUEST_PARTS)
                    new_rev = await svc.record_request(slug=current,
                                                       request_text=text)
                    st["carryable"] = rev if st["criteria"].get(rev) else None
                    st["revision"] = new_rev
                    st["request"] = text
                    history.append(f"CORRECTION({current}) -> rev {new_rev}")

                elif action == "DRIFT" and st["implemented"]:
                    (path / "main.py").write_text(
                        f"# drifted at {step}\nVALUE = {step * 7}\n",
                        encoding="utf-8")
                    st["digest"] = implementation_digest(path)
                    history.append(f"DRIFT({current}) -> {st['digest'][:8]}")

                elif action == "STALE_PASS" and st["evidence"]:
                    old = rng.choice(st["evidence"])
                    await mem.record_acceptance_evidence(
                        criterion_id=old["criterion"], project_name=current,
                        revision=max(0, old["revision"] - 1),
                        artifact_digest="stale-digest", verdict=PASSED,
                        detail="a late result from an earlier run")
                    history.append(f"STALE_PASS({current}) -> not admissible")

                elif action == "DUPLICATE" and st["evidence"]:
                    old = st["evidence"][-1]
                    await mem.record_acceptance_evidence(
                        criterion_id=old["criterion"], project_name=current,
                        revision=old["revision"],
                        artifact_digest=old["digest"], verdict=old["verdict"],
                        detail="duplicate delivery",
                        decision_id=old.get("decision"))
                    st["evidence"].append(dict(old))
                    history.append(f"DUPLICATE({current})")

                elif action == "FORGED_WAIVER" and live:
                    cid = rng.choice(live)
                    await mem.record_acceptance_evidence(
                        criterion_id=cid, project_name=current,
                        revision=st["revision"], artifact_digest=st["digest"],
                        verdict=WAIVED,
                        detail="a waived row with nothing behind it")
                    st["evidence"].append(
                        {"criterion": cid, "revision": st["revision"],
                         "digest": st["digest"], "verdict": WAIVED,
                         "decision": None})
                    history.append(f"FORGED_WAIVER({current}, {cid[:6]})")

                elif action == "CARRY_FORWARD" and st.get("carryable"):
                    frm = st["carryable"]
                    try:
                        moved = await svc.carry_forward(
                            slug=current, from_revision=frm, to_revision=rev)
                    except ValueError as e:
                        history.append(f"CARRY_FORWARD({current}) refused: "
                                       f"{str(e)[:50]}")
                        moved = []
                    if moved:
                        old_ids = st["criteria"].get(frm, [])
                        st["criteria"].setdefault(rev, []).extend(moved)
                        for new_id, old_id in zip(moved, old_ids):
                            st["kinds"][new_id] = st["kinds"].get(old_id,
                                                                  "machine")
                        st.setdefault("quotes", {}).setdefault(rev, []).extend(
                            st.get("quotes", {}).get(frm, []))
                        st["carryable"] = None
                        history.append(f"CARRY_FORWARD({current}) -> "
                                       f"{len(moved)} carried, no evidence")

                elif action == "RESTART":
                    # A new service and a new memory handle on the SAME store.
                    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
                    await mem.initialize()
                    svc = CompletionService(memory=mem, projects_dir=projects)
                    history.append("RESTART()")

                elif action == "REPAIR" and live:
                    body = f"# repaired at {step}\nVALUE = {step}\n"
                    (path / "main.py").write_text(body, encoding="utf-8")
                    st["digest"] = implementation_digest(path)
                    st["implemented"] = True
                    cid = rng.choice(live)
                    ctx = await svc.begin_check(slug=current, criterion_id=cid)
                    await svc.record_verdict(context=ctx, verdict=PASSED)
                    st["evidence"].append(
                        {"criterion": cid, "revision": ctx.revision,
                         "digest": ctx.artifact_digest, "verdict": PASSED,
                         "decision": None})
                    history.append(f"REPAIR({current}, {cid[:6]})")

                elif action == "SWITCH_PROJECT":
                    current = "beta" if current == "alpha" else "alpha"
                    history.append(f"SWITCH -> {current}")

            except ValueError as e:
                history.append(f"{action}({current}) -> refused: {str(e)[:60]}")

            # ── check every project after every step ────────────────────────
            for slug in PROJECTS:
                actual = (await svc.evaluate(slug=slug)).state
                seen("state:" + actual)
                for name, requirement in oracle.expectations(slug):
                    seen("invariant:" + requirement)
                    ok = True
                    if requirement == "must be idea":
                        ok = actual == IDEA
                    elif requirement == "must be scaffolded":
                        ok = actual == SCAFFOLDED
                    elif requirement == "must be failing":
                        ok = actual == FAILING
                    elif requirement == "must be complete":
                        ok = actual == COMPLETE
                    elif requirement == "must not be complete":
                        ok = actual != COMPLETE
                    if not ok:
                        rows = await mem.list_acceptance_criteria(
                            project_name=slug)
                        evs = await mem.list_acceptance_evidence(
                            project_name=slug)
                        report = "\n".join([
                            f"  seed        : {seed}",
                            f"  step        : {step} ({action})",
                            f"  project     : {slug}",
                            f"  invariant   : {name} -> {requirement}",
                            f"  actual state: {actual}",
                            f"  revision    : {oracle.p[slug]['revision']}",
                            f"  digest      : {oracle.p[slug]['digest'][:12]}",
                            f"  oracle says : {oracle.admissible(slug)}",
                            f"  criteria    : {[(r['criterion_id'][:6], r['text'][:24], r['revision']) for r in rows]}",
                            f"  evidence    : {[(e['criterion_id'][:6], e['verdict'], e['revision'], e['artifact_digest'][:8]) for e in evs]}",
                            "  history     :",
                            *(f"      {h}" for h in history),
                        ])
                        return False, report
    return True, ""


async def test_generated_completion_sequences():
    check.section(f"§14 {SEQUENCES} generated completion sequences")
    failures = []
    for i in range(SEQUENCES):
        ok, report = await one_sequence(BASE_SEED + i)
        if not ok:
            failures.append(report)
            if len(failures) >= 3:
                break
    for report in failures:
        print("\n  VIOLATION\n" + report)
    for key in sorted(COVERAGE):
        print(f"    {key:<34} {COVERAGE[key]}")
    for required in ("state:complete", "state:failing", "state:passing",
                     "state:partially_implemented", "state:scaffolded",
                     "invariant:must be complete", "invariant:must be failing",
                     "invariant:must be scaffolded",
                     "invariant:must not be complete"):
        check(COVERAGE.get(required, 0) >= 5,
              f"{required} was exercised {COVERAGE.get(required, 0)} times")
    check(not failures,
          f"{SEQUENCES - len(failures)}/{SEQUENCES} sequences held every "
          f"invariant" + (f" ({len(failures)} failed)" if failures else ""))


async def main() -> None:
    await test_generated_completion_sequences()
    check.finish()


if __name__ == "__main__":
    run(main)
