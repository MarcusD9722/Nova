"""U7: LLM slot extraction where the regex fast path silently misses.

Contract: the regexes stay FIRST and unchanged (no latency cost, no behavior
change on common phrasings). The model is consulted ONLY when a broad trigger
says "this is that intent" AND the precise pattern returned nothing.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.runtime import RuntimeManager, _extract_user_name, _extract_weather_city
from core.understanding import Understanding

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply, self.raises, self.calls = reply, raises, 0

    async def chat(self, messages, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.reply


class Stub:
    """Bare object exposing just what _llm_slot touches."""
    _SLOT_TRIGGERS = RuntimeManager._SLOT_TRIGGERS
    _SLOT_FIELDS = RuntimeManager._SLOT_FIELDS
    _llm_slot = RuntimeManager._llm_slot

    def __init__(self, understanding):
        self._understanding = understanding


async def main():
    # ── The regexes really do miss these today ──
    check(_extract_user_name("my name is marcus") is None,
          "regex MISSES a lowercase name (the reported bug)")
    check(_extract_user_name("my name is Marcus") == "Marcus",
          "regex still handles the capitalized form (fast path intact)")

    # ── LLM recovers the miss ──
    s = Stub(Understanding(FakeLLM(json.dumps({"name": "marcus"}))))
    check(await s._llm_slot("name", "my name is marcus") == "marcus",
          "LLM recovers the lowercase name")

    s2 = Stub(Understanding(FakeLLM(json.dumps({"destination": "Chipotle"}))))
    check(await s2._llm_slot("destination", "what's the best way over to Chipotle") == "Chipotle",
          "LLM recovers a destination phrased outside the patterns")

    s3 = Stub(Understanding(FakeLLM(json.dumps({"city": "Austin"}))))
    check(await s3._llm_slot("weather", "how's the weather looking out in Austin") == "Austin",
          "LLM recovers a weather city")

    # ── COST CONTROL: no trigger -> the model is never called ──
    llm = FakeLLM(json.dumps({"name": "nope"}))
    quiet = Stub(Understanding(llm))
    check(await quiet._llm_slot("name", "what is the capital of France") is None,
          "unrelated message -> no slot")
    check(llm.calls == 0, "unrelated message -> the model is NEVER called (no added latency)")

    # ── Junk guards: a sentence is not a slot ──
    long_reply = Stub(Understanding(FakeLLM(json.dumps(
        {"name": "well I think they probably want to be called something like Marcus maybe"}))))
    check(await long_reply._llm_slot("name", "my name is marcus") is None,
          "a rambling answer is rejected rather than acted on")
    empty = Stub(Understanding(FakeLLM(json.dumps({"name": ""}))))
    check(await empty._llm_slot("name", "my name is marcus") is None, "empty slot rejected")

    # ── Every failure mode leaves the old behavior ──
    check(await Stub(Understanding(FakeLLM(raises=True)))._llm_slot("name", "my name is marcus") is None,
          "model failure -> None (falls through, unchanged)")
    check(await Stub(Understanding(None))._llm_slot("name", "my name is marcus") is None,
          "no model wired -> None")
    check(await Stub(Understanding(FakeLLM("not json")))._llm_slot("name", "my name is marcus") is None,
          "unparseable reply -> None")
    check(await Stub(Understanding(FakeLLM("{}")))._llm_slot("banana", "my name is marcus") is None,
          "unknown slot kind -> None")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
