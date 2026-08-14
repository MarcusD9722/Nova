"""V3 P4: what does episodic memory cost per turn?

P2.5 got a simple turn to ~130 ms. The question P4 has to answer is whether
"Good morning" now pays for a historical search merely because episodic memory
exists. It should not — the gate should refuse before touching the database.

Measured against a corpus large enough that a linear scan would be visible.

Run:  venv\\Scripts\\python.exe tests\\bench_episodic_v4.py [corpus_size]
"""

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.artifacts import ArtifactStore
from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.episodes import EP_TOOL_RESULT, Episode, EpisodicStore
from memory.episodic_recall import needs_episodic_memory, retrieve

SCENARIOS = [
    ("greeting", "Good morning.", "no memory needed"),
    ("fact", "What snowboard boots do I own?", "fact recall, not episodic"),
    ("on_screen", "What about the second one?", "current artifact reference"),
    ("historical", "What was that drive we were looking at yesterday?", "episodic recall"),
    ("project", "Where did we leave off with my Jellyfin project?", "project history"),
    ("buried", "What did we decide about the WD Gold drive?", "needle in haystack"),
]


async def timed(fn, reps=10):
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - t0) * 1000)
    s = sorted(times)
    return statistics.median(s), s[min(len(s) - 1, round(0.9 * (len(s) - 1)))]


async def main():
    corpus = int(sys.argv[1]) if len(sys.argv) > 1 else 2000

    print("Nova V3 P4 — episodic memory overhead")
    print("=" * 88)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "memory.db"
        backend = SQLiteMemoryBackend(db_path)
        await backend.initialize()
        store = EpisodicStore(db_path)

        print(f"\nbuilding a {corpus}-episode corpus ...")
        t0 = time.perf_counter()
        for i in range(corpus):
            await store.record_episode(Episode(
                id=f"ep-{i}", kind=EP_TOOL_RESULT,
                summary=f"Checked the weather in city {i} and reported conditions.",
                entities=[f"city{i}"], source_tool="weather.current",
                conversation_id=f"conv-{i % 50}"))
        # One genuinely relevant episode, buried.
        await store.record_episode(Episode(
            id="needle", kind=EP_TOOL_RESULT,
            summary="Compared three 28 TB NAS drives; Marcus preferred the WD Gold.",
            entities=["WD Gold", "Seagate Exos", "drives"], source_tool="web.search",
            importance=0.8, project="media-server"))
        build_s = time.perf_counter() - t0
        print(f"  {corpus + 1} episodes in {build_s:.1f}s "
              f"({build_s / (corpus + 1) * 1000:.2f}ms each)")

        stats = await store.stats()
        db_mb = db_path.stat().st_size / 1e6
        print(f"  database {db_mb:.2f} MB   rows {stats['episodes']}")

        # ── the gate ─────────────────────────────────────────────────────────
        print("\nA. GATE — does a turn even look at history?")
        print(f"   {'scenario':<12} {'searches?':>10} {'gate median':>13} {'P90':>9}")
        for key, prompt, _what in SCENARIOS:
            kw = dict(has_result_set=True, item_count=3) if key == "on_screen" else {}

            async def run():
                needs_episodic_memory(prompt, **kw)

            med, p90 = await timed(run, reps=200)
            d = needs_episodic_memory(prompt, **kw)
            print(f"   {key:<12} {str(d.search):>10} {med * 1000:>11.1f}us "
                  f"{p90 * 1000:>7.1f}us")

        print("\n   The gate is pure string work — no database, no model. A turn that")
        print("   does not reference the past never reaches the query below.")

        # ── retrieval ────────────────────────────────────────────────────────
        print("\nB. RETRIEVAL — only for turns that passed the gate")
        print(f"   {'scenario':<12} {'median':>10} {'P90':>10} {'episodes':>10} {'chars':>8}")
        for key, prompt, _what in SCENARIOS:
            if not needs_episodic_memory(prompt).search:
                continue
            holder = {}

            async def run():
                holder["r"] = await retrieve(store, prompt, limit=3)

            med, p90 = await timed(run, reps=10)
            r = holder["r"]
            print(f"   {key:<12} {med:>9.1f}ms {p90:>9.1f}ms {len(r.episodes):>10} "
                  f"{r.chars:>8}")

        # ── cold hydration ───────────────────────────────────────────────────
        print("\nC. COLD HYDRATION")
        from memory.artifacts import Artifact

        big = Artifact(artifact_id="big", conversation_id="c", turn_id="t",
                       artifact_type="tool_result", summary="large evidence",
                       payload={"text": "X" * 100_000})
        await store.persist_artifact(big)

        async def warm_only():
            await store.load_artifact("big")

        async def with_cold():
            await store.load_artifact("big", hydrate=True)

        wm, _ = await timed(warm_only, reps=20)
        cm, _ = await timed(with_cold, reps=20)
        print(f"   warm row only        {wm:>7.2f}ms")
        print(f"   with cold hydration  {cm:>7.2f}ms   (+{cm - wm:.2f}ms)")
        print("   Hydration is opt-in per call; retrieval does not do it by default.")

        # ── total added pre-inference latency ────────────────────────────────
        print("\nD. TOTAL ADDED PRE-INFERENCE LATENCY")
        for key, prompt, _what in SCENARIOS:
            kw = dict(has_result_set=True, item_count=3) if key == "on_screen" else {}
            t0 = time.perf_counter()
            d = needs_episodic_memory(prompt, **kw)
            chars = 0
            if d.search:
                r = await retrieve(store, prompt, limit=3)
                chars = r.chars
            total = (time.perf_counter() - t0) * 1000
            print(f"   {key:<12} {total:>8.2f}ms   +{chars:>5} prompt chars")

        print("\n   A greeting adds microseconds and zero prompt characters.")
        print(f"   Corpus for these numbers: {corpus + 1} episodes, {db_mb:.2f} MB.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
