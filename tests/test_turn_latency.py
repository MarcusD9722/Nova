"""Per-turn latency: fewer LLM calls, and no queueing behind background work.

Marcus measured 30-60s replies "even for a simple one". Two causes, both
pinned here:

  1. Every turn spent a 900-token *thinking* generation on the agent loop's
     "do I need a tool?" decision — including for "good morning".
  2. Background memory work (extraction after every turn, summarization every
     8) shares the ONE 1-permit GPU semaphore with the reply, and the
     semaphore is fair rather than prioritized. Measured live: 3.2s and 6.1s
     for turns with no background work, 41.2s for one that collided with it.

The risk direction for (1) is asymmetric — wrongly skipping the decider costs
a capability silently — so most of these checks are about what must STILL
reach the tool loop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import AGENT_DECIDER_MARKER, Checks, ScriptedLLM, boot, run

from core.intent import is_purely_conversational
from core.turn_gate import TurnGate

check = Checks()


async def test_classifier() -> None:
    check.section("is_purely_conversational — social messages")
    for text in [
        "hey", "hi nova", "good morning", "good night", "thanks!", "thank you",
        "ok", "cool", "awesome", "got it", "sure", "yeah", "haha", "lol",
        "how are you", "how's it going", "what's up", "bye", "see you later",
        "I'm tired", "long day", "I'm good", "love you",
    ]:
        check(is_purely_conversational(text) is True, f"skips the tool loop: {text!r}")

    check.section("is_purely_conversational — must NEVER skip these")
    # This is the half that matters: a false positive here silently costs Nova
    # a capability, and nothing would report it.
    for text in [
        "what's the weather", "hey what's the weather like",
        "good morning, what's on my calendar",
        "hi, remind me to call the dentist at 5",
        "thanks — now search for RTX 5090 prices",
        "ok build me a snake game",
        "what time is it", "what's today's date",
        "how far is Austin", "directions to Chipotle",
        "read me the file", "what do you remember about Leslie",
        "send a message on discord", "generate an image of a fox",
        "who won the game last night", "what's the price of gold",
        "cool, open the project folder",
        "I'm tired of this bug in main.py, can you look at it",
    ]:
        check(is_purely_conversational(text) is False, f"keeps the tool loop: {text[:44]!r}")

    check.section("is_purely_conversational — boundaries")
    check(is_purely_conversational("") is False, "empty text is not fast-pathed")
    check(is_purely_conversational("   ") is False, "whitespace is not fast-pathed")
    check(is_purely_conversational("tell me about the history of the roman empire in detail") is False,
          "a long message is never fast-pathed")
    check(is_purely_conversational("so, hey") is True, "preamble is stripped before matching")
    check(is_purely_conversational("hey there, I was thinking about you") is True,
          "a short social sentence is fine")
    # "today" is on the tool-signal list ("what's on today", "today's date").
    # Vetoing it here is a false NEGATIVE — the safe direction — so it is
    # asserted deliberately rather than tuned away.
    check(is_purely_conversational("hey there, I was thinking about you today") is False,
          "an incidental tool-ish word still vetoes the fast path (safe direction)")
    check(is_purely_conversational("elephant") is False,
          "an unrecognised word is NOT assumed conversational")


async def test_turn_gate() -> None:
    check.section("TurnGate")
    gate = TurnGate()
    check(gate.busy is False, "a fresh gate is idle")
    check(await gate.wait_for_idle() is True, "waiting on an idle gate returns immediately")

    gate.turn_started()
    check(gate.busy is True, "a started turn marks the gate busy")
    check(await gate.wait_for_idle(max_wait_s=0.15) is False,
          "background work waits while a turn is in flight, then proceeds anyway")
    gate.turn_finished()
    check(gate.busy is False, "finishing the turn opens the gate")

    # Overlapping turns must not open the gate early.
    gate.turn_started(); gate.turn_started()
    gate.turn_finished()
    check(gate.busy is True, "the gate stays closed while a second turn is still running")
    gate.turn_finished()
    check(gate.busy is False, "it opens once the last turn finishes")

    # A miscounted finish must never latch the gate closed forever — that
    # would silently stop ALL background memory work.
    gate.turn_finished(); gate.turn_finished()
    check(gate.busy is False, "extra finishes cannot latch the gate closed")

    # Background work really does resume the moment a turn ends.
    gate.turn_started()
    waiter = asyncio.create_task(gate.wait_for_idle(max_wait_s=5))
    await asyncio.sleep(0.05)
    check(not waiter.done(), "the waiter is genuinely blocked")
    gate.turn_finished()
    check(await asyncio.wait_for(waiter, timeout=1) is True, "it is released as soon as the turn ends")

    async with gate.turn():
        check(gate.busy is True, "the async-with scope marks the turn")
    check(gate.busy is False, "and releases it on exit")

    # Even a turn that raises must release the gate.
    try:
        async with gate.turn():
            raise RuntimeError("turn blew up")
    except RuntimeError:
        pass
    check(gate.busy is False, "an exception inside a turn still releases the gate")


async def test_no_decider_call_for_smalltalk() -> None:
    check.section("A social turn makes no tool-decision call (real backend)")
    async with boot(default_reply="Morning, Marcus.") as nova:
        llm = nova.llm

        llm.reset_calls()
        await nova.say("good morning", conversation_id=uuid4())
        decider = llm.prompts_matching(AGENT_DECIDER_MARKER)
        check(not decider, f"'good morning' triggers NO tool-decision call ({len(decider)})")
        check(len(llm.prompts) >= 1, "the reply itself is still generated")

        # NOT a weather/maps/time phrasing: those are answered by the
        # deterministic pre-passes and never reach the tool loop anyway, so
        # they would prove nothing about the decider.
        llm.reset_calls()
        await nova.say("look up the latest reviews for that keyboard", conversation_id=uuid4())
        decider = llm.prompts_matching(AGENT_DECIDER_MARKER)
        check(bool(decider), f"a tool-shaped message STILL runs the decision ({len(decider)})")


async def test_background_yields_to_turn() -> None:
    check.section("Background memory work yields to a live turn")
    async with boot(default_reply="Sure.") as nova:
        from core.turn_gate import GATE

        # With a turn in flight, the extractor must not start its LLM call.
        GATE.turn_started()
        try:
            started = await GATE.wait_for_idle(max_wait_s=0.2)
            check(started is False, "extraction waits rather than taking the GPU mid-turn")
        finally:
            GATE.turn_finished()
        check(await GATE.wait_for_idle(max_wait_s=1) is True, "and proceeds once the turn is done")

        # The turn itself must leave the gate clean afterwards.
        await nova.say("hello there", conversation_id=uuid4())
        check(GATE.busy is False, "a completed turn leaves the gate open")
        check(nova.llm.max_concurrent == 1,
              f"the local model is still never driven concurrently (peak={nova.llm.max_concurrent})")


async def main() -> None:
    await test_classifier()
    await test_turn_gate()
    await test_no_decider_call_for_smalltalk()
    await test_background_yields_to_turn()
    check.finish()


run(main)
