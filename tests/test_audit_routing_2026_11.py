from __future__ import annotations

"""Regression suite for H2 (regexes answering before the model) and H3 (a
conversation that outgrew the context window and stayed broken).

H2's sentences are the ones measured on real weights, verbatim. Each used to be
answered by a pattern — a clock reading, a Maps error, a request for a city —
without the model ever seeing it. The second half of each check is the one that
matters: the real request has to still work, because the cheap fix for a
hijack is to delete the feature.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()


def _probe(text: str) -> list[str]:
    """Which pre-model pattern, if any, claims this message."""
    from core.capabilities.navigation import (extract_destination_from_here,
                                              extract_nearest_query,
                                              extract_place_lookup_query)
    from core.runtime import (_extract_weather_city, _looks_like_time_query,
                              asks_about_weather)

    claimed: list[str] = []
    if _looks_like_time_query(text):
        claimed.append("clock")
    if asks_about_weather(text):
        claimed.append(f"weather({_extract_weather_city(text)})")
    if extract_nearest_query(text):
        claimed.append(f"nearest({extract_nearest_query(text)})")
    if extract_place_lookup_query(text):
        claimed.append(f"place({extract_place_lookup_query(text)})")
    if extract_destination_from_here(text):
        claimed.append(f"route({extract_destination_from_here(text)})")
    return claimed


#: Measured hijacks. Each of these reached a pattern instead of the model.
HIJACKED = [
    "Find the bug: def last_n(items, n): return items[len(items) - n - 1:]",
    "Write a Python function merge_intervals(intervals) that merges overlapping "
    "intervals and show me the code.",
    "What's the time complexity of quicksort?",
    "Tell me the time you were most proud of something you built",
    "Find me a good book about Rome",
    "How do I get to the bottom of this bug?",
    "What's the nearest star?",
    "Bad weather is the worst",
    "Is the weather app in my project any good?",
    "Show me the code for that function",
    "Where is the bug in main.py?",
]

#: …and the requests that must still be answered without the model.
STILL_WORKS = [
    ("What time is it?", "clock"),
    ("What's the time?", "clock"),
    ("What's the weather in Austin right now?", "weather(Austin)"),
    ("What's the weather in Austin?", "weather(Austin)"),
    ("Austin weather today", "weather(Austin)"),
    ("Where's the nearest pharmacy?", "nearest(pharmacy)"),
    ("Find a coffee shop near me", "place(a coffee shop)"),
    ("How do I get to the airport from here?", "route(the airport)"),
    ("Where is Chipotle?", "place(Chipotle)"),
]


async def test_h2_ordinary_sentences_reach_the_model():
    check.section("H2: what the patterns are allowed to claim")
    for text in HIJACKED:
        claimed = _probe(text)
        check(not claimed, f"{text[:48]!r} reaches the model ({claimed})")
    for text, want in STILL_WORKS:
        claimed = _probe(text)
        check(want in claimed, f"{text[:44]!r} is still handled as {want} ({claimed})")


async def test_h2_weather_city_is_a_city():
    check.section("H2: the city sent to the weather API")
    from core.runtime import _extract_weather_city as city

    # "Austin right now" was sent to OpenWeather and came back 404 — twice, in
    # the real error log.
    for text, want in (("What's the weather in Austin right now?", "Austin"),
                       ("what's the weather in Chicago at the moment", "Chicago"),
                       ("weather in New York tomorrow", "New York"),
                       ("hows the weather in San Francisco today?", "San Francisco")):
        got = city(text)
        check(got == want, f"{text[:42]!r} -> {got!r} (want {want!r})")


async def test_h3_an_oversized_prompt_is_trimmed_not_refused():
    check.section("H3: the prompt is made to fit")
    from core.prompt_budget import (DROP, TRIM_OLDEST, TRIM_TAIL, Block,
                                    estimate_tokens, fit)

    recent = "\n".join(
        f"Marcus: message number {i}, with enough length to be realistic about "
        f"his day and the project" for i in range(200))
    blocks = [
        Block("episodic", "EPISODE text " * 500, DROP),
        Block("artifacts", "ARTIFACT text " * 300, DROP),
        Block("memory", "\n".join(f"FACT user note{i} = detail {i}" for i in range(200)), TRIM_TAIL),
        Block("summary", "Earlier they discussed many things. " * 60, TRIM_TAIL),
        Block("tools", "TOOL OUTPUT " * 400, TRIM_TAIL, 2400),
        Block("recent_chat", recent, TRIM_OLDEST),
        Block("persona", "You are Nova. " * 250),
        Block("grounding", "He is Marcus, married to Leslie, two sons."),
        Block("user", "Hello?"),
    ]
    before = sum(estimate_tokens(b.text) for b in blocks)
    limit = 8192 - 1536 - 256
    check(before > 8192, f"the prompt really is oversized to start with ({before} tokens)")
    out, notes = fit(blocks, context_tokens=8192, reply_tokens=1536)
    after = sum(estimate_tokens(b.text) for b in out)
    by = {b.name: b.text for b in out}
    check(after <= limit, f"it fits afterwards ({after} <= {limit})")
    check(notes, f"and says what it gave up ({notes})")
    check(by["persona"] and by["grounding"] and by["user"] == "Hello?",
          "who she is, who she is talking to and the message are never trimmed")
    check("message number 199" in by["recent_chat"],
          "the conversation keeps its NEWEST lines")
    check(by["episodic"] == "" and by["artifacts"] == "",
          "history and what-was-on-screen go first")
    check(len(by["tools"]) >= 2400,
          f"live tool output keeps at least its floor ({len(by['tools'])} chars)")

    # A prompt that already fits is left exactly alone.
    small = [Block("recent_chat", "Marcus: hi", TRIM_OLDEST), Block("persona", "You are Nova.")]
    out2, notes2 = fit(small, context_tokens=8192, reply_tokens=1536)
    check(notes2 == [] and out2[0].text == "Marcus: hi",
          "and an ordinary turn is untouched")


async def test_h3_a_long_conversation_still_answers():
    check.section("H3: end to end, a conversation far past the window")
    async with boot(default_reply="Still here.") as nova:
        conv = None
        # 60 turns of substantial text. Before the fix the prompt grew without
        # limit and llama.cpp refused it; the scripted model does not refuse,
        # so what this asserts is that the assembled prompt STAYS inside the
        # window — which is the property that was missing.
        big = "This is a long message about the project. " * 60
        for i in range(12):
            r = await nova.http.post("/chat", json={
                "message": f"{big} (turn {i})",
                **({"conversation_id": conv} if conv else {})})
            check(r.status_code == 200, f"turn {i} answered ({r.status_code})") if i in (0, 11) else None
            conv = r.json().get("conversation_id") or conv

        from core.prompt_budget import estimate_tokens

        prompts = [p for p in nova.llm.prompts if "You are Nova" in p]
        check(prompts, f"the reply prompt was built ({len(prompts)} of them)")
        worst = max((estimate_tokens(p) for p in prompts), default=0)
        limit = nova.state.llm.context_tokens - 1536
        check(worst <= limit,
              f"the biggest prompt stayed inside the window ({worst} <= {limit})")

        r = await nova.http.post("/chat", json={"message": "Hello?",
                                                "conversation_id": conv})
        body = r.json().get("assistant") or ""
        check(r.status_code == 200 and "internal error" not in body.lower(),
              f"and a short message still gets a real answer ({body[:40]!r})")


async def main():
    await test_h2_ordinary_sentences_reach_the_model()
    await test_h2_weather_city_is_a_city()
    await test_h3_an_oversized_prompt_is_trimmed_not_refused()
    await test_h3_a_long_conversation_still_answers()
    check.finish()


if __name__ == "__main__":
    run(main)
