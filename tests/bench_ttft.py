"""Where does time-to-first-token actually go?

The live voice validation put mean TTFT at 5.96 s against ~1.97 s for XTTS
synthesis, so TTFT is now the dominant term in conversational latency. Before
changing anything, measure which part of it is real — the brief is explicit that
prompt ordering must not be reshuffled on theory.

Three candidate explanations, measured separately:

  A. Hidden reasoning. `thinking=True` lets the model emit a <think> block that
     is stripped before display, so every token of it is pure latency. If this
     dominates, TTFT is a *policy* problem, not a cache problem.
  B. Prompt evaluation. A long system prompt must be evaluated before the first
     token. If this dominates, prefix stability and KV reuse are the lever.
  C. Prefix cache reuse. llama.cpp can reuse a cached prefix across calls. If a
     repeated prefix is much faster, prompt ORDER matters and stable content
     belongs first.

Run:  venv\\Scripts\\python.exe tests\\bench_ttft.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_runtime import LLMRuntime  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Deliberately mixed: trivial social turns and substantive questions, because
# the validation run showed TTFT varying by two orders of magnitude between them.
PROMPTS = [
    ("social", "Good morning."),
    ("social", "Thanks, that's perfect."),
    ("social", "How are you doing today?"),
    ("factual", "In one sentence, what is a hard drive platter?"),
    ("factual", "In one sentence, why is SSD latency lower than HDD latency?"),
    ("factual", "In one sentence, what does RAID 1 do?"),
    ("reasoning", "Should I pick a 28 TB drive or two 14 TB drives for a media server? Two sentences."),
    ("reasoning", "Is it worth paying 30 dollars more for a quieter drive? Two sentences."),
]

# The real Nova system prompt is ~2 KB of stable identity/grounding. Approximated
# here at realistic length so prompt-eval cost is not understated. Names are
# PLACEHOLDERS on purpose — only the token count matters to this measurement, so
# there is no reason for a benchmark to carry real family details.
STABLE_PREFIX = (
    "You are Nova — the user's AI companion and assistant. You're not a corporate help desk; "
    "you're a warm, sharp presence who genuinely knows them and enjoys talking with them. "
    "How you talk: Talk like a real person in a genuine conversation. React to what they "
    "actually said FIRST, before anything else. When they share something about their life — "
    "their day, their family, how they're feeling — respond to THAT like "
    "someone who cares: with warmth, real interest, a little personality. Ask about it. Don't "
    "jump to 'what do you need'. For actual tasks, be crisp and get to work. Never use help-desk "
    "filler. Keep it conversational — a sentence or a few. Never invent tool results or reasons. "
    "Who you're talking to: the primary user, on Windows with an RTX 5080. Family: partner and "
    "two children. Active project: Nova itself. Current focus: voice latency. "
)
# Mirrors core/runtime.py, INCLUDING the deliberate absence of the literal think
# tag — naming it made the model recite the instruction back inside its reasoning
# and produce nothing 30% of the time (tests/bench_empty_generations.py).
TAIL = ("\n\nIMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
        "any analysis, planning, notes, or a reasoning block — just say your reply directly.")


async def measure(llm, messages, *, thinking, max_tokens=1536):
    """Returns (ttft_s, total_s, reply_chars, retries).

    `retries` is the number of generations that produced NO visible output and
    were silently thrown away before this call succeeded. It is measured because
    the first version of this benchmark conflated it with TTFT and produced
    nonsense: "Good morning" appeared to take 19.8 s to first token, when what
    actually happened was two full wasted generations at 1536 tokens each.
    """
    before = llm.usage_stats
    t0 = time.perf_counter()
    ttft = None
    parts = []
    async for token in llm.chat_stream(messages, max_tokens=max_tokens,
                                       temperature=0.4, thinking=thinking):
        if ttft is None:
            ttft = time.perf_counter() - t0
        parts.append(token)
    total = time.perf_counter() - t0
    after = llm.usage_stats
    retries = int(after["empty_retries"]) - int(before["empty_retries"])
    reply = "".join(parts).strip()
    return (ttft if ttft is not None else float("nan")), total, len(reply), retries


async def clean_measure(llm, messages, *, max_tokens=400, tries=6):
    """A TTFT sample from a generation that succeeded on its FIRST attempt.

    Anything else is contaminated by wasted retries and says nothing about
    prompt-evaluation cost. Returns None when no clean sample could be had,
    which is reported rather than papered over with a nan.
    """
    for _ in range(tries):
        ttft, _, chars, retries = await measure(llm, messages, thinking=False,
                                                max_tokens=max_tokens)
        if retries == 0 and chars > 0 and ttft == ttft:
            return ttft
    return None


def fmt(v):
    return f"{v:.3f}s" if v is not None else "no clean sample"


def msgs(user_text, prefix=STABLE_PREFIX):
    return [{"role": "system", "content": prefix + TAIL},
            {"role": "user", "content": user_text}]


async def main():
    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    print(f"Loading {model_path.name} ...")
    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    print(f"  loaded, offload={llm.gpu_status.status}\n")

    # Warm the runtime so the first measurement is not a one-off outlier.
    await measure(llm, msgs("Hi."), thinking=False, max_tokens=64)

    # ── A. thinking on vs off ────────────────────────────────────────────────
    print("=" * 78)
    print("A. HIDDEN REASONING — same prompt, thinking on vs off (max_tokens=1536)")
    print("=" * 78)
    print(f"{'kind':<10} {'prompt':<40} {'think':>6} {'TTFT':>7} {'total':>7} "
          f"{'chars':>6} {'wasted':>7}")

    agg = {True: [], False: []}
    empties = {True: 0, False: 0}
    wasted = {True: 0, False: 0}
    clean_ttft = {True: [], False: []}
    for kind, prompt in PROMPTS:
        for thinking in (True, False):
            ttft, total, chars, retries = await measure(llm, msgs(prompt), thinking=thinking)
            agg[thinking].append((ttft, total))
            wasted[thinking] += retries
            if chars == 0:
                empties[thinking] += 1
            elif retries == 0:
                # Only a first-attempt success measures real first-token latency.
                clean_ttft[thinking].append(ttft)
            print(f"{kind:<10} {prompt[:38]:<40} {str(thinking):>6} "
                  f"{ttft:>6.2f}s {total:>6.2f}s {chars:>6} {retries:>7}")

    print()
    for thinking in (True, False):
        vals = [t for t, _ in agg[thinking] if t == t]
        tot = [t for _, t in agg[thinking]]
        clean = clean_ttft[thinking]
        if vals:
            print(f"  thinking={str(thinking):<5}  observed TTFT {sum(vals)/len(vals):>6.2f}s   "
                  f"total {sum(tot)/len(tot):>6.2f}s")
            print(f"                  TRUE first-attempt TTFT "
                  f"{(sum(clean)/len(clean)) if clean else float('nan'):>6.2f}s "
                  f"(n={len(clean)})   wasted generations {wasted[thinking]}   "
                  f"empty {empties[thinking]}/{len(PROMPTS)}")

    # ── B/C. prompt evaluation and prefix reuse ──────────────────────────────
    print()
    print("=" * 78)
    print("B/C. PROMPT EVAL AND PREFIX REUSE")
    print("=" * 78)

    long_prefix = STABLE_PREFIX * 6      # ~7 KB, a heavily-grounded turn
    probe = "In one sentence, what is a filesystem journal?"

    short_ttft = await clean_measure(llm, msgs(probe, prefix="You are Nova. "))
    long_ttft = await clean_measure(llm, msgs(probe, prefix=long_prefix))
    print(f"  short system prompt (~0.1 KB):  TTFT {fmt(short_ttft)}")
    print(f"  long system prompt  (~7 KB):    TTFT {fmt(long_ttft)}")
    if short_ttft is not None and long_ttft is not None:
        print(f"  cost of the extra ~7 KB:        {long_ttft - short_ttft:+.3f}s")

    # Same long prefix twice in a row: if llama.cpp reuses the cached prefix,
    # the second call should be measurably cheaper.
    a_ttft = await clean_measure(llm, msgs("What is a sector?", prefix=long_prefix))
    b_ttft = await clean_measure(llm, msgs("What is a cylinder?", prefix=long_prefix))
    print(f"\n  same long prefix, 1st call:     TTFT {fmt(a_ttft)}")
    print(f"  same long prefix, 2nd call:     TTFT {fmt(b_ttft)}")
    if a_ttft is not None and b_ttft is not None:
        print(f"  prefix reuse saving:            {a_ttft - b_ttft:+.3f}s")

    # Now change the PREFIX between calls (as injecting fresh memory each turn
    # would) and see whether the saving disappears.
    c_ttft = await clean_measure(
        llm, msgs("What is a track?", prefix=long_prefix + "Note: it is Tuesday. "))
    print(f"  prefix CHANGED, next call:      TTFT {fmt(c_ttft)}")
    if b_ttft is not None and c_ttft is not None:
        print(f"  cost of invalidating prefix:    {c_ttft - b_ttft:+.3f}s")

    u = llm.usage_stats
    print(f"\n  TOTAL wasted generations this run: {u['empty_retries']}   "
          f"turns that produced nothing at all: {u['empty_exhausted']}")

    print("\n" + "=" * 78)
    print("Interpretation is in docs/JARVIS_V2_BENCHMARKS.md — do not act on a")
    print("single run; re-run before making a policy change.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
