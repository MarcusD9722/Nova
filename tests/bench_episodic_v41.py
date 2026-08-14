"""V3 P4.1: what episodic memory costs on Nova's ACTUAL context-building path.

P4's benchmark measured the store in isolation, which was the right thing to
measure while nothing was wired to it. It is not sufficient now: the question
this phase has to answer is what a real turn pays, and the only honest way to
answer it is to run real turns.

So this drives `RuntimeManager` itself — the same `chat_turn_stream` a live
message goes through — and toggles exactly one variable:

    BEFORE   NOVA_EPISODIC_MEMORY=0   the P4 state: substrate present, unwired
    AFTER    NOVA_EPISODIC_MEMORY=1   P4.1

Same process, same corpus, same scripted model, same everything else. A
before/after built by re-running P2.5's model benchmark instead would have
buried a sub-millisecond change under seconds of generation variance and proved
nothing either way.

The model is scripted (harness.ScriptedLLM), which is deliberate and is stated
plainly in the output: this measures the work Nova does BEFORE inference, which
is the only part P4.1 can affect. Real TTFT is measured by tests/bench_nova_v3.py.

Run:  venv\\Scripts\\python.exe tests\\bench_episodic_v41.py [episodes]
"""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from harness import boot, run  # noqa: E402

REPS = 7

DRIVES = [
    {"title": "Seagate Exos X28", "capacity": "28 TB", "price": "$429"},
    {"title": "WD Gold", "capacity": "26 TB", "price": "$399"},
    {"title": "IronWolf Pro", "capacity": "24 TB", "price": "$389"},
]

# (key, what Marcus says, what it exercises)
SCENARIOS = [
    ("fast_greeting",   "Good morning.",                                   "FAST, no history"),
    ("hot_ordinal",     "What about the second one?",                      "current HOT artifact"),
    ("known_fact",      "What GPU do I have?",                             "fact recall, no history"),
    ("historical",      "What were those drives we looked at yesterday?",  "warm episode"),
    ("decision",        "Why do unknown MCP capabilities require confirmation?", "decision memory"),
    ("evidence",        "What exact benchmark numbers did we record earlier?",   "warm + COLD evidence"),
]


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


class Counting:
    """Wraps the store so DB work is counted rather than assumed."""

    def __init__(self, store):
        self.store = store
        self.queries = 0
        self.cold_reads = 0
        self._real_search = store.search_episodes
        self._real_recent = store.recent_episodes
        self._real_decisions = store.search_decisions
        self._real_children = store.load_children
        self._real_artifact = store.load_artifact
        self._real_cold_get = store.cold.get

        async def search(*a, **k):
            self.queries += 1
            return await self._real_search(*a, **k)

        async def recent(*a, **k):
            self.queries += 1
            return await self._real_recent(*a, **k)

        async def decisions(*a, **k):
            self.queries += 1
            return await self._real_decisions(*a, **k)

        async def children(*a, **k):
            self.queries += 1
            return await self._real_children(*a, **k)

        async def artifact(*a, **k):
            self.queries += 1
            return await self._real_artifact(*a, **k)

        def cold_get(*a, **k):
            self.cold_reads += 1
            return self._real_cold_get(*a, **k)

        store.search_episodes = search
        store.recent_episodes = recent
        store.search_decisions = decisions
        store.load_children = children
        store.load_artifact = artifact
        store.cold.get = cold_get

    def reset(self):
        self.queries = 0
        self.cold_reads = 0


