"""Why does the model return nothing visible, and what actually fixes it?

Measured elsewhere (docs/JARVIS_V2_BENCHMARKS.md §0b): roughly a third of first
attempts produce no visible text, each one costing a full wasted generation. The
retry loop hides it as latency. That is now the largest single cost in
conversational latency, so this script finds the cause rather than guessing.

Two parts:

  1. FORENSICS — capture the RAW model output (before <think> stripping) on
     failures. Did a think block open? Did it close? What was the finish reason?
     Was the output empty at the model, or only after filtering?

  2. CONFIG MATRIX — vary one thing at a time and measure the empty rate:
     the /no_think system-message injection, the stop sequences, and
     repeat_penalty. Whichever measurably lowers the rate is the fix.

Deliberately bypasses LLMRuntime's retry loop and think filter, calling
llama.cpp directly, because the whole point is to see what the loop is hiding.

Run:  venv\\Scripts\\python.exe tests\\bench_empty_generations.py [samples]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_runtime import LLMRuntime, _strip_think  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

_BASE = ("You are Nova — Marcus's AI companion and assistant. Talk like a real person in a "
         "genuine conversation. Keep it conversational — a sentence or a few.\n\n")

# As shipped in core/runtime.py. Note it contains the LITERAL string "<think>".
SYSTEM_TAGGED = _BASE + (
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, notes, or a reasoning/<think> block — just say your reply directly."
)

# The same instruction with the literal tag removed. The forensics showed every
# failure dying inside an unclosed <think> whose contents were the model QUOTING
# this very constraint back to itself, generation stopping at "...or a
# reasoning/" — exactly where the next token is the tag. Naming the tag may be
# teaching the model to emit it.
SYSTEM_CLEAN = _BASE + (
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, or notes — just say your reply directly."
)

# The variant that keeps the instruction's FORCE while dropping the tag. The
# first "clean" attempt removed both the tag and the words "or a reasoning
# block", which stopped the failures but also stopped discouraging the reasoning
# — measured TTFT went up because the model then reasoned freely on every turn.
SYSTEM_NOTAG_STRICT = _BASE + (
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, notes, or a reasoning block — just say your reply directly."
)

SYSTEM = SYSTEM_TAGGED

PROMPTS = [
    "Good morning.",
    "Thanks, that's perfect.",
    "In one sentence, what is a hard drive platter?",
    "In one sentence, what does RAID 1 do?",
    "Should I pick a 28 TB drive or two 14 TB drives? Two sentences.",
    "Is it worth paying 30 dollars more for a quieter drive? Two sentences.",
]

DEFAULT_STOP = ["\n\nUser:", "\n\nAssistant:"]


def raw_generate(llama, messages, *, max_tokens=1536, temperature=0.4,
                 stop=None, repeat_penalty=1.15):
    """One completion, unfiltered, with the finish reason."""
    import contextlib
    import io

    sink = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        out = llama.create_chat_completion(
            messages=messages,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            stop=(DEFAULT_STOP if stop is None else stop),
            top_k=40, top_p=0.9,
            repeat_penalty=float(repeat_penalty),
            stream=False,
        )
    elapsed = time.perf_counter() - t0
    choice = (out.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    return text, choice.get("finish_reason"), elapsed


def msgs(prompt, *, no_think=False, system=None):
    base = [{"role": "system", "content": system or SYSTEM},
            {"role": "user", "content": prompt}]
    if no_think:
        return [{"role": "system", "content": "/no_think"}, *base]
    return base


def analyse(raw):
    """What happened inside a raw completion."""
    visible = _strip_think(raw).strip()
    opened = raw.count("<think>")
    closed = raw.count("</think>")
    return {
        "raw_chars": len(raw),
        "visible_chars": len(visible),
        "think_opened": opened,
        "think_closed": closed,
        "unclosed_think": opened > closed,
        "empty_at_model": len(raw.strip()) == 0,
        "empty_after_filter": len(visible) == 0,
    }


def main():
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    print(f"Loading {model_path.name} ...")
    import asyncio

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    asyncio.run(llm.initialize())
    llama = llm._llama          # diagnostic: we need the unfiltered stream
    print("  loaded\n")

    # ── 1. FORENSICS ─────────────────────────────────────────────────────────
    print("=" * 78)
    print("1. FORENSICS — what the model emits when nothing visible comes out")
    print("=" * 78)

    failures = []
    total = 0
    for prompt in PROMPTS:
        for _ in range(samples):
            raw, finish, elapsed = raw_generate(llama, msgs(prompt))
            info = analyse(raw)
            total += 1
            if info["empty_after_filter"]:
                failures.append((prompt, raw, finish, elapsed, info))

    print(f"\n  {len(failures)}/{total} generations produced no visible text\n")
    for prompt, raw, finish, elapsed, info in failures[:4]:
        print(f"  prompt        : {prompt[:60]!r}")
        print(f"  finish_reason : {finish}")
        print(f"  elapsed       : {elapsed:.2f}s")
        print(f"  raw chars     : {info['raw_chars']}  (visible {info['visible_chars']})")
        print(f"  <think> open  : {info['think_opened']}   closed: {info['think_closed']}"
              f"   UNCLOSED: {info['unclosed_think']}")
        print(f"  empty at model: {info['empty_at_model']}")
        head = raw[:150].replace("\n", "\\n")
        tail = raw[-150:].replace("\n", "\\n")
        print(f"  raw head      : {head!r}")
        print(f"  raw tail      : {tail!r}")
        print()

    if failures:
        unclosed = sum(1 for *_, i in failures if i["unclosed_think"])
        at_model = sum(1 for *_, i in failures if i["empty_at_model"])
        lengths = [i["raw_chars"] for *_, i in failures]
        print(f"  of {len(failures)} failures: {unclosed} had an UNCLOSED <think>, "
              f"{at_model} were empty at the model itself")
        print(f"  raw output length on failure: min {min(lengths)} / "
              f"max {max(lengths)} chars")

    # ── 2. CONFIG MATRIX ─────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("2. CONFIG MATRIX — empty rate per configuration")
    print("=" * 78)

    # The variable the forensics actually implicate is the PROMPT WORDING, which
    # none of the earlier knobs touched. Test it against the previous best knob
    # combination so the two effects can be told apart.
    configs = [
        ("shipped prompt (has <think>)",  dict(sys=SYSTEM_TAGGED,       no_think=False, stop=None, rp=1.15)),
        ("tag + prohibition removed",     dict(sys=SYSTEM_CLEAN,        no_think=False, stop=None, rp=1.15)),
        ("tag removed, prohibition kept", dict(sys=SYSTEM_NOTAG_STRICT, no_think=False, stop=None, rp=1.15)),
    ]

    print()
    print(f"{'config':<36} {'empty':>7} {'rate':>7} {'unclosed':>9} {'mean s':>8} "
          f"{'chars':>7}")
    results = []
    for label, cfg in configs:
        empty = 0
        unclosed = 0
        times = []
        chars = []
        n = 0
        for prompt in PROMPTS:
            for _ in range(samples):
                raw, _finish, elapsed = raw_generate(
                    llama, msgs(prompt, no_think=cfg["no_think"], system=cfg["sys"]),
                    stop=cfg["stop"], repeat_penalty=cfg["rp"])
                info = analyse(raw)
                n += 1
                times.append(elapsed)
                chars.append(info["visible_chars"])
                if info["empty_after_filter"]:
                    empty += 1
                if info["unclosed_think"]:
                    unclosed += 1
        rate = empty / n if n else 0.0
        results.append((label, rate, empty, n))
        print(f"{label:<36} {empty:>3}/{n:<3} {rate:>6.0%} {unclosed:>9} "
              f"{sum(times)/len(times):>7.2f}s {sum(chars)//len(chars):>7}")

    print()
    best = min(results, key=lambda r: r[1])
    baseline = results[0]
    print(f"  baseline empty rate : {baseline[1]:.0%} ({baseline[2]}/{baseline[3]})")
    print(f"  best configuration  : {best[0]!r} at {best[1]:.0%} "
          f"({best[2]}/{best[3]})")
    if best[1] < baseline[1]:
        print(f"  improvement         : {baseline[1] - best[1]:.0%} fewer empty generations")
    else:
        print("  no configuration beat the baseline — the cause is not in these knobs")

    print(f"\n  NOTE: {samples} samples per prompt, {len(PROMPTS)} prompts, temperature 0.4.")
    print("  Re-run before acting; this model is stochastic and n is small.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
