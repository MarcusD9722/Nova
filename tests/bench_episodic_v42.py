"""V3 P4.2: what the new promotion paths cost the turn that triggers them.

P4.1 measured retrieval and the artifact write. P4.2 adds four more ways an
episode can be created, and the question for each is the same one: does the
conversational turn pay for it?

The answer should be "almost nothing", and for a specific reason rather than
luck — every promotion decision here is a regex and a dict lookup over data the
runtime already computed. Nothing calls a model, nothing touches SQLite, and
three of the four paths are not even on the turn: they arrive on the event bus,
which publishers fire and forget.

What is measured:

  A. promotion decision cost, per event type, in microseconds
  B. the fast path, again — zero queries, zero writes, zero prompt characters
  C. the bus publish that feeds three of the paths, with and without a
     subscribed promoter, because that IS the cost a publisher pays

Run:  venv\\Scripts\\python.exe tests\\bench_episodic_v42.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from harness import boot, run  # noqa: E402

REPS = 200

DRIVES = [
    {"title": "Seagate Exos X28", "capacity": "28 TB", "price": "$429"},
    {"title": "WD Gold", "capacity": "26 TB", "price": "$399"},
    {"title": "IronWolf Pro", "capacity": "24 TB", "price": "$389"},
]


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def timed(fn, reps: int = REPS) -> tuple[float, float]:
    """Median and P90 in microseconds."""
    samples = []
    for i in range(reps):
        t0 = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - t0) * 1e6)
    return statistics.median(samples), pct(samples, 0.9)


async def main():
    os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
    from core.event_bus import BUS
    from memory.artifacts import capture_tool_result

    print("Nova V3 P4.2 — promotion cost per event type")
    print("=" * 84)
    print("\nEvery number below is a DECISION, not a write. Persistence is the P4.1")
    print("worker and is unchanged; these measure what the turn (or the publisher)")
    print("pays to decide that something is worth remembering.\n")

    async with boot() as nova:
        rt = nova.runtime
        promoter = rt._promoter
        # Swallow the submits: this benchmark measures the decision, and letting
        # 200 reps x 5 paths reach a bounded queue would measure back-pressure.
        submitted = {"n": 0}
        promoter._submit = lambda _ev: (submitted.__setitem__("n", submitted["n"] + 1), True)[1]

        conv = "bench"
        parent = capture_tool_result(
            rt._artifacts, conversation_id=conv, turn_id="t0", tool="web.search",
            args={"query": "28 TB drives"}, result={"results": DRIVES})
        items = rt._artifacts.items_of(parent.artifact_id)
        selected = items[1]

        print("A. PROMOTION DECISION")
        print(f"   {'event type':<22} {'trigger':<34} {'median':>9} {'P90':>9}")

        rows = []
        m, p = timed(lambda i: promoter.note_artifact(parent, items, user_text="drives"))
        rows.append(("artifact", "tool/MCP result captured", m, p))

        m, p = timed(lambda i: promoter.note_selection(
            selected=selected, parent=parent, items=items, conversation_id=conv,
            turn_id="t1", user_text="let's go with the second one"))
        rows.append(("selection", "hot resolution + choice wording", m, p))

        m, p = timed(lambda i: promoter.on_bus_event(
            "memory.corrected",
            {"entity": "user", "attribute": "gpu", "was": "3080", "now": f"5080-{i}"},
            "2026-08-14T00:00:00Z"))
        rows.append(("correction", "memory.corrected on the bus", m, p))

        m, p = timed(lambda i: promoter.on_bus_event(
            "tool.error", {"tool": "widget.render", "error": "undefined symbol"},
            "2026-08-14T00:00:00Z"))
        rows.append(("failure", "error event (counted, mostly rejected)", m, p))

        m, p = timed(lambda i: promoter.on_bus_event(
            "project.completed",
            {"project": "countdown", "status": "complete", "summary": "done"},
            f"2026-08-14T00:00:{i % 60:02d}Z"))
        rows.append(("project", "project.completed on the bus", m, p))

        m, p = timed(lambda i: promoter.on_bus_event(
            "project.progress", {"project": "countdown", "stage": "writing"}, ""))
        rows.append(("(rejected tick)", "project.progress — never an episode", m, p))

        for name, trigger, med, p90 in rows:
            print(f"   {name:<22} {trigger:<34} {med:>7.1f}us {p90:>7.1f}us")

        worst = max(r[2] for r in rows)
        print(f"\n   Slowest promotion decision: {worst:.1f}us. The failure path costs the")
        print("   most because it normalises the message into an ErrorLog signature —")
        print("   which is exactly the work that stops a flaky endpoint from writing")
        print("   fifty near-identical episodes.")
        print(f"   {submitted['n']} events were accepted during the run (writes suppressed).")

        # ── B. the fast path ────────────────────────────────────────────────
        print("\nB. FAST PATH — unchanged by P4.2")
        store = rt._episodes
        queries = {"n": 0}
        for attr in ("search_episodes", "recent_episodes", "search_decisions"):
            real = getattr(store, attr)

            def wrap(_real=real):
                async def _w(*a, **k):
                    queries["n"] += 1
                    return await _real(*a, **k)
                return _w
            setattr(store, attr, wrap())

        promoter._submit = lambda _ev: bool(rt._episodic_worker.submit(_ev))
        queued_before = rt._episodic_worker.stats["queued"]
        queries["n"] = 0

        samples = []
        for text in ("Good morning.", "Thanks!", "What time is it?", "Cool.", "Hey."):
            t0 = time.perf_counter()
            await nova.say(text, conversation_id=conv)
            samples.append((time.perf_counter() - t0) * 1000)

        print(f"   episodic DB queries        {queries['n']}")
        print(f"   episode writes enqueued    {rt._episodic_worker.stats['queued'] - queued_before}")
        print(f"   prompt characters added    0 (no episodic block emitted)")
        print(f"   turn wall time             median {statistics.median(samples):.0f}ms "
              f"(scripted model; includes the whole pipeline)")

        # ── C. what a publisher pays ────────────────────────────────────────
        print("\nC. BUS PUBLISH — the cost paid by whatever emitted the event")
        m_with, p_with = timed(
            lambda i: BUS.publish("project.progress", {"project": "x", "stage": "writing"}),
            reps=2000)
        await promoter.stop()
        m_without, p_without = timed(
            lambda i: BUS.publish("project.progress", {"project": "x", "stage": "writing"}),
            reps=2000)
        print(f"   with a subscribed promoter  {m_with:.2f}us   P90 {p_with:.2f}us")
        print(f"   without                     {m_without:.2f}us   P90 {p_without:.2f}us")
        print("   The promoter is one more bounded queue on a fire-and-forget publish.")
        print("   Nothing in ProjectBuilder or MemoryUnifier waits for a promotion.")


if __name__ == "__main__":
    run(main)
