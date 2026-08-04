"""INTEGRATION (U10): navigation, driven through a live backend.

`test_destination_misroute.py` pins the REGEX. This suite pins the BEHAVIOR
Marcus actually met: he said his family was about to go to sleep and Nova
replied "Happy to route you to sleep — where are you starting from?". The
regex test would not have caught a misroute introduced anywhere else on the
path (pre-pass ordering, pending-state handling, the tool dispatch), and the
misroute also left a pending map request sitting in memory, poisoning the NEXT
turn. Both are only visible with the real pipeline running.

Maps credentials are blanked by the harness, so a genuine route attempt fails
honestly (PluginConfigError) instead of reaching Google. That is deliberate:
these checks are about ROUTING DECISIONS, and the honest-failure text is
itself part of what must not regress.
"""
from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, boot, run

check = Checks()

CHAT_REPLY = "Sounds like a good night. Sleep well."

# Every phrase the routing pre-pass emits, so a check can tell "Nova answered
# in chat" from "Nova tried to navigate".
ROUTING_TELLS = ("route you to", "where are you starting from", "where should i look for the nearest",
                 "i opened it on the map", "put the options on the map")


def routed(text: str) -> bool:
    low = (text or "").lower()
    return any(tell in low for tell in ROUTING_TELLS)


async def pending_map(nova) -> str:
    fact = await nova.memory.get_latest_fact(entity="session", attribute="pending_map_request")
    return (fact.value if fact else "") or ""


async def main() -> None:
    async with boot(default_reply=CHAT_REPLY) as nova:

        check.section("Talking about your evening is NOT a navigation request")
        for text in [
            "We all just got home together and we are about to go to sleep.",
            "I have to go to work tomorrow so I'm turning in early",
            "the kids need to go to school in the morning",
            "we're gonna go to bed after this episode",
        ]:
            res = await nova.say(text, conversation_id=uuid4())
            ok_reply = not routed(res.assistant_text)
            ok_tools = not any(str(c.get("tool", "")).startswith("maps.") for c in res.tool_calls)
            ok_state = not await pending_map(nova)
            check(ok_reply and ok_tools and ok_state,
                  f"stays in conversation: {text[:44]!r} -> {res.assistant_text[:40]!r}")

        check.section("A real navigation request still routes")
        conv = uuid4()
        res = await nova.say("take me to Chipotle", conversation_id=conv)
        check("where are you starting from" in res.assistant_text.lower(),
              f"asks for an origin instead of assuming one ({res.assistant_text[:52]!r})")
        check("Chipotle" in res.assistant_text, "names the destination it heard")
        pending = await pending_map(nova)
        check("Chipotle" in pending and '"kind": "route"' in pending,
              "remembers the pending route across the turn boundary")

        check.section("Answering the origin completes the request — honestly")
        res = await nova.say("Austin, Texas", conversation_id=conv)
        called = [c["tool"] for c in res.tool_calls]
        # The geocode used to be dropped from tool_calls entirely, so a failing
        # maps call was invisible to the UI and the event log.
        check("maps.geocode" in called, f"the stated origin is geocoded (tools: {called})")
        check(not any(c.get("ok") for c in res.tool_calls),
              "with no API key the maps call genuinely fails")
        low = res.assistant_text.lower()
        # Invariant #1. Nova used to answer "I couldn't find 'Austin, Texas' on
        # the map — give me a full address", which is false and unfixable by
        # retyping: the real state is that the key is missing.
        check("google_maps_api_key" in low and "isn't set up" in low,
              f"names the real reason, not a fake 'not found' ({res.assistant_text[:74]!r})")
        check(not any(unit in low for unit in (" miles", " min ", "turn left", "turn right")),
              "no fabricated distance/turn-by-turn")

        check.section("An unrelated message abandons a pending request")
        await nova.say("how do I get to the hardware store", conversation_id=conv)
        check(bool(await pending_map(nova)), "pending route is set")
        res = await nova.say("actually, what did the kids have for lunch", conversation_id=conv)
        check(not await pending_map(nova), "the stale pending route is dropped, not applied")
        check(not routed(res.assistant_text),
              f"the unrelated message gets a normal answer ({res.assistant_text[:40]!r})")

        check.section("Calling it off actually cancels")
        # Found in the live boot check, not by a test: the abort list was
        # exact-match, so "nevermind, forget the directions" was geocoded as an
        # address and Nova replied "I couldn't find 'nevermind, forget the
        # directions' on the map".
        for text in ["nevermind, forget the directions", "no thanks, I'll drive myself", "cancel that"]:
            await nova.say("take me to the hardware store", conversation_id=conv)
            res = await nova.say(text, conversation_id=conv)
            called = [c["tool"] for c in res.tool_calls]
            check("maps.geocode" not in called and not await pending_map(nova),
                  f"cancelled cleanly: {text[:34]!r} -> {res.assistant_text[:40]!r}")

        check.section("Nearest-place requests ask where to look")
        res = await nova.say("where's the nearest coffee shop", conversation_id=uuid4())
        check("where should i look for the nearest" in res.assistant_text.lower(),
              f"asks instead of assuming the device location ({res.assistant_text[:56]!r})")
        check('"kind": "nearest"' in await pending_map(nova), "remembers the pending nearby search")

    check.finish()


run(main)