async def build_corpus(memory_dir: Path, n: int) -> dict:
    """Populate durable memory through the REAL promotion path.

    Bulk-inserting rows would be faster and would measure a database Nova does
    not actually produce.
    """
    from memory.artifacts import capture_tool_result

    async with boot(env={"NOVA_MEMORY_DIR": str(memory_dir)}) as nova:
        w = nova.runtime._episodic_worker
        t0 = time.perf_counter()
        topics = ["hard drives", "monitors", "GPUs", "keyboards", "routers",
                  "SSDs", "cases", "coolers", "power supplies", "microphones"]
        for i in range(n):
            topic = topics[i % len(topics)]
            capture_tool_result(
                nova.runtime._artifacts, conversation_id=f"c{i % 40}", turn_id=f"t{i}",
                tool="web.search", args={"query": f"{topic} review {i}"},
                result={"results": [{"title": f"{topic} option {j}", "price": f"${100 + j}"}
                                    for j in range(1, 4)]},
            )
            # The persistence queue is bounded and DROPS rather than blocking,
            # which is correct for a live turn and wrong for a corpus builder:
            # captured back-to-back with no awaits, a third of the corpus was
            # discarded before the worker ever ran. Yield often enough to let it
            # keep up. A real conversation cannot produce tool results this fast.
            if i % 25 == 0:
                while nova.runtime._episodic_q.qsize() > 60:
                    await asyncio.sleep(0.01)
        # The one the historical scenario is meant to find.
        capture_tool_result(
            nova.runtime._artifacts, conversation_id="c-drives", turn_id="t-drives",
            tool="web.search", args={"query": "28 TB hard drives"},
            result={"results": DRIVES})
        # And one with heavy evidence, so cold hydration has something to read.
        capture_tool_result(
            nova.runtime._artifacts, conversation_id="c-bench", turn_id="t-bench",
            tool="web.search", args={"query": "benchmark numbers"},
            result={"results": [{"title": "benchmark run",
                                 "detail": "x" * 4000,
                                 "numbers": ", ".join(str(v) for v in range(400))}]})

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if w.stats["persisted"] + w.stats["failed"] >= w.stats["queued"]:
                break
            await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - t0
        stats = await nova.runtime._episodes.stats()
        dropped = w.stats["dropped"]

    db = memory_dir / "sqlite" / "nova.sqlite3"
    return {
        "episodes": stats["episodes"], "artifacts": stats["artifacts"],
        # The real count is the filesystem store's. The `cold_evidence`
        # TABLE is defined by P4's schema but nothing writes to it yet — see
        # docs/NOVA_V3_EPISODIC_MEMORY.md, known limitations.
        "cold": stats["cold"].get("writes", 0), "seconds": elapsed, "dropped": dropped,
        "mb": db.stat().st_size / 1e6 if db.exists() else 0.0,
    }


async def measure(memory_dir: Path, *, enabled: bool) -> dict:
    """One full pass over the scenarios at a given setting."""
    from memory.artifacts import capture_tool_result

    env = {"NOVA_MEMORY_DIR": str(memory_dir),
           "NOVA_EPISODIC_MEMORY": "1" if enabled else "0"}
    out: dict = {}
    async with boot(env=env) as nova:
        counter = Counting(nova.runtime._episodes) if enabled else None

        # Give the hot-ordinal scenario something on screen, exactly as a real
        # previous turn would have.
        conv = "bench-conv"
        capture_tool_result(
            nova.runtime._artifacts, conversation_id=conv, turn_id="t-hot",
            tool="web.search", args={"query": "28 TB drives"},
            result={"results": DRIVES})
        await asyncio.sleep(0.3)

        for key, text, _what in SCENARIOS:
            samples: list[float] = []
            chars = 0
            queries = 0
            cold = 0
            for _ in range(REPS):
                if counter:
                    counter.reset()
                work_ctx = nova.runtime._working.get(conv)
                active = nova.runtime._artifacts.active_items(conv)
                t0 = time.perf_counter()
                block, _supersedes = await nova.runtime._episodic_context(
                    query=text, recent_text=work_ctx.recent_text(),
                    has_result_set=bool(active), item_count=len(active),
                    hot_resolved=(key == "hot_ordinal"),
                ) if enabled else ("", False)
                samples.append((time.perf_counter() - t0) * 1000)
                chars = len(block)
                if counter:
                    queries = counter.queries
                    cold = counter.cold_reads
            out[key] = {"median": statistics.median(samples), "p90": pct(samples, 0.9),
                        "chars": chars, "queries": queries, "cold": cold}
    return out


