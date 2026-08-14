"""V3 P3: what does MCP cost a turn that does not use it?

The scaling claim is that Nova can carry hundreds or thousands of MCP
capabilities without paying for them on every turn. That claim is only worth
making if it is measured, and the number that matters most is the one for
"Good morning" — a turn that touches no MCP tool at all should cost nothing.

No model or server needed: this measures registry and selection cost, which is
where the per-turn expense would actually land.

Run:  venv\\Scripts\\python.exe tests\\bench_mcp_v3.py
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mcp.registry import CapabilityRegistry
from core.tools.selector import ToolEmbeddingCache, ToolSelector

NATIVE = {
    "weather.current": "Current weather conditions for a city or location.",
    "weather.forecast": "Weather forecast for the coming days at a location.",
    "memory.remember": "Save a fact, preference or detail to long-term memory.",
    "memory.recall": "Look up something previously remembered about the user.",
    "maps.directions": "Driving directions and travel time between two places.",
    "web.search": "Search the web for current information and news.",
    "reminder.create": "Create a reminder or timer for a future time.",
    "time.now": "Get the current date and time.",
}


def synth_tools(n: int):
    domains = ["repository", "calendar", "light", "database", "document", "message",
               "sensor", "printer", "playlist", "invoice"]
    return [
        {"name": f"tool_{i}",
         "description": f"Perform action {i} on a {domains[i % len(domains)]} "
                        f"with several options and filters.",
         "inputSchema": {"type": "object", "properties": {
             "target": {"type": "string", "description": "The thing to act on." + "x" * 60},
             "options": {"type": "object", "description": "Extra options." + "y" * 60}}},
         "annotations": {"readOnlyHint": i % 3 == 0}}
        for i in range(n)
    ]


def timed(fn, reps=20):
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), sorted(times)[min(len(times) - 1,
                                                       round(0.9 * (len(times) - 1)))]


def main():
    print("Nova V3 P3 — MCP overhead")
    print("=" * 84)
    # Lexical selector: deterministic, and isolates registry/selection cost from
    # embedding-model variability.
    sel = ToolSelector(cache=ToolEmbeddingCache(enabled=False))

    print("\nA. Does a turn that uses no MCP pay for MCP?")
    print(f"   {'registry size':<18} {'select median':>15} {'select P90':>12} "
          f"{'tools shown':>12}")
    for n in (0, 10, 100, 1000):
        reg = CapabilityRegistry()
        if n:
            reg.replace_server("bench", synth_tools(n))
        merged = {**NATIVE, **reg.selector_descriptions()}
        result_holder = {}

        def run():
            result_holder["r"] = sel.select("Good morning.", merged)

        med, p90 = timed(run)
        shown = len(result_holder["r"].tools)
        print(f"   {n:<18} {med:>13.2f}ms {p90:>10.2f}ms {shown:>12}")

    print("\n   'Good morning' is a deterministic no-tool match, so it short-circuits")
    print("   before any ranking — registry size should barely move it.")

    print("\nB. Registry operations")
    for n in (100, 1000):
        reg = CapabilityRegistry()
        tools = synth_tools(n)
        med, _ = timed(lambda: reg.replace_server("bench", tools), reps=5)
        print(f"   discovery+normalise {n:>5} tools : {med:>8.1f}ms")

        med, _ = timed(lambda: reg.selector_descriptions())
        print(f"   selector metadata   {n:>5} tools : {med:>8.2f}ms")

        ids = list(reg.selector_descriptions())[:8]
        med, _ = timed(lambda: reg.hydrate(ids))
        print(f"   hydrate 8 schemas   {n:>5} tools : {med:>8.3f}ms")

        med, _ = timed(lambda: reg.search("repository action filters"))
        print(f"   capability.search   {n:>5} tools : {med:>8.2f}ms")

    print("\nC. Prompt cost: metadata vs full schemas")
    print(f"   {'tools':<8} {'metadata chars':>16} {'schema chars':>15} {'saved':>8}")
    for n in (100, 1000):
        reg = CapabilityRegistry()
        reg.replace_server("bench", synth_tools(n))
        meta = sum(len(v) for v in reg.selector_descriptions().values())
        schema = sum(len(str(c.schema)) + len(c.description) for c in reg.all())
        print(f"   {n:<8} {meta:>16} {schema:>15} {1 - meta / schema:>7.0%}")

    print("\n   Selection sees the metadata column. The schema column is hydrated")
    print("   only for the handful of capabilities that survive selection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
