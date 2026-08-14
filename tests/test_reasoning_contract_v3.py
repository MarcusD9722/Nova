"""V3 P2.5: the FAST/DEEP reasoning contract.

The contract is deliberately not a new router — it rides the `thinking` flag
Nova already threads through every call site. What these tests pin down is that
the flag means what the P2.5 measurements assume it means, and that the callers
which must keep reasoning still do.

No model required: the mechanism is message construction, so it is checked
directly rather than inferred from timings.
"""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_runtime import _CLOSED_THINK_PREFILL, _apply_no_think

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


BASE = [{"role": "system", "content": "You are Nova."},
        {"role": "user", "content": "Good morning."}]


def test_fast_contract():
    print("\nFAST contract (thinking=False)")
    out = _apply_no_think(BASE, thinking=False)
    check(len(out) == len(BASE) + 1, "a trailing assistant turn is appended")
    check(out[-1]["role"] == "assistant", "the appended turn is the assistant's")
    check(out[-1]["content"] == _CLOSED_THINK_PREFILL,
          "it prefills an already-closed reasoning block")
    check("<think>" in _CLOSED_THINK_PREFILL and "</think>" in _CLOSED_THINK_PREFILL,
          "the block is opened AND closed — an unclosed one would be the old bug")
    check(out[:len(BASE)] == BASE, "the caller's own messages are untouched")


def test_deep_contract():
    print("\nDEEP contract (thinking=True)")
    out = _apply_no_think(BASE, thinking=True)
    check(out == BASE, "nothing is added; the model reasons natively")
    check(not any(m.get("role") == "assistant" for m in out),
          "no prefill can suppress reasoning on the deep path")


def test_escape_hatches():
    print("\nescape hatches")
    prev_allow = os.environ.get("NOVA_LLM_ALLOW_THINKING")
    prev_prefill = os.environ.get("NOVA_LLM_FAST_PREFILL")
    try:
        os.environ["NOVA_LLM_ALLOW_THINKING"] = "1"
        check(_apply_no_think(BASE, thinking=False) == BASE,
              "NOVA_LLM_ALLOW_THINKING=1 forces reasoning on even for FAST callers")
        del os.environ["NOVA_LLM_ALLOW_THINKING"]

        os.environ["NOVA_LLM_FAST_PREFILL"] = "0"
        out = _apply_no_think(BASE, thinking=False)
        check(out[0]["content"].endswith("/no_think"),
              "NOVA_LLM_FAST_PREFILL=0 falls back to the old switch")
        check(not any(m.get("role") == "assistant" for m in out),
              "the fallback does not prefill")
    finally:
        os.environ.pop("NOVA_LLM_ALLOW_THINKING", None)
        os.environ.pop("NOVA_LLM_FAST_PREFILL", None)
        if prev_allow is not None:
            os.environ["NOVA_LLM_ALLOW_THINKING"] = prev_allow
        if prev_prefill is not None:
            os.environ["NOVA_LLM_FAST_PREFILL"] = prev_prefill


def _calls_with_thinking(path: str, func_name: str | None = None):
    """Every chat/chat_stream call in a file, with its `thinking` value."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None)
        if name not in {"chat", "chat_stream"}:
            continue
        val = None
        for kw in node.keywords:
            if kw.arg == "thinking":
                val = getattr(kw.value, "value", "?")
        found.append((node.lineno, name, val))
    return found


def test_decision_paths_keep_reasoning():
    print("\nthe paths that MUST keep reasoning still do")
    # The whole safety argument for a fast spoken reply is that reasoning
    # happens where decisions are made. If the agent loop ever went fast, that
    # argument silently evaporates — so it is pinned here rather than trusted.
    agent = _calls_with_thinking("core/orchestrator/agent.py")
    check(bool(agent), f"found agent LLM calls to check ({len(agent)})")
    check(all(v is True for _l, _n, v in agent),
          f"every agent decide() call still reasons ({agent})")

    deep = Path("core/orchestrator/deep.py")
    if deep.exists():
        calls = _calls_with_thinking(str(deep))
        check(all(v is True for _l, _n, v in calls) if calls else True,
              f"deep mode planner/critic still reason ({calls})")
    else:
        print("       (core/orchestrator/deep.py not present — skipped)")


def test_spoken_reply_is_fast():
    print("\nthe spoken reply uses the fast contract")
    src = Path("core/runtime.py").read_text(encoding="utf-8")
    # The streamed conversational reply is the call this phase changed.
    check("temperature=0.4, thinking=False" in src,
          "the streamed reply path passes thinking=False")
    check("FAST contract for the spoken reply" in src,
          "and says why, with the measurement, at the call site")


def test_retries_stay_bounded_and_visible():
    print("\nretries remain bounded and observable")
    src = Path("core/llm_runtime.py").read_text(encoding="utf-8")
    check("empty_retries" in src and "empty_exhausted" in src,
          "empty generations are still counted in usage_stats")
    check("for attempt in range(3)" in src, "retry count is still bounded at 3")
    check("_attempt_messages" in src, "retries still escalate rather than repeat")


def main():
    test_fast_contract()
    test_deep_contract()
    test_escape_hatches()
    test_decision_paths_keep_reasoning()
    test_spoken_reply_is_fast()
    test_retries_stay_bounded_and_visible()
    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
