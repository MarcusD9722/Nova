"""Full-audit coverage for core/policy/ (previously none).

This is the layer that turns model text into DECISIONS: what Nova remembers,
what she summarizes, what an autonomous task does next. Every class here has
the same shape — call the model, extract JSON, validate, fall back on failure —
and every fallback is silent by design so a bad generation can't crash a turn.

That design is right, but it means a systematic parse failure looks exactly
like "the user said nothing worth remembering". So these tests care most about
the boundary: what survives a malformed response, and what gets silently
dropped that shouldn't be.

Driven by harness.ScriptedLLM, so the model's exact output is chosen per case.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, ScriptedLLM

from core.policy._json_extract import extract_first_json_object
from core.policy.autonomy_planner import AutonomyPlannerLLM
from core.policy.contracts import MemoryExtractorOutput
from core.policy.followup_generator import FollowUpGeneratorLLM
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.storyteller import is_story_request, story_system_prompt
from core.policy.summarizer import SummarizerLLM

check = Checks()


def llm_returning(text: str) -> tuple[ScriptedLLM, asyncio.Semaphore]:
    m = ScriptedLLM()
    m.default_reply = text
    return m, asyncio.Semaphore(1)


async def test_json_extract() -> None:
    check.section("_json_extract — parses EVERY LLM decision Nova makes")

    check(extract_first_json_object('{"a": 1}') == {"a": 1}, "plain JSON")
    check(extract_first_json_object('  {"a": 1}  ') == {"a": 1}, "surrounding whitespace")
    check(extract_first_json_object('Sure! {"a": 1} hope that helps')["a"] == 1, "JSON wrapped in prose")
    check(extract_first_json_object('```json\n{"a": 1}\n```')["a"] == 1, "JSON in a markdown fence")
    check(extract_first_json_object('{"a": {"b": [1,2]}}')["a"]["b"] == [1, 2], "nested structures")
    check(extract_first_json_object('{"s": "a } brace in a string"}')["s"] == "a } brace in a string",
          "braces inside strings do not end the object")
    check(extract_first_json_object(r'{"s": "escaped \" quote"}')["s"] == 'escaped " quote',
          "escaped quotes inside strings")
    check(extract_first_json_object('{"a":1} {"b":2}')["a"] == 1, "the FIRST of two objects wins")
    check(extract_first_json_object('not json at all {oops} {"a":1}')["a"] == 1,
          "recovers past an unparseable brace group")

    check(extract_first_json_object("") is None, "empty string -> None")
    check(extract_first_json_object(None) is None, "None -> None")
    check(extract_first_json_object("just prose") is None, "prose with no JSON -> None")
    check(extract_first_json_object("[1, 2, 3]") is None, "a bare array is not a dict -> None")
    check(extract_first_json_object('{"a": 1') is None, "an unterminated object -> None")

    # Known limits — documented, not asserted as desirable.
    check(extract_first_json_object("{'a': 1}") is None, "single-quoted pseudo-JSON is NOT accepted (documented limit)")
    check(extract_first_json_object('{"a": 1,}') is None, "a trailing comma is NOT accepted (documented limit)")

    big = '{"k": "' + "x" * 200000 + '"}'
    check(extract_first_json_object(big)["k"].startswith("xxx"), "a 200KB payload parses without blowing up")


async def test_memory_extractor() -> None:
    check.section("MemoryExtractorLLM")

    good = '{"facts": [{"entity":"user","attribute":"spouse","value":"Leslie","confidence":0.9,"persist":true}]}'
    m, sem = llm_returning(good)
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="my wife is Leslie")
    check(len(out.facts) == 1 and out.facts[0].value == "Leslie", "a well-formed fact is extracted")
    check(out.facts[0].persist is True, "persist defaults true")

    for label, reply in [
        ("empty model reply", ""),
        ("pure prose", "I could not find any facts."),
        ("malformed JSON", '{"facts": [ {'),
        ("wrong shape", '{"something_else": 1}'),
    ]:
        m, sem = llm_returning(reply)
        out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="hi")
        check(out.facts == [], f"{label} -> empty facts, no raise")

    m, sem = llm_returning('Here you go:\n```json\n' + good + '\n```')
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="x")
    check(len(out.facts) == 1, "fenced + prose-wrapped output still extracts")

    # ── BUG 1: one unsupported attribute discards EVERY fact in the batch ──
    mixed = ('{"facts": ['
             '{"entity":"user","attribute":"spouse","value":"Leslie","confidence":0.9,"persist":true},'
             # Deliberately a PASSING STATE, not a durable property — the line
             # the widened attribute set is drawn at. ("favorite_color" used to
             # be the example here and is now legitimately supported.)
             '{"entity":"user","attribute":"feeling_right_now","value":"tired","confidence":0.9,"persist":true},'
             '{"entity":"user","attribute":"child","value":"Mateo","confidence":0.9,"persist":true}'
             ']}')
    m, sem = llm_returning(mixed)
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="x")
    values = sorted(f.value for f in out.facts)
    check(values == ["Leslie", "Mateo"],
          f"an unsupported attribute drops ONLY itself, keeping the valid facts (got {values})")

    # ── BUG 2: "mom"/"dad" validate but nothing downstream can read them ──
    m, sem = llm_returning('{"facts":[{"entity":"user","attribute":"mom","value":"Tara",'
                           '"confidence":0.9,"persist":true}]}')
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="my mom is Tara")
    attrs = [f.attribute for f in out.facts]
    check(attrs == ["mother"],
          f"'mom' is normalized to the canonical 'mother' everything else reads (got {attrs})")

    m, sem = llm_returning('{"facts":[{"entity":"user","attribute":"dad","value":"Ron",'
                           '"confidence":0.9,"persist":true}]}')
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="my dad is Ron")
    check([f.attribute for f in out.facts] == ["father"], "'dad' is normalized to 'father'")

    m, sem = llm_returning('{"facts":[{"entity":"user","attribute":"spouse","value":"L",'
                           '"confidence":5,"persist":true}]}')
    out = await MemoryExtractorLLM(m, llm_semaphore=sem).extract(user_text="x")
    check(out.facts == [], "an out-of-range confidence drops that fact")


async def test_summarizer() -> None:
    check.section("SummarizerLLM")

    m, sem = llm_returning('{"summary":"They talked about the kids.","key_facts":null,"open_loops":null}')
    out = await SummarizerLLM(m, llm_semaphore=sem).summarize(transcript="...")
    check(out.summary == "They talked about the kids.", "a valid summary is returned")

    for label, reply in [("empty", ""), ("prose", "no json here"), ("broken", "{{{")]:
        m, sem = llm_returning(reply)
        out = await SummarizerLLM(m, llm_semaphore=sem).summarize(transcript="...")
        check(out.summary == "", f"{label} reply -> empty summary, no raise")

    m, sem = llm_returning('{"summary":"S","key_facts":[{"k":"v"}],"open_loops":[{"o":"1"}]}')
    out = await SummarizerLLM(m, llm_semaphore=sem).summarize(transcript="...")
    check(out.key_facts == [{"k": "v"}] and out.open_loops == [{"o": "1"}], "optional lists round-trip")


async def test_followup() -> None:
    check.section("FollowUpGeneratorLLM")

    m, sem = llm_returning('{"follow_up_question":"How did the fort turn out?"}')
    out = await FollowUpGeneratorLLM(m, llm_semaphore=sem).regenerate(user_text="x", avoid=[])
    check(out.follow_up_question.endswith("?"), "a question is returned")

    m, sem = llm_returning('{"follow_up_question":"How did it go."}')
    out = await FollowUpGeneratorLLM(m, llm_semaphore=sem).regenerate(user_text="x", avoid=[])
    check(out.follow_up_question == "How did it go?", "a missing '?' is repaired")

    m, sem = llm_returning('{"follow_up_question":"Really!!"}')
    out = await FollowUpGeneratorLLM(m, llm_semaphore=sem).regenerate(user_text="x", avoid=[])
    check(out.follow_up_question == "Really?", "trailing punctuation is normalized to '?'")

    m, sem = llm_returning("nonsense")
    out = await FollowUpGeneratorLLM(m, llm_semaphore=sem).regenerate(user_text="x", avoid=[])
    check(out.follow_up_question == "", "an unparseable reply yields an empty question, no raise")


async def test_autonomy_planner() -> None:
    check.section("AutonomyPlannerLLM")

    tools = ["weather.current", "web.search"]
    plan = ('{"action":"tool","reason":"need weather","tool_calls":'
            '[{"tool":"weather.current","args":{"city":"Austin"}}],"new_tasks":[],"message_to_user":null}')
    m, sem = llm_returning(plan)
    out = await AutonomyPlannerLLM(m, llm_semaphore=sem).plan(
        title="t", details="d", memory_context="", available_tools=tools)
    check(out.action == "tool" and len(out.tool_calls) == 1, "a valid plan is returned")
    check(out.tool_calls[0].args == {"city": "Austin"}, "tool args survive")

    # The registry guard is a real safety property: a hallucinated tool must
    # never reach the router.
    bad = ('{"action":"tool","reason":"r","tool_calls":'
           '[{"tool":"launch.missiles","args":{}},{"tool":"web.search","args":{}}],'
           '"new_tasks":[],"message_to_user":null}')
    m, sem = llm_returning(bad)
    out = await AutonomyPlannerLLM(m, llm_semaphore=sem).plan(
        title="t", details="d", memory_context="", available_tools=tools)
    check([tc.tool for tc in out.tool_calls] == ["web.search"],
          "a tool outside the registry is stripped, the real one kept")

    for label, reply, reason in [
        ("unparseable", "I think we should wait.", "planner_unparseable"),
        ("invalid shape", '{"action":"explode"}', "planner_invalid"),
    ]:
        m, sem = llm_returning(reply)
        out = await AutonomyPlannerLLM(m, llm_semaphore=sem).plan(
            title="t", details="d", memory_context="", available_tools=tools)
        check(out.action == "idle" and out.reason == reason,
              f"{label} -> idle with an honest reason ({out.reason})")
        check(out.tool_calls == [], f"{label} -> no tool calls")


async def test_storyteller() -> None:
    check.section("storyteller triggers")

    for text in ["tell me a story", "write me a short story about a fox",
                 "continue the story", "make up a tale"]:
        check(is_story_request(text) is True, f"strong trigger: {text!r}")

    for text in ["what's the weather", "build me a game", "how are you"]:
        check(is_story_request(text) is False, f"not a story request: {text!r}")

    check(is_story_request("keep going") is False, "a weak trigger alone does NOT start a story")
    check(is_story_request("keep going", story_active=True) is True,
          "a weak trigger continues an ACTIVE story")
    check(is_story_request("") is False, "empty text is not a story request")

    p = story_system_prompt("Marcus", "Chapter 1: the fox ran.")
    check("Marcus" in p, "the prompt addresses the reader by name")
    check("Chapter 1" in p, "prior story state is carried into the prompt")
    check("the reader" in story_system_prompt(None, ""), "a missing name degrades to 'the reader'")


async def test_semaphore_is_respected() -> None:
    """Every policy class takes the GPU semaphore. If one forgot, concurrent
    policy work would drive llama.cpp in parallel — the CUDA crash class."""
    check.section("All policy classes serialize on their semaphore")
    m = ScriptedLLM()
    m.default_reply = '{"facts": []}'
    m.call_delay = 0.02
    sem = asyncio.Semaphore(1)

    ex = MemoryExtractorLLM(m, llm_semaphore=sem)
    await asyncio.gather(*(ex.extract(user_text=f"msg {i}") for i in range(8)))
    check(m.max_concurrent == 1,
          f"8 concurrent extractions never overlap on the model (peak={m.max_concurrent})")


async def main() -> None:
    await test_json_extract()
    await test_memory_extractor()
    await test_summarizer()
    await test_followup()
    await test_autonomy_planner()
    await test_storyteller()
    await test_semaphore_is_respected()
    check.finish()


asyncio.run(main())