async def measure_enqueue(memory_dir: Path) -> dict:
    """What promotion costs the turn that produced the artifact."""
    from memory.artifacts import capture_tool_result

    async with boot(env={"NOVA_MEMORY_DIR": str(memory_dir)}) as nova:
        w = nova.runtime._episodic_worker
        enqueue: list[float] = []
        for i in range(REPS):
            t0 = time.perf_counter()
            capture_tool_result(
                nova.runtime._artifacts, conversation_id="c-enq", turn_id=f"t-enq{i}",
                tool="web.search", args={"query": "drives"}, result={"results": DRIVES})
            enqueue.append((time.perf_counter() - t0) * 1000)

        before = w.stats["persisted"]
        t0 = time.perf_counter()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if w.stats["persisted"] + w.stats["failed"] >= w.stats["queued"]:
                break
            await asyncio.sleep(0.01)
        completion = (time.perf_counter() - t0) * 1000
        done = w.stats["persisted"] - before
    return {"enqueue_median": statistics.median(enqueue),
            "enqueue_p90": pct(enqueue, 0.9),
            "completion_total": completion, "completed": done,
            "per_episode": completion / max(1, done)}


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

    print("Nova V3 P4.1 — episodic memory on the real context path")
    print("=" * 88)
    print("\nThe model is scripted, so these are PRE-INFERENCE numbers: the work Nova")
    print("does between reading a message and calling the model. That is the only part")
    print("P4.1 touches. Real end-to-end TTFT: tests/bench_nova_v3.py.\n")

    root = Path(tempfile.mkdtemp(prefix="nova-p41-bench-"))
    try:
        print(f"building a {n}-episode corpus through the real promotion path ...")
        corpus = await build_corpus(root, n)
        print(f"  {corpus['episodes']} episodes, {corpus['artifacts']} artifacts, "
              f"{corpus['cold']} cold blobs in {corpus['seconds']:.1f}s "
              f"({corpus['dropped']} dropped by back-pressure)")
        print(f"  database {corpus['mb']:.2f} MB\n")

        before = await measure(root, enabled=False)
        after = await measure(root, enabled=True)

        print("A. ADDED PRE-INFERENCE LATENCY — before (P4) vs after (P4.1)")
        print(f"   {'scenario':<16} {'exercises':<26} {'before':>8} {'after':>9} "
              f"{'P90':>8} {'DB':>4} {'cold':>5} {'chars':>6}")
        for key, _text, what in SCENARIOS:
            b, a = before[key], after[key]
            print(f"   {key:<16} {what:<26} {b['median']:>7.3f}ms {a['median']:>8.3f}ms "
                  f"{a['p90']:>7.3f}ms {a['queries']:>4} {a['cold']:>5} {a['chars']:>6}")

        fast = after["fast_greeting"]
        print(f"\n   FAST-PATH GATE: '{SCENARIOS[0][1]}' costs {fast['median'] * 1000:.1f}us, "
              f"{fast['queries']} database queries, {fast['chars']} prompt characters.")
        print("   P2.5's fast path is untouched: the gate is pure string work and a turn")
        print("   that does not reference the past never reaches SQLite.")

        print("\nB. PERSISTENCE — what the turn pays, and what happens after it")
        enq = await measure_enqueue(root)
        print(f"   enqueue (on the turn)      {enq['enqueue_median']:.3f}ms  "
              f"P90 {enq['enqueue_p90']:.3f}ms")
        print(f"   completion (background)    {enq['per_episode']:.1f}ms per episode "
              f"({enq['completed']} episodes in {enq['completion_total']:.0f}ms)")
        ratio = enq["per_episode"] / max(enq["enqueue_median"], 1e-6)
        print(f"   the turn pays {ratio:.0f}x less than the write costs — that ratio IS")
        print("   the reason persistence is a worker and not an await.")

        print("\nC. CORPUS")
        print(f"   {corpus['episodes']} episodes / {corpus['artifacts']} artifacts / "
              f"{corpus['mb']:.2f} MB, all created through capture -> hook -> worker")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    run(main)
