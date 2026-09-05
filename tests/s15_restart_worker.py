"""One life of Nova, for the Stage 15 restart matrix.

    python tests/s15_restart_worker.py <root> <phase> [args...]

Each invocation is a SEPARATE INTERPRETER booted against the same durable root.
Nothing is shared but storage -- no service objects, no module state, no event
bus, no broker memory. That is the whole point: "it survived a restart" must
mean the next process reconstructed it from disk, not that an object was handed
its own state back.

A `b*` phase ends with `os._exit(0)` while work is in flight. That skips every
`finally`, every shutdown hook and every flush the harness would otherwise run,
which is what a crash actually looks like. Only what was COMMITTED survives.

The last line of stdout is `##NOVA##{json}` -- the authoritative facts of this
life, for the driver to compare against the next one's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import boot  # noqa: E402

from core.completion import INCONCLUSIVE, PASSED  # noqa: E402

SIDE_EFFECTS = "side_effects.log"


def say(**facts) -> None:
    print("##NOVA##" + json.dumps(facts, default=str))
    sys.stdout.flush()


def side_effect(root: Path, note: str) -> int:
    """A real, observable, NON-idempotent act. Counted, never overwritten."""
    p = root / SIDE_EFFECTS
    with p.open("a", encoding="utf-8") as fh:
        fh.write(note + "\n")
    return len(p.read_text(encoding="utf-8").strip().splitlines())


def seed(nova, slug: str) -> None:
    p = nova.projects_dir / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {slug}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("def add(a, b):\n    return a + b\n",
                               encoding="utf-8")


async def contract(nova, slug: str, verdict: str | None = None):
    svc = nova.runtime.completion
    rev = await svc.record_request(slug=slug,
                                   request_text="a tool that adds numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds numbers", "origin_quote": "adds numbers",
         "verify_kind": "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    if verdict is not None:
        ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
        await svc.record_verdict(context=ctx, verdict=verdict,
                                 error=("undecided"
                                        if verdict == INCONCLUSIVE else ""))
    return rev, ids


async def work(nova, slug: str, *, claim: bool = False):
    g = await nova.memory.create_goal(project_name=slug, title=f"{slug} work",
                                      objective=f"{slug} work")
    t = await nova.memory.enqueue_goal_task(goal_id=g, project_name=slug,
                                            tool_name=f"demo.{slug}")
    c = await nova.memory.claim_next_goal_task() if claim else None
    return g, t, c


async def facts(nova, *, rid: str = "") -> dict:
    """Everything authoritative this process can see, by identity."""
    goals = [{"goal_id": str(g["goal_id"]), "project": str(g["project_name"]),
              "status": str(g["status"]), "generation": int(g["generation"])}
             for g in await nova.memory.list_goals(limit=20)]
    tasks = []
    for g in goals:
        for t in await nova.memory.list_goal_tasks(goal_id=g["goal_id"]):
            tasks.append({"task_id": str(t["task_id"]),
                          "project": str(t["project_name"]),
                          "status": str(t["status"]),
                          "generation": int(t["generation"]),
                          "outcome": str(t.get("outcome") or ""),
                          "error": str(t.get("last_error") or "")[:60]})
    states = {}
    for slug in ("alpha", "bravo"):
        if (nova.projects_dir / slug / "PROJECT.md").exists():
            v = await nova.runtime.completion.evaluate(slug=slug)
            req = await nova.memory.current_requirement(project_name=slug)
            states[slug] = {
                "state": str(v.state),
                "reason": (str(v.reasons[0]) if getattr(v, "reasons", None)
                           else ""),
                "revision": (int(req["revision"]) if req else None),
                "sealed": bool(req and req.get("sealed_at")),
            }
    log = nova.root / SIDE_EFFECTS
    broker = nova.runtime._permission_broker
    return {
        "goals": goals,
        "tasks": tasks,
        "completion": states,
        "side_effects": (len(log.read_text(encoding="utf-8").strip()
                             .splitlines()) if log.exists() else 0),
        "pending_permissions": [str(r["request_id"]) for r in broker.pending()],
        "settled_as": (broker.settled_as(rid) if rid else ""),
    }


# ── crash boundaries ────────────────────────────────────────────────────────

async def b1_after_task_creation(nova, root, argv):
    seed(nova, "alpha")
    g, t, _ = await work(nova, "alpha")
    say(goal_id=str(g), task_id=str(t), **await facts(nova))


async def b2_after_task_claim(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    say(goal_id=str(g), task_id=str(t), generation=int(c["generation"]),
        **await facts(nova))


async def b3_after_tool_invocation(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    n = side_effect(root, f"tool ran for {t}")
    say(goal_id=str(g), task_id=str(t), generation=int(c["generation"]),
        ran=n, **await facts(nova))


async def b4_after_side_effect_before_result(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    n = side_effect(root, f"tool ran for {t}")
    # The artifact the tool produced is on disk; the RESULT never reached the
    # task row. This is the gap the restart has to reason about honestly.
    (nova.projects_dir / "alpha" / "generated.py").write_text(
        "VALUE = 1\n", encoding="utf-8")
    say(goal_id=str(g), task_id=str(t), generation=int(c["generation"]),
        ran=n, **await facts(nova))


async def b5_after_task_completion(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    n = side_effect(root, f"tool ran for {t}")
    applied = await nova.memory.complete_goal_task(
        task_id=str(c["task_id"]), status="done", result={"ok": True},
        expected_generation=int(c["generation"]))
    say(goal_id=str(g), task_id=str(t), applied=applied, ran=n,
        **await facts(nova))


async def b6_after_goal_advancement(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    await nova.memory.complete_goal_task(
        task_id=str(c["task_id"]), status="done", result={"ok": True},
        expected_generation=int(c["generation"]))
    await nova.memory.update_goal_status(goal_id=g, status="completed")
    say(goal_id=str(g), task_id=str(t), **await facts(nova))


async def b7_after_completion_evaluation(nova, root, argv):
    seed(nova, "alpha")
    await contract(nova, "alpha", PASSED)
    v = await nova.runtime.completion.evaluate(slug="alpha")
    say(evaluated=str(v.state), **await facts(nova))


async def b8_before_announcement(nova, root, argv):
    seed(nova, "alpha")
    rev, ids = await contract(nova, "alpha", PASSED)
    # Deliberately NOT announced: the evidence is durable, the announcement is
    # the thing the crash destroys.
    say(revision=int(rev), criterion=str(ids[0]), **await facts(nova))


async def b9_interleaved_ab(nova, root, argv):
    seed(nova, "alpha")
    seed(nova, "bravo")
    g_a, t_a, c_a = await work(nova, "alpha", claim=True)
    g_b, t_b, _ = await work(nova, "bravo")
    await contract(nova, "alpha", PASSED)
    await contract(nova, "bravo")
    side_effect(root, f"tool ran for {t_a}")
    say(goal_a=str(g_a), task_a=str(t_a), goal_b=str(g_b), task_b=str(t_b),
        generation_a=int(c_a["generation"]), **await facts(nova))


async def b9b_two_conversations(nova, root, argv):
    """Two conversations, two projects, one crash."""
    from uuid import uuid4
    seed(nova, "alpha")
    seed(nova, "bravo")
    conv_a, conv_b = str(uuid4()), str(uuid4())
    await nova.brain.chat("Remember the codeword ZULUALPHA for alpha",
                          conversation_id=conv_a)
    await nova.brain.chat("Remember the codeword ZULUBRAVO for bravo",
                          conversation_id=conv_b)
    # Ingestion is QUEUED, so wait on the durable fact rather than on a clock:
    # crashing before the write lands would measure the race, not attribution.
    landed = 0
    for _ in range(300):
        hits = await nova.memory.search("ZULUALPHA", limit=5)
        if hits:
            landed = len(hits)
            break
        await asyncio.sleep(0.05)
    say(conv_a=conv_a, conv_b=conv_b, landed=landed, **await facts(nova))


async def act_conversations(nova, root, argv):
    """Did each codeword stay in the conversation that said it?"""
    from uuid import UUID
    conv_a, conv_b = UUID(argv[0]), UUID(argv[1])
    out = {}
    for label, conv in (("a", conv_a), ("b", conv_b)):
        for word in ("ZULUALPHA", "ZULUBRAVO"):
            hits = await nova.memory.search(word, conversation_id=conv,
                                            limit=10)
            out[f"{label}:{word}"] = len(hits)
    say(scoped=out, **await facts(nova))


async def b10_stale_generation_queued(nova, root, argv):
    seed(nova, "alpha")
    g, t, c = await work(nova, "alpha", claim=True)
    await nova.memory.cancel_goal(goal_id=g)          # -> generation N+1
    say(goal_id=str(g), task_id=str(t), generation=int(c["generation"]),
        **await facts(nova))


async def b11_with_inconclusive(nova, root, argv):
    seed(nova, "alpha")
    rev, ids = await contract(nova, "alpha", INCONCLUSIVE)
    say(revision=int(rev), criterion=str(ids[0]), **await facts(nova))


async def b12_pending_permission(nova, root, argv):
    seed(nova, "alpha")
    broker = nova.runtime._permission_broker
    raised = await broker.request("project.delete",
                                  details={"project": "alpha",
                                           "name": "alpha"})
    rid = str(raised.get("request_id") or "")
    say(request_id=rid, decision=str(raised.get("decision")),
        **await facts(nova, rid=rid))


# ── what the NEXT life does ────────────────────────────────────────────────

async def inspect(nova, root, argv):
    say(**await facts(nova, rid=(argv[0] if argv else "")))


async def act_claim(nova, root, argv):
    """Can anything still be claimed and run a second time?"""
    c = await nova.memory.claim_next_goal_task()
    ran = 0
    if c is not None:
        ran = side_effect(root, f"tool ran AGAIN for {c['task_id']}")
    say(claimed=(None if c is None else {"task_id": str(c["task_id"]),
                                         "project": str(c["project_name"]),
                                         "generation": int(c["generation"])}),
        ran_again=bool(c is not None), side_effects_now=ran,
        **await facts(nova))


async def act_complete_stale(nova, root, argv):
    """A worker from the previous life reports success after the restart."""
    task_id, gen = argv[0], int(argv[1])
    outcome = await nova.memory.complete_goal_task(
        task_id=task_id, status="done", result={"ok": True},
        expected_generation=gen)
    # POSITIVE CONTROL: the same call, on live work in this same process,
    # must still apply -- otherwise "refused" above would prove nothing.
    g, t, c = await work(nova, "alpha", claim=True)
    live = await nova.memory.complete_goal_task(
        task_id=str(c["task_id"]), status="done", result={"ok": True},
        expected_generation=int(c["generation"]))
    say(outcome=str(outcome), live_outcome=str(live), live_task=str(t),
        **await facts(nova))


async def act_decide(nova, root, argv):
    """Liveness: a check that DOES decide, in the new process."""
    slug, criterion = argv[0], argv[1]
    svc = nova.runtime.completion
    ctx = await svc.begin_check(slug=slug, criterion_id=criterion)
    await svc.record_verdict(context=ctx, verdict=PASSED)
    say(**await facts(nova))


async def act_permission(nova, root, argv):
    """Is the dead life's request approvable? Is a NEW one still normal?"""
    old = argv[0]
    broker = nova.runtime._permission_broker
    revived = broker.resolve(old, True)
    settled_old = broker.settled_as(old)
    fresh = await broker.request("project.delete",
                                 details={"project": "alpha", "name": "alpha"})
    new_rid = str(fresh.get("request_id") or "")
    resolved_new = broker.resolve(new_rid, False) if new_rid else False
    say(old_resolved=bool(revived), old_settled_as=str(settled_old),
        new_decision=str(fresh.get("decision")),
        new_resolved=bool(resolved_new),
        new_settled_as=broker.settled_as(new_rid) if new_rid else "",
        **await facts(nova))


PHASES = {fn.__name__: fn for fn in (
    b1_after_task_creation, b2_after_task_claim, b3_after_tool_invocation,
    b4_after_side_effect_before_result, b5_after_task_completion,
    b6_after_goal_advancement, b7_after_completion_evaluation,
    b8_before_announcement, b9_interleaved_ab, b10_stale_generation_queued,
    b9b_two_conversations, b11_with_inconclusive,
    b12_pending_permission, act_conversations,
    inspect, act_claim, act_complete_stale, act_decide, act_permission,
)}


async def main() -> None:
    root = Path(sys.argv[1])
    phase = sys.argv[2]
    argv = sys.argv[3:]
    fn = PHASES[phase]
    async with boot(root=root, default_reply="Sure.") as nova:
        await fn(nova, root, argv)
        if phase.startswith("b"):
            sys.stdout.flush()
            sys.stderr.flush()
            # THE CRASH. No shutdown, no cleanup, no flush of anything that
            # was not already committed.
            os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
