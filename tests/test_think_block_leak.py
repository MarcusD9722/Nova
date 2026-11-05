"""The chain of thought must never reach the reader.

SECOND regression from the M10 fix, found in the same live run as the eos one
(tests/test_audit_medium_2026_11.py :: test_the_eos_token_survives_detokenisation).

M10 replaced a prefilled assistant turn with the template's own
`enable_thinking` flag. With thinking ON, that template writes the OPENING tag
into the generation prompt itself:

    <|im_start|>assistant\\n<think>\\n

So the model's output begins INSIDE the reasoning block and never emits
`<think>`. `_ThinkStreamFilter` started at `_in_think = False` and entered the
block only when it SAW an opening tag, so it passed the whole chain of thought
straight through. Measured, asking for a story with thinking on, the stream
began:

    'Thinking Process:\\n\\n1.  **Analyze the Request:**\\n'

`_strip_think` had the same hole on the non-streaming path: with only a CLOSING
tag present, `_THINK_BLOCK_RE` needs both tags and `_THINK_UNCLOSED_RE` needs
the opening one, so neither matched and the reasoning was returned verbatim.

This is not only a `thinking=True` problem. `chat_stream_ex` ESCALATES: its
final attempt asks for real reasoning even when the caller asked for none, so
an ordinary chat turn that failed the cheap path twice leaked as well. That is
why the state is read per ATTEMPT rather than once per call.

What is asserted here, in the order it has to hold:

  1. production marks the model, and the mark is TRUE of the real template;
  2. the stream filter can start inside the block, with the closing tag split
     across token boundaries the way a real stream splits it;
  3. end to end through `chat_stream` and `chat`, with thinking on;
  4. end to end on the ESCALATED attempt of a thinking=False caller;
  5. and the fix does not eat ordinary answers that carry no tags at all.

Run:  venv\\Scripts\\python.exe tests\\test_think_block_leak.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

check = Checks()

REASONING = ("Thinking Process:\n\n1.  **Analyze the Request:** The user wants "
             "a story. I should pick a setting and a voice.\n2.  **Draft:** A "
             "lighthouse, maybe.\n")
ANSWER = "The lighthouse keeper had not slept in three days."
#: What the real model returns under the handler: no opening tag, because the
#: prompt already carried it.
PRE_OPENED_REPLY = REASONING + "</think>\n\n" + ANSWER
#: A reply with BOTH tags, which is what the model produces when the prompt did
#: not pre-open the block. The old code handled this, and still must.
BOTH_TAGS_REPLY = "<think>\n" + REASONING + "</think>\n\n" + ANSWER


def _strip_to_answer(body: str) -> str:
    """What the model returns when reasoning was turned OFF at the template.

    Everything up to and including the closing tag is reasoning it would never
    have produced, so it goes. A body with no closing tag is already a plain
    answer and is returned untouched — the fake does not try to guess whether
    tag-free text "looks like" reasoning, it just does what it was asked.
    """
    if "</think>" in body:
        return body.split("</think>", 1)[1].strip()
    return body


def _chunks(text: str, n: int = 5) -> list[str]:
    """Token-sized pieces. `n=5` guarantees `</think>` lands across a boundary,
    which is the case a naive `token == '</think>'` check would miss."""
    return [text[i:i + n] for i in range(0, len(text), n)]


class _PreOpenedLlama:
    """A model whose output begins INSIDE the reasoning block.

    `_nova_prompt_opens_think` is the mark `_install_thinking_switch` puts on
    the real model; the first test below proves production really sets it, so
    this is mirroring observed behaviour rather than inventing it.
    """

    def __init__(self, bodies: list[str]) -> None:
        self._nova_prompt_opens_think = True
        self._bodies = list(bodies)
        #: The messages each attempt was actually given, so a test can prove
        #: WHICH attempt it is looking at rather than assuming.
        self.prompts: list[list[dict]] = []
        #: And the budget, because the retry policy escalates it.
        self.budgets: list[int] = []

    def create_chat_completion(self, messages=None, stream=False,
                               max_tokens=0, **kw):  # noqa: ANN001, ARG002
        msgs = list(messages or [])
        self.prompts.append(msgs)
        self.budgets.append(int(max_tokens))
        body = self._bodies.pop(0) if self._bodies else ""
        # A faithful fake does what it was ASKED. When the prompt carries the
        # fast-path marker the template closes the block, so the real model
        # emits no tags and no reasoning — a fake that returns a pre-opened
        # thought anyway is the "fake drifted from the thing it replaces"
        # failure, and it made this suite assert the wrong thing once already.
        if _marker_last(msgs) and body:
            body = _strip_to_answer(body)
        if not stream:
            return {"choices": [{"message": {"content": body}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 40}}

        def _gen():
            for piece in _chunks(body):
                yield {"choices": [{"delta": {"content": piece},
                                    "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return _gen()


def _runtime(fake: _PreOpenedLlama):
    from core.llm_runtime import LLMRuntime

    rt = LLMRuntime(model_path=None, context_tokens=4096)
    rt._llama = fake
    return rt


def _marker_last(msgs: list[dict]) -> bool:
    from core.llm_runtime import _CLOSED_THINK_PREFILL

    return bool(msgs and msgs[-1].get("role") == "assistant"
                and str(msgs[-1].get("content") or "").strip()
                == _CLOSED_THINK_PREFILL.strip())


# ─────────────────────────────────────────────────────────────────────────────
# 1. The state is READ from the model, never assumed
# ─────────────────────────────────────────────────────────────────────────────

def test_production_marks_the_model_and_the_mark_is_true() -> None:
    check.section("the mark comes from production, and says something true")
    from core import llm_runtime

    class _Tiny:
        """Enough for the switch to install: a template it can compile."""

        metadata = {
            "tokenizer.chat_template":
                "{% for m in messages %}{{ m['content'] }}{% endfor %}",
            "tokenizer.ggml.eos_token_id": "248046",
        }
        chat_handler = None

        def detokenize(self, tokens, prev_tokens=None, special=False):  # noqa: ANN001, ARG002
            return b"<|im_end|>" if special else b""

    tiny = _Tiny()
    installed = llm_runtime._install_thinking_switch(tiny)
    check(installed is True, "the switch installs on a model with a template")
    check(getattr(tiny, "_nova_prompt_opens_think", False) is True,
          "and PRODUCTION marks that model as one whose prompt opens <think> "
          "— the mark is not something this test invents")

    # A model WITHOUT the handler is not marked, so the old built-in-format
    # path keeps its old behaviour.
    class _NoTemplate:
        metadata: dict = {}
        chat_handler = None

    bare = _NoTemplate()
    # And it SAYS SO. Without the handler the fast path reverts to the measured
    # 8.2s-median, sometimes-empty behaviour; a boot that loses that silently is
    # a boot nobody can diagnose.
    said: list[tuple] = []
    real_logger = llm_runtime.logger
    llm_runtime.logger = type(
        "_Rec", (), {
            "warning": lambda _s, ev, **kw: said.append(("warning", ev, kw)),
            "info": lambda _s, ev, **kw: said.append(("info", ev, kw)),
            "debug": lambda _s, ev, **kw: said.append(("debug", ev, kw)),
        })()
    try:
        installed_bare = llm_runtime._install_thinking_switch(bare)
    finally:
        llm_runtime.logger = real_logger
    check(installed_bare is False,
          "no template -> no handler (a boot is never stopped by this)")
    check(getattr(bare, "_nova_prompt_opens_think", False) is False,
          "and no mark, so nothing downstream changes for it")
    check(any(lvl == "warning" for lvl, _ev, _kw in said),
          f"and it is logged ABOVE debug, not swallowed ({said!r})")

    # Now: is the mark TRUE of the real template? Rendered with vocab_only, so
    # no VRAM. This is the premise the whole fix rests on.
    gguf = REPO / "model" / "Qwen3.5-9B-Q6_K.gguf"
    if not gguf.exists():
        check(False, f"the model file is not here, so the premise cannot be "
                     f"checked against the real template ({gguf.name})")
        return
    try:
        from llama_cpp import Llama, llama_chat_format as lcf
    except ImportError:
        check(False, "llama_cpp is not importable here")
        return
    llm = Llama(model_path=str(gguf), vocab_only=True, verbose=False)
    template = (llm.metadata or {}).get("tokenizer.chat_template") or ""
    fmt = lcf.Jinja2ChatFormatter(template=template, eos_token="<|im_end|>",
                                  bos_token="", add_generation_prompt=True)
    msgs = [{"role": "user", "content": "Tell me a story."}]
    deep = fmt(messages=msgs).prompt
    check(deep.endswith("<think>\n"),
          f"thinking ON: the real template leaves the block OPEN in the "
          f"GENERATION PROMPT, so the model never emits the tag "
          f"({deep[-20:]!r})")
    fast = fmt(messages=msgs, enable_thinking=False).prompt
    check(fast.endswith("<think>\n\n</think>\n\n"),
          f"thinking OFF: it is closed for the model instead ({fast[-24:]!r})")


def test_the_state_tracks_the_handlers_own_condition() -> None:
    check.section("_prompt_opens_think reads the same marker the handler does")
    from core.llm_runtime import _apply_no_think, _prompt_opens_think

    fake = _PreOpenedLlama([])
    user = [{"role": "user", "content": "hi"}]

    on = _apply_no_think(user, thinking=True)
    off = _apply_no_think(user, thinking=False)
    check(not _marker_last(on), "thinking on: no fast-path marker is appended")
    check(_marker_last(off), "thinking off: the marker IS appended")

    check(_prompt_opens_think(fake, on) is True,
          "so with thinking on the prompt opens the block")
    check(_prompt_opens_think(fake, off) is False,
          "and with thinking off it does not — the same condition the handler "
          "uses to set enable_thinking=False, so the two cannot drift")

    class _Unmarked:
        pass

    check(_prompt_opens_think(_Unmarked(), on) is False,
          "a model without the handler is never treated as pre-opened")
    check(_prompt_opens_think(fake, []) is True,
          "and an empty message list is the thinking-on case, not a crash")

    # The documented field fallback. `/no_think` is a SOFT switch: it asks the
    # model not to reason but does nothing to the template, which still opens
    # the block in the generation prompt. So that path needs the protection
    # too — and the leak it had was worse, because the caller asked for no
    # thinking at all.
    os.environ["NOVA_LLM_FAST_PREFILL"] = "0"
    try:
        soft = _apply_no_think(user, thinking=False)
    finally:
        os.environ.pop("NOVA_LLM_FAST_PREFILL", None)
    check(any("/no_think" in str(m.get("content") or "") for m in soft),
          "NOVA_LLM_FAST_PREFILL=0 falls back to the /no_think switch")
    check(not _marker_last(soft),
          "which appends NO marker, so the template still opens the block")
    check(_prompt_opens_think(fake, soft) is True,
          "and the fallback is therefore covered by the same fix")


# ─────────────────────────────────────────────────────────────────────────────
# 2. The stream filter
# ─────────────────────────────────────────────────────────────────────────────

def test_the_filter_can_start_inside_the_block() -> None:
    check.section("the stream filter, fed the way a real stream feeds it")
    from core.llm_runtime import _ThinkStreamFilter

    f = _ThinkStreamFilter(in_think=True)
    out = "".join(f.feed(piece) for piece in _chunks(PRE_OPENED_REPLY))
    out += f.flush()
    check("Thinking Process" not in out,
          f"no reasoning reaches the reader ({out[:60]!r})")
    check("Analyze the Request" not in out, "none of it, not just the first line")
    check("</think>" not in out, "and the closing tag is consumed, not shown")
    check(out.strip() == ANSWER,
          f"what IS released is exactly the answer ({out.strip()!r})")

    # The default is unchanged, so a reply that carries both tags still works.
    g = _ThinkStreamFilter()
    out2 = "".join(g.feed(piece) for piece in _chunks(BOTH_TAGS_REPLY))
    out2 += g.flush()
    check(out2.strip() == ANSWER,
          f"a both-tags reply is still handled by the default filter "
          f"({out2.strip()!r})")

    # Cut off mid-thought with no closing tag: nothing is an answer, so nothing
    # is released. Better empty (and retried) than leaking.
    h = _ThinkStreamFilter(in_think=True)
    out3 = "".join(h.feed(piece) for piece in _chunks(REASONING))
    out3 += h.flush()
    check(out3 == "",
          f"cut off mid-thought -> nothing released ({out3[:40]!r})")


def test_the_whole_reply_strip_handles_a_closing_tag_alone() -> None:
    check.section("_strip_think, told the reply began inside the block")
    from core.llm_runtime import _strip_think

    leaked = _strip_think(PRE_OPENED_REPLY)
    check("Thinking Process" in leaked,
          "WITHOUT the flag the old regexes strip nothing — this is the bug, "
          "asserted so the test fails if the premise stops being true")

    clean = _strip_think(PRE_OPENED_REPLY, True)
    check("Thinking Process" not in clean,
          f"WITH the flag the reasoning is gone ({clean[:60]!r})")
    check(clean == ANSWER, f"and only the answer is left ({clean!r})")

    check(_strip_think(REASONING, True) == "",
          "all reasoning, no closing tag -> empty, which the retry loop sees")
    check(_strip_think(BOTH_TAGS_REPLY, True) == ANSWER,
          "a both-tags reply is handled either way")


# ─────────────────────────────────────────────────────────────────────────────
# 3 & 4. End to end, which is the only place the leak was visible
# ─────────────────────────────────────────────────────────────────────────────

async def test_a_thinking_stream_leaks_nothing() -> None:
    check.section("end to end: chat_stream with thinking on")
    fake = _PreOpenedLlama([PRE_OPENED_REPLY])
    rt = _runtime(fake)

    got: list[str] = []
    async for tok in rt.chat_stream([{"role": "user", "content": "Tell me a story."}],
                                    max_tokens=256, thinking=True):
        got.append(tok)
    streamed = "".join(got)

    check(len(fake.prompts) == 1, f"one attempt was enough ({len(fake.prompts)})")
    check(not _marker_last(fake.prompts[0]),
          "the attempt really did ask for reasoning (no marker)")
    check("Thinking Process" not in streamed,
          f"the reader never sees the reasoning ({streamed[:70]!r})")
    check("</think>" not in streamed, "nor the tag")
    check(streamed.strip() == ANSWER,
          f"they see the answer ({streamed.strip()!r})")


async def test_a_thinking_chat_call_leaks_nothing() -> None:
    check.section("end to end: chat() with thinking on")
    fake = _PreOpenedLlama([PRE_OPENED_REPLY])
    rt = _runtime(fake)

    reply = await rt.chat([{"role": "user", "content": "Tell me a story."}],
                          max_tokens=256, thinking=True)
    check("Thinking Process" not in reply,
          f"the non-streaming path strips it too ({reply[:70]!r})")
    check(reply == ANSWER, f"leaving the answer ({reply!r})")


async def test_the_escalated_attempt_of_a_fast_turn_leaks_nothing() -> None:
    check.section("end to end: a thinking=False turn that ESCALATES")
    # Attempts 0 and 1 are the cheap path and produce nothing visible; attempt 2
    # asks for real reasoning. That third attempt is a leak path for callers who
    # never asked for thinking at all, which is most of them.
    fake = _PreOpenedLlama(["", "", PRE_OPENED_REPLY])
    rt = _runtime(fake)

    got: list[str] = []
    async for tok in rt.chat_stream([{"role": "user", "content": "Good morning."}],
                                    max_tokens=256, thinking=False):
        got.append(tok)
    streamed = "".join(got)

    check(len(fake.prompts) == 3,
          f"all three attempts ran ({len(fake.prompts)})")
    check(_marker_last(fake.prompts[0]) and _marker_last(fake.prompts[1]),
          "the first two were the cheap path (marker present)")
    check(not _marker_last(fake.prompts[2]),
          "and the third escalated to real reasoning (marker gone) — which is "
          "why the state is read per attempt, not once per call")
    check("Thinking Process" not in streamed,
          f"a caller that never asked for thinking still sees none of it "
          f"({streamed[:70]!r})")
    check(streamed.strip() == ANSWER, f"just the answer ({streamed.strip()!r})")


# ─────────────────────────────────────────────────────────────────────────────
# 5. The fix must not eat ordinary answers
# ─────────────────────────────────────────────────────────────────────────────

async def test_a_decision_is_read_from_the_conclusion_not_the_draft() -> None:
    check.section("the leak was not cosmetic: it changed WHICH tool ran")
    # `Agent.decide` runs with thinking=True and takes
    # `extract_first_json_object(raw)` — the FIRST object in the reply. While the
    # reasoning leaked, that first object was whatever the model drafted inside
    # its own scratchpad and then rejected, not its conclusion. A model weighing
    # options writes candidate JSON as it thinks, so this was reachable on
    # ordinary turns.
    from core.policy._json_extract import extract_first_json_object

    draft = '{"action": "tool", "tool": "shell.exec", "args": {"cmd": "rm -rf ."}}'
    final = '{"action": "respond"}'
    reply = (f'Let me consider the options. I could run a command: {draft} — no, '
             f'that is destructive and I already have what I need.\n'
             f'</think>\n\n{final}')

    fake = _PreOpenedLlama([reply])
    rt = _runtime(fake)
    raw = await rt.chat([{"role": "user", "content": "what's the weather?"}],
                        max_tokens=512, thinking=True)
    obj = extract_first_json_object(raw or "")
    check(obj == {"action": "respond"},
          f"the decision read back is the model's CONCLUSION ({obj!r})")
    check("shell.exec" not in (raw or ""),
          "the rejected draft never reaches the parser at all")

    # And the same reply, unstripped, is what the old code handed over — stated
    # as a check so this test fails if that stops being the thing being fixed.
    was = extract_first_json_object(reply)
    check(was is not None and was.get("tool") == "shell.exec",
          f"whereas the leaked reply parsed to the DRAFT ({was!r})")


async def test_a_tag_free_answer_survives() -> None:
    check.section("the fix does not eat a reply that has no tags at all")
    plain = "It is currently 71°F and clear in Austin."

    # Thinking OFF: the prompt closed the block, the model emits no tags, and
    # the answer must come through untouched. If the pre-opened state were
    # computed wrongly (or hardcoded true), _strip_think would return "".
    fake = _PreOpenedLlama([plain])
    rt = _runtime(fake)
    reply = await rt.chat([{"role": "user", "content": "weather?"}],
                          max_tokens=128, thinking=False)
    check(reply == plain, f"chat(): the answer is intact ({reply!r})")

    fake2 = _PreOpenedLlama([plain])
    rt2 = _runtime(fake2)
    got: list[str] = []
    async for tok in rt2.chat_stream([{"role": "user", "content": "weather?"}],
                                     max_tokens=128, thinking=False):
        got.append(tok)
    check("".join(got) == plain,
          f"chat_stream(): the same ({''.join(got)!r})")


async def test_a_budget_spent_inside_the_block_escalates() -> None:
    check.section("three identical deep attempts are three times the same wait")
    # MEASURED on real weights, a story with thinking on at the audit's
    # 1200-token budget: attempts of 22s, 22s, 22s, all finish=length, all zero
    # visible words — 65.9s of silence and then the caller's apology. The same
    # prompts at 2400 tokens produced 137, 336 and 431 words. The budget is what
    # fails, so repeating it identically buys nothing.
    #
    # The leak hid this: while the reasoning was being handed to the reader,
    # those attempts "succeeded" and the retry loop never ran.
    fake = _PreOpenedLlama([REASONING, REASONING, PRE_OPENED_REPLY])
    rt = _runtime(fake)

    got: list[str] = []
    async for tok in rt.chat_stream([{"role": "user", "content": "Tell me a story."}],
                                    max_tokens=600, thinking=True):
        got.append(tok)
    streamed = "".join(got)

    check(fake.budgets == [600, 1200, 600],
          f"the budget DOUBLES for a thought that was merely long, then "
          f"returns to the caller's ({fake.budgets})")
    check(not _marker_last(fake.prompts[0]) and not _marker_last(fake.prompts[1]),
          "the first two attempts still ask for reasoning")
    check(_marker_last(fake.prompts[2]),
          "and the third turns it OFF — a model that cannot stop deliberating "
          "cannot be out-budgeted")
    check("Thinking Process" not in streamed,
          f"nothing leaks on the way ({streamed[:60]!r})")
    check(streamed.strip() == ANSWER,
          f"and the turn is RESCUED rather than apologised for "
          f"({streamed.strip()!r})")

    # Same policy on the non-streaming path, which had no escalation at all.
    # Measured: one of three live reasoning questions returned nothing after
    # 55.7s of three identical attempts.
    fake2 = _PreOpenedLlama([REASONING, REASONING, PRE_OPENED_REPLY])
    rt2 = _runtime(fake2)
    reply = await rt2.chat([{"role": "user", "content": "Think, then answer."}],
                           max_tokens=600, thinking=True)
    check(fake2.budgets == [600, 1200, 600],
          f"chat() escalates the same way ({fake2.budgets})")
    check(_marker_last(fake2.prompts[2]),
          "and drops reasoning on its last attempt too")
    check(reply and "Thinking Process" not in reply,
          f"with a real answer at the end of it ({(reply or '')[:60]!r})")


async def test_the_fast_chain_is_left_alone() -> None:
    check.section("the thinking=False chain is unchanged")
    # A fast attempt costs ~36ms and its failure IS stochastic, so repeating it
    # is both cheap and effective (0/18 empty, measured). Escalating immediately
    # put a 19.7s P90 back on turns as simple as "Good morning", so this chain
    # must keep its two cheap tries before paying for reasoning.
    fake = _PreOpenedLlama(["", "", PRE_OPENED_REPLY])
    rt = _runtime(fake)
    got: list[str] = []
    async for tok in rt.chat_stream([{"role": "user", "content": "Good morning."}],
                                    max_tokens=600, thinking=False):
        got.append(tok)

    check(fake.budgets == [600, 600, 600],
          f"no budget escalation on the cheap path ({fake.budgets})")
    check(_marker_last(fake.prompts[0]) and _marker_last(fake.prompts[1]),
          "two cheap attempts first")
    check(not _marker_last(fake.prompts[2]),
          "then one deep one, which is the existing measured policy")
    check("".join(got).strip() == ANSWER, "and it still strips the reasoning")

    # `chat()` now follows the same chain. It had NO escalation before, so a
    # twice-failed cheap turn came back empty here while the stream path would
    # have rescued it with the deep attempt.
    fake2 = _PreOpenedLlama(["", "", PRE_OPENED_REPLY])
    rt2 = _runtime(fake2)
    reply = await rt2.chat([{"role": "user", "content": "Good morning."}],
                           max_tokens=600, thinking=False)
    check(fake2.budgets == [600, 600, 600],
          f"chat(): no budget escalation on the cheap path ({fake2.budgets})")
    check(_marker_last(fake2.prompts[0]) and _marker_last(fake2.prompts[1]),
          "chat(): two cheap attempts first")
    check(not _marker_last(fake2.prompts[2]),
          "chat(): then the deep one, same chain as the stream")
    check(reply == ANSWER,
          f"chat(): and the twice-failed turn is rescued ({reply!r})")


async def test_all_three_attempts_empty_is_reported_as_empty() -> None:
    check.section("when even the fallback returns nothing")
    # The honest outcome is an empty stream with finish_reason "empty", which
    # the caller already handles — never reasoning shown as if it were an answer.
    fake = _PreOpenedLlama(["", "", ""])
    rt = _runtime(fake)

    events: list[dict] = []
    async for ev in rt.chat_stream_ex([{"role": "user", "content": "Tell me a story."}],
                                      max_tokens=64, thinking=True):
        events.append(ev)

    text = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    done = [e for e in events if e.get("type") == "done"]
    check(text == "", f"nothing was yielded to the reader ({text[:50]!r})")
    check(len(fake.prompts) == 3,
          f"all three attempts were tried ({len(fake.prompts)})")
    check(bool(done) and done[-1].get("finish_reason") == "empty",
          f"and it is reported as empty, not as a finished answer ({done!r})")


async def main() -> None:
    test_production_marks_the_model_and_the_mark_is_true()
    test_the_state_tracks_the_handlers_own_condition()
    test_the_filter_can_start_inside_the_block()
    test_the_whole_reply_strip_handles_a_closing_tag_alone()
    await test_a_thinking_stream_leaks_nothing()
    await test_a_thinking_chat_call_leaks_nothing()
    await test_the_escalated_attempt_of_a_fast_turn_leaks_nothing()
    await test_a_decision_is_read_from_the_conclusion_not_the_draft()
    await test_a_tag_free_answer_survives()
    await test_a_budget_spent_inside_the_block_escalates()
    await test_the_fast_chain_is_left_alone()
    await test_all_three_attempts_empty_is_reported_as_empty()
    check.finish()


if __name__ == "__main__":
    run(main)
