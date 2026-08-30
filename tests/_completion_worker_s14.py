"""One process, one batch of completion operations, then exit (§16 worker).

The restart tests spawn this. Every invocation is a genuinely new interpreter:
new module state, new caches, new connections, nothing carried but the files
on disk. That is the only way to test that a state survives a restart rather
than surviving an attribute.

Reads a JSON batch on argv[2], prints one JSON object on stdout.

    {"root": "...", "ops": [{"op": "request", "slug": "p", "text": "..."}, ...]}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from core.completion import FAILED, PASSED  # noqa: E402
from core.completion_events import CompletionAnnouncer  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402


async def run_batch(root: Path, ops: list[dict]) -> dict:
    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
    await mem.initialize()
    projects = root / "projects"
    svc = CompletionService(memory=mem, projects_dir=projects)
    out: dict = {"pid": os.getpid(), "results": []}

    for spec in ops:
        op = spec["op"]
        slug = spec.get("slug", "")
        res: dict = {"op": op}
        try:
            if op == "request":
                res["revision"] = await svc.record_request(
                    slug=slug, request_text=spec["text"])
            elif op == "criteria":
                (projects / slug).mkdir(parents=True, exist_ok=True)
                res["ids"] = await svc.set_criteria(
                    slug=slug, revision=spec["revision"],
                    criteria=spec["criteria"])
            elif op == "seal":
                await svc.seal_contract(slug=slug, revision=spec["revision"])
                res["sealed"] = True
            elif op == "write":
                p = projects / slug
                p.mkdir(parents=True, exist_ok=True)
                (p / spec.get("name", "main.py")).write_text(
                    spec["body"], encoding="utf-8")
                res["wrote"] = spec.get("name", "main.py")
            elif op == "prove":
                ctx = await svc.begin_check(slug=slug,
                                            criterion_id=spec["criterion_id"])
                verdict = spec.get("verdict", PASSED)
                res["evidence"] = await svc.record_verdict(
                    context=ctx, verdict=verdict,
                    error="did not hold" if verdict == FAILED else "")
                res["context_revision"] = ctx.revision
                res["context_digest"] = ctx.artifact_digest[:12]
            elif op == "ask":
                res["decision_id"] = await svc.ask_human(
                    slug=slug, criterion_id=spec["criterion_id"],
                    prompt=spec.get("prompt", "?"))
            elif op == "resolve":
                await svc.resolve_human_decision(
                    decision_id=spec["decision_id"],
                    accepted=bool(spec.get("accepted", True)),
                    actor=spec.get("actor", "marcus"),
                    channel=spec.get("channel", "ui"))
                res["resolved"] = True
            elif op == "open_decisions":
                rows = await mem.list_human_decisions(project_name=slug,
                                                      open_only=True)
                res["open"] = [{"decision_id": r["decision_id"],
                                "criterion_id": r["criterion_id"],
                                "revision": r["revision"]} for r in rows]
            elif op == "state":
                v = await svc.evaluate(slug=slug)
                res.update({
                    "state": v.state, "revision": v.revision,
                    "reason": (v.reasons[0] if v.reasons else ""),
                    "outstanding": [s.criterion.text for s in v.outstanding],
                    "failing": [s.criterion.text for s in v.failing],
                    "stale": [s.stale_reason for s in v.criteria
                              if getattr(s, "stale_reason", "")],
                    "sealed": v.seal_mode,
                })
            elif op == "announce":
                v = await svc.evaluate(slug=slug)
                announcer = CompletionAnnouncer(memory=mem)
                await announcer.announce(slug=slug, verdict=v)
                res["state"] = v.state
            elif op == "criteria_ids":
                v = await svc.evaluate(slug=slug)
                res["ids"] = [s.criterion.criterion_id for s in v.criteria]
                res["texts"] = [s.criterion.text for s in v.criteria]
            else:
                res["error"] = f"unknown op {op!r}"
        except Exception as e:  # recorded, never swallowed
            res["error"] = f"{type(e).__name__}: {e}"
        out["results"].append(res)

    # BUS.recent() is process-wide, and here that is exactly the right scope:
    # this interpreter was created for this batch and dies with it, so every
    # event in it belongs to these ops. (In-process, this same call is how I
    # twice attributed other tests' events to the code under test.)
    out["published_completed"] = [
        e.data.get("project", "") for e in BUS.recent(500)
        if e.type == "project.completed"]
    await mem.close() if hasattr(mem, "close") else None
    return out


def main() -> None:
    batch = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = asyncio.run(run_batch(Path(batch["root"]), batch["ops"]))
    print("RESULT_JSON " + json.dumps(result))


if __name__ == "__main__":
    main()
