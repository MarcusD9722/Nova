"""Phase 4 / #8: Knowledge Graph 2.0 — path-finding, subgraph, auto-discovery,
universal typed links."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.graph import Edge
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        g = mem.graph

        # A small family graph through the hub 'user'.
        await g.upsert_edge(Edge("person", "Liam", "child_of", "person", "user"))
        await g.upsert_edge(Edge("person", "Mateo", "child_of", "person", "user"))
        await g.upsert_edge(Edge("person", "Leslie", "spouse_of", "person", "user"))

        # ── path_between: Liam -> Leslie through user ──
        path = await mem.graph_path("Liam", "Leslie")
        keys = [h["key"] for h in path]
        check(keys == ["liam", "user", "leslie"], f"shortest path found through the hub (got {keys})")
        check(len(path) - 1 == 2, "path reports 2 hops")

        # ── disconnected nodes -> no path ──
        await g.upsert_edge(Edge("topic", "island", "note", "topic", "lonely"))
        check(await mem.graph_path("Liam", "island") == [], "unconnected nodes return no path")

        # ── subgraph around user ──
        sub = await mem.graph_subgraph("user", depth=1)
        node_keys = {n["key"] for n in sub["nodes"]}
        check({"user", "liam", "mateo", "leslie"} <= node_keys, f"subgraph gathers the 1-hop neighborhood (got {node_keys})")
        check(len(sub["edges"]) >= 3, "subgraph returns the connecting edges")

        # ── automatic association discovery (shared-neighbor co-occurrence) ──
        # alpha & beta both connect to x and y but not to each other.
        for node in ("alpha", "beta"):
            await g.upsert_edge(Edge("topic", node, "relates", "topic", "x"))
            await g.upsert_edge(Edge("topic", node, "relates", "topic", "y"))
        result = await mem.discover_graph_associations(min_shared=2)
        check(result["discovered"] >= 1, f"discovery infers at least one association (got {result})")
        alpha_edges = await g.edges_for("alpha")
        check(any(e["predicate"] == "associated_with" and "beta" in (e["src_key"], e["dst_key"]) for e in alpha_edges),
              "alpha auto-linked to beta via shared neighbors")

        # discovery never invents links off the 'user' hub (would be noise)
        before = len(await g.edges_for("liam"))
        await mem.discover_graph_associations(min_shared=2)
        after = len(await g.edges_for("liam"))
        check(before == after, "no spurious associations created through the 'user' hub")

        # ── universal typed link (any node kind) ──
        ok = await mem.link("movie", "Inception", "explores", "concept", "dreams")
        check(ok, "link() records an arbitrary typed edge")
        inception = await g.edges_for("inception")
        check(any(e["predicate"] == "explores" and "dreams" in (e["src_key"], e["dst_key"]) for e in inception),
              "movie->concept edge stored and retrievable")
        check(await mem.link("x", "", "rel", "y", "z") is False, "degenerate link (empty endpoint) rejected")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
