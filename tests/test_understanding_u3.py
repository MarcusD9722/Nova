"""U3: LLM-driven understanding, with the deterministic heuristic as fallback.

The contract under test is "never worse than the regex it replaces": with a
working model the judgement improves; with no model, a broken model, a timeout,
or a nonsense answer, behavior is exactly the previous heuristic.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_LLM_UNDERSTANDING", "1")

from core.orchestrator.society import COORDINATOR_ID, select_specialists, select_specialists_smart
from core.understanding import Understanding

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class FakeLLM:
    """Returns a canned reply; can also simulate failure/garbage."""
    def __init__(self, reply="", raises=False, hang=False):
        self.reply, self.raises, self.hang = reply, raises, hang
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("model exploded")
        if self.hang:
            await asyncio.sleep(5)
        return self.reply


async def main():
    # ── classify: model answer is used when valid ──
    u = Understanding(FakeLLM(json.dumps({"label": "task"})))
    check(u.available, "understanding is available with a model + flag on")
    got = await u.classify("could you look into the crash", labels=["task", "conversation"], fallback="conversation")
    check(got == "task", f"valid model label used (got {got})")

    # ── classify: EVERY failure mode returns the fallback ──
    for name, llm in [
        ("model raises", FakeLLM(raises=True)),
        ("empty reply", FakeLLM("")),
        ("garbage reply", FakeLLM("I think maybe it's a task?")),
        ("label not in allowed set", FakeLLM(json.dumps({"label": "banana"}))),
    ]:
        u2 = Understanding(llm, timeout_s=0.5)
        r = await u2.classify("x", labels=["task", "conversation"], fallback="conversation")
        check(r == "conversation", f"fallback used when {name}")

    slow = Understanding(FakeLLM("{}", hang=True), timeout_s=0.05)
    check(await slow.classify("x", labels=["a", "b"], fallback="b") == "b", "fallback used on timeout")

    # ── no model at all -> fallback, and no call attempted ──
    none_u = Understanding(None)
    check(not none_u.available, "unavailable without a model")
    check(await none_u.classify("x", labels=["a", "b"], fallback="a") == "a", "fallback with no model wired")

    # ── flag off restores pure heuristic behavior ──
    os.environ["NOVA_LLM_UNDERSTANDING"] = "0"
    off_llm = FakeLLM(json.dumps({"label": "task"}))
    off = Understanding(off_llm)
    check(not off.available, "flag off -> understanding disabled")
    check(await off.classify("x", labels=["task", "conversation"], fallback="conversation") == "conversation",
          "flag off -> fallback used")
    check(off_llm.calls == 0, "flag off -> the model is never called")
    os.environ["NOVA_LLM_UNDERSTANDING"] = "1"

    # ── rank: unknown ids discarded, empty result falls back ──
    opts = {"chief_engineer": "systems", "psychologist": "wellbeing"}
    ranked = Understanding(FakeLLM(json.dumps({"ids": ["psychologist", "not_a_real_id"]})))
    check(await ranked.rank("q", options=opts, fallback=["chief_engineer"]) == ["psychologist"],
          "rank keeps valid ids and drops unknown ones")
    empty = Understanding(FakeLLM(json.dumps({"ids": []})))
    check(await empty.rank("q", options=opts, fallback=["chief_engineer"]) == ["chief_engineer"],
          "rank falls back when the model returns nothing usable")

    # ── expand_query UNIONS with heuristic terms (never removes them) ──
    exp = Understanding(FakeLLM(json.dumps({"terms": ["wife", "partner"]})))
    out = await exp.expand_query("who is my spouse", fallback=["spouse"])
    check("spouse" in out, "expansion never drops the deterministic term")
    check("wife" in out and "partner" in out, "expansion adds model terms")
    bad = Understanding(FakeLLM("not json"))
    check(await bad.expand_query("q", fallback=["spouse"]) == ["spouse"], "expansion falls back on bad JSON")

    # ── extract: slots parsed, junk falls back ──
    ex = Understanding(FakeLLM(json.dumps({"destination": "Chipotle", "mode": None})))
    slots = await ex.extract("what's the best way over to Chipotle",
                             fields={"destination": "place", "mode": "travel mode"})
    check(slots.get("destination") == "Chipotle", "slot extracted from a phrasing regexes miss")
    check("mode" not in slots, "null slots omitted")

    # ── Specialist routing: the real U3 win, plus fallback parity ──
    cross = "I keep putting off my training"
    kw = select_specialists(cross)
    check("psychologist" not in kw, f"keyword routing MISSES the procrastination angle (got {kw})")

    smart = await select_specialists_smart(
        cross, understanding=Understanding(FakeLLM(json.dumps({"ids": ["psychologist", "fitness_coach"]}))),
    )
    check("psychologist" in smart and "fitness_coach" in smart, f"LLM routing catches both angles (got {smart})")
    check(COORDINATOR_ID in smart, "multi-specialist council still gets a coordinator")

    # no understanding wired -> identical to the old keyword behavior
    check(await select_specialists_smart(cross, understanding=None) == kw,
          "no model wired -> byte-identical to keyword routing")
    broken = await select_specialists_smart(cross, understanding=Understanding(FakeLLM(raises=True)))
    check(broken == kw, "model failure -> falls back to keyword routing")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
