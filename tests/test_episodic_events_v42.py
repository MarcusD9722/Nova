"""V3 P4.2: the promotion semantics P4 defined and P4.1 left unconnected.

P4 wrote `worth_remembering()` with five signals. P4.1 wired one caller to it —
the hot artifact store — which meant `user_selected`, `is_correction`,
`is_failure` and `is_decision` defaulted to False on every real turn. They were
reachable only from unit tests calling the function directly.

So the thing this suite has to prove is not "the rules work" (P4 proved that)
but "the rules are reached by real events". Every episode here is produced by
something that genuinely happened:

    selection    a turn where hot resolution picked the artifact
    correction   the real memory.correct tool, publishing memory.corrected
    failure      a real failing tool through the real ToolRouter
    project      a real build publishing project.started / project.completed

Nothing calls `record_episode`, `record_happening`, or `submit()` with a
hand-made event — except the duplicate-delivery test, whose entire point is
redelivering an event the production path already produced.

Run:  venv\\Scripts\\python.exe tests\\test_episodic_events_v42.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

# The model's side of a small successful build. Copied rather than imported:
# tests/test_it_project_build.py calls run(main) at module scope with no
# __name__ guard, so importing it would run that whole suite and exit.
_PLAN = ('{"summary": "A countdown timer.", "language": "python", '
         '"files": [{"path": "main.py", "purpose": "countdown logic"}], '
         '"run": "python main.py"}')
_MAIN = '''```python
def next_value(current):
    """One countdown step; never goes below zero."""
    return max(0, current - 1)


def countdown(start):
    values = []
    current = start
    while current > 0:
        current = next_value(current)
        values.append(current)
    return values


if __name__ == "__main__":
    print(countdown(3))
```'''
_TEST = '''```python
from main import countdown, next_value

assert next_value(3) == 2
assert countdown(3) == [2, 1, 0]
print("ok")
```'''
_SUGGESTIONS = '{"suggestions": ["Add a pause command", "Support counting up"]}'


#: Stage 14 asks for the acceptance contract BEFORE any code is generated, and
#: then for one check per criterion. A fixture that answers neither leaves the
#: project with no contract at all — which is honestly `idea`, and correctly
#: never completes. This suite is about which MILESTONES become episodes, not
#: about what an unanswered decomposition does, so it scripts both.
_CRITERIA = (
    '{"criteria": ['
    '{"text": "provides a runnable countdown script",'
    ' "origin_quote": "build a python script called Countdown",'
    ' "verify_kind": "machine"},'
    '{"text": "counts down from ten to one",'
    ' "origin_quote": "counting down from ten", "verify_kind": "machine"}]}'
)

_COUNTDOWN_CHECK = (
    "```python\n"
    "import main\n"
    "print('countdown module imported')\n"
    "```"
)


def build_rules():
    return [
        ("ACCEPTANCE CRITERIA", _CRITERIA),
        ("decides ONE question about a program", _COUNTDOWN_CHECK),
        ("Plan a small, complete, WORKING project", _PLAN),
        ("Write the COMPLETE contents of `main.py`", _MAIN),
        ("Write a test file `test_main.py`", _TEST),
        ("TRACE each assertion", _TEST),
        ("A project was just built", _SUGGESTIONS),
    ]

DRIVES = [
    {"title": "Seagate Exos X28", "capacity": "28 TB", "price": "$429"},
    {"title": "WD Gold", "capacity": "26 TB", "price": "$399"},
    {"title": "IronWolf Pro", "capacity": "24 TB", "price": "$389"},
]


# ── helpers ──────────────────────────────────────────────────────────────────

def show_drives(nova, conversation_id: str, turn_id: str = "t-drives"):
    """Put a real result set on screen through the production capture path."""
    from memory.artifacts import capture_tool_result
    return capture_tool_result(
        nova.runtime._artifacts, conversation_id=str(conversation_id), turn_id=turn_id,
        tool="web.search", args={"query": "28 TB hard drives"},
        result={"results": DRIVES})


async def settle(nova, *, timeout: float = 20.0) -> None:
    """Wait for every accepted episode to be written."""
    w = nova.runtime._episodic_worker
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if w.stats["persisted"] + w.stats["failed"] >= w.stats["queued"]:
            return
        await asyncio.sleep(0.02)


async def settle_bus(nova, *, expect: int = 1, timeout: float = 20.0) -> None:
    """Wait for the promoter to decide, THEN for the worker to write.

    Both stages are asynchronous and the second cannot be waited on until the
    first has happened — an empty queue means nothing while a bus event is
    still in flight toward the promoter. Requires the count to hold steady
    across two checks so a partially-arrived batch is not mistaken for a
    finished one.
    """
    p = nova.runtime._promoter
    keys = ("artifact", "selection", "correction", "failure", "project")
    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        decided = sum(p.stats[k] for k in keys)
        if decided >= expect:
            stable += 1
            if stable >= 2:
                break
        await asyncio.sleep(0.05)
    await settle(nova, timeout=timeout)


async def kinds(nova) -> dict[str, int]:
    out: dict[str, int] = {}
    for ep in await nova.runtime._episodes.recent_episodes(limit=200):
        out[ep.kind] = out.get(ep.kind, 0) + 1
    return out


def prompt_containing(nova, needle: str) -> str:
    for p in reversed(nova.llm.prompts):
        if needle.lower() in p.lower():
            return p
    return ""


# ── 1. selection by ordinal ──────────────────────────────────────────────────

async def test_selection_by_ordinal():
    check.section("choosing 'the second one' is an outcome, not just a reference")
    shared = Path(tempfile.mkdtemp(prefix="nova-p42-sel-"))
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            conv = "c-sel"
            show_drives(nova, conv)
            await settle(nova)
            before = await kinds(nova)
            check(before.get("tool_result") == 1, "the search itself was remembered")

            # A QUESTION about the same item must not count as choosing it.
            await nova.say("What about the second one?", conversation_id=None)
            await asyncio.sleep(0.1)
            check(nova.runtime._promoter.stats["selection"] == 0,
                  "asking about an item is not selecting it")

            await nova.say("Let's go with the second one.", conversation_id=conv)
            await settle(nova)

            eps = await nova.runtime._episodes.recent_episodes(limit=20)
            sel = [e for e in eps if e.kind == "selection"]
            check(len(sel) == 1, f"choosing it produced one selection episode ({len(sel)})")
            if not sel:
                return
            ep = sel[0]
            check("WD Gold" in ep.summary, f"the RESOLVED item was recorded ({ep.summary})")
            check(ep.provenance.get("item_index") == 2, "its ordinal position was kept")
            check(ep.provenance.get("parent_id"), "and the result set it came from")
            check(ep.importance > 0.6, f"a choice outranks a search result ({ep.importance})")

            # The chosen artifact row still belongs to the result set it came
            # from. A selection references an artifact; it does not adopt it.
            art = await nova.runtime._episodes.load_artifact(
                ep.provenance["artifact_id"])
            check(art is not None and art.parent_id == ep.provenance["parent_id"],
                  "the artifact still points at its original result set")
            items = await nova.runtime._episodes.load_children(ep.provenance["parent_id"])
            check(len(items) == 3, f"and the set still has all three items ({len(items)})")

            after = await kinds(nova)
            check(after.get("tool_result") == 1,
                  "the original result set was NOT duplicated")
            parent_ep = await nova.runtime._episodes.get_episode(
                f"ep-{ep.provenance['parent_id']}")
            check(parent_ep is not None and parent_ep.access_count >= 1,
                  "it was reinforced instead "
                  f"(access_count={parent_ep.access_count if parent_ep else 'missing'})")

        # ---- survives a restart, and answers the question -----------------
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            eps = await nova.runtime._episodes.recent_episodes(limit=20)
            check(any(e.kind == "selection" for e in eps), "the choice survived a restart")
            nova.llm.reset_calls()
            await nova.say("What drive did I end up choosing?")
            prompt = prompt_containing(nova, "chose")
            check(bool(prompt), "a normal turn retrieved the choice")
            check("WD Gold" in prompt, "and it names the right drive")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


# ── 2. selection by name ─────────────────────────────────────────────────────

async def test_selection_by_name():
    check.section("'let's use the WD Gold' links the named artifact")
    async with boot() as nova:
        conv = "c-name"
        show_drives(nova, conv)
        await settle(nova)

        await nova.say("Let's use the WD Gold.", conversation_id=conv)
        await settle(nova)

        sel = [e for e in await nova.runtime._episodes.recent_episodes(limit=20)
               if e.kind == "selection"]
        check(len(sel) == 1, f"a named choice is a selection too ({len(sel)})")
        if sel:
            check(sel[0].provenance.get("item_index") == 2,
                  "resolved to the same artifact the ordinal would have")
            check(sel[0].trust == "UNTRUSTED_EXTERNAL",
                  f"the item's trust is unchanged by being chosen ({sel[0].trust})")


# ── 3. selection dedupe ──────────────────────────────────────────────────────

async def test_selection_dedupe():
    check.section("saying it again is the same decision")
    async with boot() as nova:
        conv = "c-dup"
        show_drives(nova, conv)
        await settle(nova)

        for phrase in ("Let's go with the second one.",
                       "Yeah, I like that one.",
                       "I'll take the WD Gold."):
            await nova.say(phrase, conversation_id=conv)
        await settle(nova)

        sel = [e for e in await nova.runtime._episodes.recent_episodes(limit=20)
               if e.kind == "selection"]
        check(len(sel) == 1,
              f"three ways of saying it, one durable decision ({len(sel)})")
        check(nova.runtime._promoter.stats["selection"] >= 2,
              "the promoter did see them all — dedupe is by identity, not by luck")


# ── 4. correction ────────────────────────────────────────────────────────────

async def test_correction():
    check.section("a real correction is remembered as an event, not just a new fact")
    shared = Path(tempfile.mkdtemp(prefix="nova-p42-corr-"))
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            from core.tool_router import ToolCall
            await nova.memory.add_fact(entity="user", attribute="gpu", value="RTX 3080",
                                       confidence=0.9)
            # The production correction path: the real memory.correct tool.
            res = await nova.runtime._router.execute(ToolCall(
                "memory.correct", {"entity": "user", "attribute": "gpu",
                                   "value": "RTX 5080"}))
            check(res.ok, f"the correction tool succeeded ({res.error})")
            await settle_bus(nova, expect=1)

            corr = [e for e in await nova.runtime._episodes.recent_episodes(limit=20)
                    if e.kind == "correction"]
            check(len(corr) == 1, f"one correction episode ({len(corr)})")
            if not corr:
                return
            ep = corr[0]
            check("3080" in ep.summary and "5080" in ep.summary,
                  f"it records what changed, not just the new value ({ep.summary})")
            check(ep.provenance.get("signal") == "memory.corrected",
                  "and where the evidence came from")
            check(ep.provenance.get("was") and ep.provenance.get("now"),
                  "old and new values are structured, not only prose")
            check(ep.trust == "TRUSTED_INTERNAL_STATE",
                  f"Nova's own state change is internal, not external ({ep.trust})")

            # The FACT is still fact memory's job, unchanged.
            fact = await nova.memory.get_latest_fact(entity="user", attribute="gpu")
            check(fact is not None and "5080" in fact.value,
                  "fact memory still holds the current value")

            # An ordinary new fact is not a correction.
            n_before = len(corr)
            await nova.memory.add_fact(entity="user", attribute="keyboard",
                                       value="HHKB", confidence=0.9)
            await asyncio.sleep(0.3)
            corr2 = [e for e in await nova.runtime._episodes.recent_episodes(limit=20)
                     if e.kind == "correction"]
            check(len(corr2) == n_before, "an ordinary new fact is not a correction")

        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            nova.llm.reset_calls()
            await nova.say("When did I tell you I upgraded to the 5080?")
            prompt = prompt_containing(nova, "corrected")
            check(bool(prompt), "the correction is retrievable after a restart")
            check("5080" in prompt, "with the value that changed")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


# ── 5. failure ───────────────────────────────────────────────────────────────

async def test_failure_recurrence():
    check.section("a failure becomes durable when it becomes a pattern")
    async with boot() as nova:
        from core.episodic_promoter import FAILURE_RECURRENCE
        from core.tool_router import ToolCall

        async def always_broken(_args):
            raise RuntimeError("libwidget.so: undefined symbol vtable_for_thing")

        nova.runtime._router.register("widget.render", always_broken, "Render a widget.")

        # One real failure. ToolRouter has already retried internally, so this
        # is "failed for real, once" — still not a life event.
        await nova.runtime._router.execute(ToolCall("widget.render", {}), retries=0)
        await asyncio.sleep(0.4)
        fails = [e for e in await nova.runtime._episodes.recent_episodes(limit=50)
                 if e.kind == "failure"]
        check(not fails, f"one failure is not yet worth remembering ({len(fails)})")

        for _ in range(FAILURE_RECURRENCE - 1):
            await nova.runtime._router.execute(ToolCall("widget.render", {}), retries=0)
        await settle_bus(nova, expect=1)

        fails = [e for e in await nova.runtime._episodes.recent_episodes(limit=50)
                 if e.kind == "failure"]
        check(len(fails) == 1, f"the pattern is ({len(fails)})")
        if not fails:
            return
        check("undefined symbol" in fails[0].summary, "the real error text is kept")
        check(fails[0].provenance.get("signature"),
              "keyed on ErrorLog's signature, not raw text")
        check(fails[0].provenance.get("count", 0) >= FAILURE_RECURRENCE,
              "and records how often it happened")

        # More of the same failure updates that episode; it does not add rows.
        for _ in range(4):
            await nova.runtime._router.execute(ToolCall("widget.render", {}), retries=0)
        await settle(nova)
        fails = [e for e in await nova.runtime._episodes.recent_episodes(limit=50)
                 if e.kind == "failure"]
        check(len(fails) == 1, f"seven failures, one episode ({len(fails)})")

        # A DIFFERENT transient that only happens once stays unpromoted.
        async def flaky(_args):
            raise TimeoutError("read timed out")

        nova.runtime._router.register("weather.current", flaky, "Weather.")
        await nova.runtime._router.execute(ToolCall("weather.current", {}), retries=0)
        await asyncio.sleep(0.4)
        fails = [e for e in await nova.runtime._episodes.recent_episodes(limit=50)
                 if e.kind == "failure"]
        check(len(fails) == 1, "a one-off timeout is still not remembered")


# ── 6. project milestones ────────────────────────────────────────────────────

async def test_project_milestones():
    check.section("a real build is a project event; its progress ticks are not")
    shared = Path(tempfile.mkdtemp(prefix="nova-p42-proj-"))
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)},
                        rules=build_rules(), default_reply="Working on it.") as nova:
            from uuid import uuid4
            await nova.say("build a python script called Countdown, counting down from ten",
                           conversation_id=uuid4())
            builder = nova.runtime._project_builder
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if not builder.is_building("countdown"):
                    break
                await asyncio.sleep(0.25)
            await settle_bus(nova, expect=2, timeout=30)

            proj = [e for e in await nova.runtime._episodes.recent_episodes(limit=50)
                    if e.kind == "project_event"]
            check(len(proj) == 2,
                  f"the build start and finish are events ({len(proj)})")
            check(all(e.project == "countdown" for e in proj),
                  "both are attributed to the project")
            starts = [e for e in proj if e.provenance.get("event") == "project.started"]
            ends = [e for e in proj if e.provenance.get("event") == "project.completed"]
            check(len(starts) == 1 and len(ends) == 1, "one of each, not a stream")
            check(ends and ends[0].outcome, f"the finish records its status ({ends[0].outcome if ends else None})")
            # Stage 14: the finish carries the DERIVED state, not the word
            # "ok". Before the promoter was updated it read a field the
            # payload no longer has and recorded every build as "ok".
            check(ends and ends[0].outcome in
                  ("complete", "failing", "passing", "partially_implemented",
                   "scaffolded", "planned", "idea"),
                  f"and it is one of the seven states "
                  f"({ends[0].outcome if ends else None})")
            check(len(proj) < 6,
                  f"the many project.progress ticks were NOT promoted ({len(proj)} total)")

        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            nova.llm.reset_calls()
            await nova.say("What happened the last time we worked on the countdown project?")
            prompt = prompt_containing(nova, "countdown")
            check(bool(prompt), "the project history is retrievable after a restart")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


# ── 7. noise ─────────────────────────────────────────────────────────────────

async def test_noise_rejection():
    check.section("a realistic session promotes events, not turns")
    async with boot() as nova:
        from core.tool_router import ToolCall
        from memory.artifacts import capture_tool_result

        conv = "c-noise"
        turns = 0

        for i in range(20):
            await nova.say(["Hi.", "Thanks!", "Cool.", "Good morning.", "Nice."][i % 5],
                           conversation_id=conv)
            turns += 1
        for q in ("What time is it?", "What's the weather?", "How are you?",
                  "What's 2 + 2?", "Tell me a joke."):
            await nova.say(q, conversation_id=conv)
            turns += 1

        # Three real searches.
        for i in range(3):
            capture_tool_result(
                nova.runtime._artifacts, conversation_id=conv, turn_id=f"t-s{i}",
                tool="web.search", args={"query": f"topic {i}"},
                result={"results": [{"title": f"result {i}-{j}"} for j in range(3)]})
            turns += 1

        # One selection.
        show_drives(nova, conv, turn_id="t-drives")
        await nova.say("I'll take the WD Gold.", conversation_id=conv)
        turns += 2

        # One correction.
        await nova.memory.add_fact(entity="user", attribute="editor", value="vim",
                                   confidence=0.9)
        await nova.runtime._router.execute(ToolCall(
            "memory.correct", {"entity": "user", "attribute": "editor", "value": "helix"}))
        turns += 1

        # One meaningful (recurring) failure.
        async def broken(_args):
            raise RuntimeError("disk quota exceeded on volume D")

        nova.runtime._router.register("backup.run", broken, "Back up.")
        for _ in range(3):
            await nova.runtime._router.execute(ToolCall("backup.run", {}), retries=0)
        turns += 3

        await settle_bus(nova, expect=7, timeout=40)
        counts = await kinds(nova)
        total = sum(counts.values())
        stats = nova.runtime._promoter.status()

        print(f"       {turns} interactions -> {total} durable episodes: {counts}")
        print(f"       promoter: {stats}")

        check(counts.get("tool_result", 0) == 4,
              f"4 result sets (3 searches + the drives) ({counts.get('tool_result')})")
        check(counts.get("selection", 0) == 1, f"1 selection ({counts.get('selection')})")
        check(counts.get("correction", 0) == 1, f"1 correction ({counts.get('correction')})")
        check(counts.get("failure", 0) == 1, f"1 failure ({counts.get('failure')})")
        check(total == 7, f"7 episodes from {turns} interactions ({total})")
        # The 25 trivial turns are not "rejected" — they never reach the
        # promoter at all, because a greeting produces no artifact and no error
        # event. The rejection counter only sees things that DID arrive: the
        # first two occurrences of the failure signature, below its threshold.
        check(stats["rejected"] == 2,
              f"the two sub-threshold failures were rejected ({stats['rejected']})")
        check(stats["failure"] == 1, "and the third promoted exactly once")


# ── 8. the fast path is still free ───────────────────────────────────────────

async def test_fast_path_unaffected():
    check.section("'Good morning' still costs nothing")
    async with boot() as nova:
        store = nova.runtime._episodes
        calls = {"n": 0}
        real_search = store.search_episodes
        real_recent = store.recent_episodes
        real_dec = store.search_decisions

        async def counted(fn):
            async def _w(*a, **k):
                calls["n"] += 1
                return await fn(*a, **k)
            return _w

        store.search_episodes = await counted(real_search)
        store.recent_episodes = await counted(real_recent)
        store.search_decisions = await counted(real_dec)

        show_drives(nova, "c-fast")
        await settle(nova)
        calls["n"] = 0
        writes_before = nova.runtime._episodic_worker.stats["queued"]

        for text in ("Good morning.", "Thanks!", "What time is it?"):
            await nova.say(text, conversation_id="c-fast")
        await asyncio.sleep(0.3)

        check(calls["n"] == 0, f"zero episodic database queries ({calls['n']})")
        check(nova.runtime._episodic_worker.stats["queued"] == writes_before,
              "zero episode writes")
        prompt = nova.llm.prompts[-1] if nova.llm.prompts else ""
        check("From earlier sessions" not in prompt and "chose" not in prompt,
              "zero new prompt characters")


# ── 9. duplicate delivery of a real event ────────────────────────────────────

async def test_duplicate_delivery():
    check.section("redelivering any event type stays idempotent")
    async with boot() as nova:
        from core.tool_router import ToolCall
        conv = "c-idem"
        show_drives(nova, conv)
        await nova.say("Let's go with the WD Gold.", conversation_id=conv)
        await nova.memory.add_fact(entity="user", attribute="shell", value="bash",
                                   confidence=0.9)
        await nova.runtime._router.execute(ToolCall(
            "memory.correct", {"entity": "user", "attribute": "shell", "value": "fish"}))
        await settle_bus(nova, expect=3, timeout=30)

        before = await kinds(nova)

        # Replay the SOURCE events, exactly as a retry or a duplicated publish
        # would. The promoter — not the test — builds the persist events again.
        from core.event_bus import BUS
        BUS.publish("memory.corrected", {"entity": "user", "attribute": "shell",
                                         "was": "bash", "now": "fish"})
        sel = nova.runtime._artifacts.resolve("the WD Gold", conv)
        nova.runtime._note_selection(sel, conv, "t-replay", "Let's go with the WD Gold.")
        await asyncio.sleep(0.6)
        await settle(nova)

        after = await kinds(nova)
        check(after == before, f"no new episodes from redelivery ({before} -> {after})")


# ── 10. disable switch ───────────────────────────────────────────────────────

async def test_disabled_covers_new_paths():
    check.section("NOVA_EPISODIC_MEMORY=0 turns off the new promotion paths too")
    async with boot(env={"NOVA_EPISODIC_MEMORY": "0"}) as nova:
        from core.tool_router import ToolCall
        conv = "c-off"
        show_drives(nova, conv)
        await nova.say("Let's go with the WD Gold.", conversation_id=conv)
        await nova.memory.add_fact(entity="user", attribute="tz", value="CST",
                                   confidence=0.9)
        await nova.runtime._router.execute(ToolCall(
            "memory.correct", {"entity": "user", "attribute": "tz", "value": "CDT"}))
        await asyncio.sleep(0.5)

        check(nova.runtime._episodic_worker.stats["queued"] == 0, "nothing was enqueued")
        p = nova.runtime._promoter.status()
        check(p["selection"] == 0 and p["correction"] == 0, "and nothing was promoted")
        check(not p["listening"], "the bus listener never started")


# ── 11. gate variations ──────────────────────────────────────────────────────

async def test_gate_variations():
    check.section("the new acceptance questions actually open the gate")
    from memory.episodic_recall import is_selection, needs_episodic_memory

    history = [
        ("What drive did I end up choosing?", True),
        ("Which one did I pick in the end?", True),
        ("When did I tell you I upgraded to the 5080?", True),
        ("What went wrong the last time that build failed?", True),
        ("What happened the last time we worked on that project?", True),
        ("Did we ever fix that problem?", True),
        ("Why did that build fail last time?", True),
        # ...and the present tense still does not.
        ("Good morning.", False),
        ("What time is it?", False),
        ("Do we have a spare drive?", False),
        ("What about the second one?", False),
        ("Can you pick a colour for this?", False),
    ]
    wrong = [q for q, want in history if bool(needs_episodic_memory(q)) != want]
    check(not wrong, f"historical gate agrees on all {len(history)} ({wrong or 'none wrong'})")

    selections = [
        ("Let's go with the second one.", True),
        ("I'll take the WD Gold.", True),
        ("I like that one.", True),
        ("We'll use the Seagate.", True),
        ("Going with option 3.", True),
        ("What about the second one?", False),
        ("Tell me more about the WD Gold.", False),
        ("How much is the second one?", False),
        ("Is that one any good?", False),
    ]
    wrong = [q for q, want in selections if is_selection(q) != want]
    check(not wrong, f"selection intent agrees on all {len(selections)} ({wrong or 'none wrong'})")


async def main():
    await test_selection_by_ordinal()
    await test_selection_by_name()
    await test_selection_dedupe()
    await test_correction()
    await test_failure_recurrence()
    await test_project_milestones()
    await test_noise_rejection()
    await test_fast_path_unaffected()
    await test_duplicate_delivery()
    await test_disabled_covers_new_paths()
    await test_gate_variations()
    check.finish()


if __name__ == "__main__":
    run(main)
