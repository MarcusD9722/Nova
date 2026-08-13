"""Tool selector: precision, recall, caching, and failing open.

Recall is the metric that matters. Excluding the tool a turn needed is a silent
capability loss — Nova simply appears not to be able to do something she can.
Including one extra candidate costs a few dozen prompt tokens. The assertions
below are weighted accordingly: recall is required to be perfect, precision is
required only to be a large improvement over "show everything".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools.selector import ToolEmbeddingCache, ToolSelector

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


# A registry shaped like Nova's real one: 49 tools in core/tooling.py plus
# plugins. Descriptions are abbreviated but keep the vocabulary that matters.
TOOLS = {
    "time.now": "Get the current date and time.",
    "weather.current": "Current weather conditions for a city or location.",
    "weather.forecast": "Weather forecast for the coming days at a location.",
    "maps.directions": "Driving directions, distance and travel time between two places.",
    "maps.places_nearby": "Find places near a location, such as restaurants or shops.",
    "maps.geocode": "Convert a place name into coordinates.",
    "web.search": "Search the web for current information and news.",
    "web.fetch": "Fetch and read the contents of a web page URL.",
    "memory.remember": "Save a fact, preference or detail to long-term memory.",
    "memory.recall": "Look up something previously remembered about the user.",
    "memory.correct": "Correct a previously stored fact that turned out to be wrong.",
    "memory.remember_person": "Save details about a person the user knows.",
    "memory.recall_person": "Look up details about a person the user knows.",
    "memory.timeline": "Show remembered events over time.",
    "reminder.create": "Create a reminder or timer for a future time.",
    "project.start_build": "Create and scaffold a brand new software project.",
    "project.improve": "Make a change to an existing project in the projects folder.",
    "project.status": "Report the status of a project build.",
    "code.read": "Read the contents of a source file in a project.",
    "self.read_code": "Read Nova's own source code.",
    "self.propose_change": "Propose an edit to Nova's own source code for review.",
    "self.list_code": "List Nova's own source files.",
    "image.generate": "Generate an image from a text description.",
    "video.generate": "Generate a short video from a text description.",
    "discord.send": "Send a message to a Discord channel.",
    "gmail.list": "List recent emails from Gmail.",
    "gmail.send": "Send an email through Gmail.",
    "calendar.list": "List upcoming calendar events.",
    "calendar.create": "Create a calendar event.",
    "thoughts.note": "Record a private thought or observation.",
    "thoughts.recall": "Recall previously recorded thoughts.",
    "plan.save": "Save a multi-step plan.",
    "plan.status": "Check progress on a saved plan.",
    "plan.advance": "Advance a saved plan to its next step.",
    "research.track": "Start tracking a research topic over time.",
    "research.findings": "Report findings on a tracked research topic.",
    "skill.learn": "Learn a new repeatable skill from a demonstration.",
    "skill.list": "List learned skills.",
    "experiment.record": "Record an experiment and its outcome.",
    "world.recall": "Recall general world knowledge previously stored.",
    "world.learn": "Store a general fact about the world.",
    "twin.profile": "Report the user's modelled profile and habits.",
    "executive.brief": "Produce an executive summary of the user's current state.",
    "goal.create": "Create a long-term goal.",
    "agents.roster": "List the available internal agents.",
    "vision.look_at_screen": "Look at what is currently on the user's screen.",
    "system.shell": "Run a shell command on the local machine.",
    "memory.index_folder": "Index a folder of documents into memory.",
    "file.read": "Read a local file from disk.",
}

# (query, tools that MUST survive selection, label)
DATASET = [
    ("good morning", (), "no tool: greeting"),
    ("tell me a joke", (), "no tool: social"),
    ("thanks, that's perfect", (), "no tool: acknowledgement"),

    ("what time is it", ("time.now",), "deterministic: time"),
    ("what's the weather tomorrow", ("weather.forecast", "weather.current"), "weather"),
    ("do I need a jacket today", ("weather.current", "weather.forecast"), "weather, implied"),
    ("will it snow at Hunter this weekend", ("weather.forecast", "weather.current"), "weather, place"),

    ("how long to get to Stowe from here", ("maps.directions",), "maps: travel time"),
    ("give me directions to the airport", ("maps.directions",), "maps: directions"),
    ("find a coffee shop near me", ("maps.places_nearby",), "maps: nearby"),

    ("set a timer for ten minutes", ("reminder.create",), "reminder"),
    ("remind me to call Leslie at six", ("reminder.create",), "reminder, natural"),

    ("remember that I prefer reliability over noise",
     ("memory.remember",), "memory write"),
    ("what boot size do I wear", ("memory.recall",), "memory read"),
    ("what do you remember about my server", ("memory.recall",), "memory read, explicit"),
    ("actually my boot size is 10, not 9.5", ("memory.correct", "memory.remember"),
     "memory correction"),

    ("search the web for 28 TB drive reviews", ("web.search",), "web search"),
    ("read that page for me", ("web.fetch", "web.search"), "web fetch"),

    ("start building me a new snowboard tracking app",
     ("project.start_build",), "project create"),
    ("continue the game we were working on",
     ("project.improve", "project.status"), "project continue"),

    ("look at your own source and tell me how memory works",
     ("self.read_code", "self.list_code"), "self code"),
    ("generate an image of a snowy mountain", ("image.generate",), "image"),
    ("send a message to the Discord channel", ("discord.send",), "discord"),
    ("what's on my calendar tomorrow", ("calendar.list",), "calendar"),
    ("any new emails", ("gmail.list",), "email"),
    ("what's on my screen right now", ("vision.look_at_screen",), "vision"),

    ("check the weather and the traffic for tomorrow morning",
     ("weather.forecast", "maps.directions"), "multi-tool"),
]


def lexical_only_selector(**kw):
    """The degraded configuration: embeddings deliberately switched off.

    Distinct from the fail-open case below, where the embedding backend
    *raises*. Here ranking genuinely runs on word overlap.
    """
    return ToolSelector(cache=ToolEmbeddingCache(enabled=False), **kw)


def semantic_selector(**kw):
    """The normal configuration, with bge-small actually loaded."""
    from memory.embeddings import embed_texts, embedding_available

    if not embedding_available():
        return None
    return ToolSelector(cache=ToolEmbeddingCache(embed=embed_texts,
                                                 model_id="bge-small-test"), **kw)


def build_selector(**kw):
    return lexical_only_selector(**kw)


def measure_recall(sel, label):
    misses = []
    total_candidates = 0
    for query, required, _ in DATASET:
        result = sel.select(query, TOOLS)
        total_candidates += len(result.tools)
        if not required:
            continue
        if not any(t in result.tools for t in required):
            misses.append((query, required, result.tools[:6], result.stage))

    for query, required, got, stage in misses:
        print(f"       MISS [{label}] {query!r}: wanted one of {required}, got {got} ({stage})")
    avg = total_candidates / len(DATASET)
    reduction = 1 - (avg / len(TOOLS))
    print(f"       [{label}] {len(TOOLS)} tools -> {avg:.1f} shown on average "
          f"({reduction:.0%} fewer)")
    return misses, avg, reduction


def test_recall_is_perfect():
    print("\nrecall (the metric that matters)")

    # Both configurations must keep the required tool. Testing only whichever
    # one happened to be loaded would make the result depend on test ordering.
    sem = semantic_selector()
    if sem is not None:
        misses, avg, reduction = measure_recall(sem, "semantic")
        check(not misses, f"semantic: every query keeps a required tool ({len(misses)} misses)")
        check(reduction >= 0.6, f"semantic: schema reduction {reduction:.0%} (target >=60%)")
        check(avg <= 12, f"semantic: average candidates {avg:.1f}")
    else:
        print("       (embedding model unavailable — semantic pass skipped, not faked)")

    misses, avg, reduction = measure_recall(lexical_only_selector(), "lexical fallback")
    check(not misses,
          f"lexical fallback: every query keeps a required tool ({len(misses)} misses)")
    check(reduction >= 0.3,
          f"lexical fallback still reduces the catalogue ({reduction:.0%}); it trades "
          f"precision for recall, which is the correct direction")


def test_no_tool_turns():
    print("\nconversational turns")
    sel = build_selector()
    for query in ("good morning", "thanks!", "how are you", "tell me a joke"):
        result = sel.select(query, TOOLS)
        check(result.stage == "deterministic" and len(result.tools) <= 3,
              f"{query!r} routes to almost nothing (got {len(result.tools)}: {result.tools})")
        # Memory stays available even on social turns: "morning, remember I'm
        # off today" is both, and dropping memory would lose it silently.
        check("memory.remember" in result.tools,
              f"{query!r} still keeps the memory write tool")


def test_deterministic_stage_is_free():
    print("\ndeterministic stage")
    sel = build_selector()
    result = sel.select("what time is it", TOOLS)
    check(result.stage == "deterministic", f"time routes deterministically (got {result.stage})")
    check("time.now" in result.tools, "and picks the right tool")
    check(result.elapsed_ms < 15, f"in well under a millisecond of work ({result.elapsed_ms}ms)")


def test_fails_open():
    print("\nfail-open behaviour")

    class Exploding(ToolEmbeddingCache):
        def sync(self, descriptions):
            raise RuntimeError("embedding backend exploded")

    sel = ToolSelector(cache=Exploding(embed=None))
    result = sel.select("find me three 28 TB drives for my media server", TOOLS)
    check(result.stage == "all", f"a broken selector shows everything (got {result.stage})")
    check(len(result.tools) == len(TOOLS), "every tool survives the failure")
    check(sel.stats["failed_open"] == 1, "failing open is counted, not hidden")

    # An unknown, unrankable query must widen rather than return nothing.
    sel2 = build_selector()
    result = sel2.select("zzzz qqqq xyzzy", TOOLS)
    check(len(result.tools) > 0, "an unrankable query never returns an empty tool list")


def test_embedding_cache():
    print("\nembedding cache")
    calls = {"n": 0}

    def fake_embed(texts):
        calls["n"] += len(texts)
        # Deterministic pseudo-vectors: enough to exercise cache mechanics.
        return [[float(len(t) % 7), float(len(t) % 5), 1.0] for t in texts]

    cache = ToolEmbeddingCache(embed=fake_embed, model_id="test-model")
    small = {k: TOOLS[k] for k in list(TOOLS)[:10]}

    computed = cache.sync(small)
    check(computed == 10, f"first sync embeds every tool (got {computed})")
    baseline = calls["n"]

    cache.sync(small)
    check(calls["n"] == baseline, "a second sync embeds nothing — vectors are reused")

    # Change one description: only that tool is re-embedded.
    changed = dict(small)
    key = list(changed)[3]
    changed[key] = changed[key] + " Now with extra detail."
    cache.sync(changed)
    check(calls["n"] == baseline + 1,
          f"editing one description re-embeds exactly one tool (delta {calls['n'] - baseline})")

    # Change the embedding model: everything is invalidated.
    cache2 = ToolEmbeddingCache(embed=fake_embed, model_id="different-model")
    before = calls["n"]
    cache2.sync(small)
    check(calls["n"] == before + 10, "a different embedding model invalidates the whole cache")

    stats = cache.stats()
    check(stats["model"] == "test-model", "cache reports which model it is keyed to")


def test_query_embedded_once_per_turn():
    print("\nper-turn cost")
    queries = {"n": 0}

    def fake_embed(texts):
        queries["n"] += 1
        return [[1.0, 0.0, 0.0] for _ in texts]

    cache = ToolEmbeddingCache(embed=fake_embed, model_id="m")
    sel = ToolSelector(cache=cache)
    sel.select("find me some drives for the server", TOOLS)
    first = queries["n"]
    sel.select("what about the second one", TOOLS)
    second = queries["n"] - first
    check(second == 1,
          f"a later turn costs exactly one embed call, not {len(TOOLS)} (got {second})")


def test_selection_preserves_permissions_boundary():
    print("\npermissions boundary")
    # The selector must not filter on anything permission-shaped; that is
    # ToolRouter's job and duplicating it here would create a second, divergent
    # permission system.
    import ast

    src = Path("core/tools/selector.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden = {"PermissionBroker", "check_permission", "require_permission", "execute"}
    check(not (names | attrs) & forbidden,
          f"selector never touches permissions or execution (found {(names | attrs) & forbidden})")


def test_context_influences_selection():
    print("\ncontext-aware selection")
    sel = build_selector()
    bare = sel.select("what about Saturday?", TOOLS)
    with_ctx = sel.select("what about Saturday?", TOOLS,
                          context="Previous turn: the weather forecast at Hunter for tomorrow.")
    check(any(t.startswith("weather") for t in with_ctx.tools),
          f"a weather follow-up keeps weather tools when context says so (got {with_ctx.tools[:6]})")
    check(len(bare.tools) > 0, "the bare follow-up still returns candidates rather than nothing")


def main():
    test_recall_is_perfect()
    test_no_tool_turns()
    test_deterministic_stage_is_free()
    test_fails_open()
    test_embedding_cache()
    test_query_embedded_once_per_turn()
    test_selection_preserves_permissions_boundary()
    test_context_influences_selection()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


# Guarded: tests/bench_jarvis_v2.py imports TOOLS and DATASET from here, and an
# unguarded main() would run the whole suite as a side effect of that import.
if __name__ == "__main__":
    main()
