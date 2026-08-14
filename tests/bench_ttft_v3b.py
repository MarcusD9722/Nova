"""V3 P2.5 step 2: can the anti-reasoning instruction be removed safely?

Step 1 found that model compute TTFT is 35ms and the 10-33s delay is entirely
hidden reasoning. It also found something counter-intuitive: the candidate that
DROPS the "Do NOT write any analysis, planning, notes, or a reasoning block"
instruction was fastest on hard turns (87ms median vs 14,174ms) and had the
fewest empty replies.

That instruction exists for a reason though — it is what stops the model
narrating its planning into the visible answer. So removing it cannot be judged
on latency alone. This measures BOTH:

  latency        first visible token, simple and hard
  contamination  does analysis leak into what Nova would actually say

A variant that is fast but starts every reply with "Okay, so first I should
consider..." has not fixed anything, it has moved the problem into the user's
ears.

Run:  venv\\Scripts\\python.exe tests\\bench_ttft_v3b.py [reps]
"""

import asyncio
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from bench_ttft_v3 import CASES, PROD_MAX_TOKENS, agg, fmt, run_once  # noqa: E402

PERSONA = "You are Nova — Marcus's AI companion and assistant. Talk like a real person."

# Each variant changes ONLY how output format is requested. None of them names
# analysis, planning or reasoning — that vocabulary is the suspect.
VARIANTS = [
    ("P0 current production",
     PERSONA + " Keep it conversational — a sentence or a few.\n\n"
     "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
     "any analysis, planning, notes, or a reasoning block — just say your reply directly."),
    ("P1 persona only",
     PERSONA),
    ("P2 spoken-voice framing",
     PERSONA + " Reply in a natural speaking voice, one to three sentences."),
    ("P3 answer-first framing",
     PERSONA + " Lead with the answer itself. Keep it to a few sentences."),
    ("P4 spoken + brevity, no meta words",
     PERSONA + " You are speaking out loud. Give the answer in one to three "
     "short sentences."),
]

# Phrases that mean planning has leaked into what Nova would SAY. Checked
# against the visible text only.
LEAK = re.compile(
    r"\b(let me (think|consider|start|break)|first,? i|i should (start|consider|check)|"
    r"step 1|okay,? so|to answer this|my (approach|plan) (is|would)|"
    r"i'll (start by|begin by)|breaking this down|let's break)\b", re.I)


def visible_text(llama, system, prompt, thinking, budget):
    """Re-run and capture the visible text, not just its length."""
    import contextlib
    import io

    from bench_ttft_v3 import apply_no_think

    messages = apply_no_think(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        thinking)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        out = llama.create_chat_completion(
            messages=messages, max_tokens=int(budget), temperature=0.4,
            stop=["\n\nUser:", "\n\nAssistant:"], top_k=40, top_p=0.9,
            repeat_penalty=1.15, stream=False)
    raw = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.S).strip()


async def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    from core.llm_runtime import LLMRuntime

    print("Nova V3 P2.5 — removing the anti-reasoning instruction: latency AND quality")
    print("=" * 104)
    print(f"reps={reps}  max_tokens={PROD_MAX_TOKENS}  thinking=True (unchanged)\n")

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    llama = llm._llama
    run_once(llama, PERSONA, "Hi.", True, 128)

    print(f"{'variant':<32} {'simple med':>11} {'simple P90':>11} {'worst':>9} "
          f"{'hard med':>9} {'empty':>8} {'leaks':>7}")

    table = []
    for label, system in VARIANTS:
        rows = []
        for key, prompt, kind in CASES:
            for _ in range(reps):
                r = run_once(llama, system, prompt, True, PROD_MAX_TOKENS)
                r["case"], r["kind"] = key, kind
                rows.append(r)

        # Quality: one visible sample per case, checked for planning leakage.
        leaks = 0
        samples = []
        for key, prompt, kind in CASES:
            txt = visible_text(llama, system, prompt, True, PROD_MAX_TOKENS)
            if txt and LEAK.search(txt):
                leaks += 1
            samples.append((key, txt[:110]))

        simple = [r for r in rows if r["kind"] == "simple"]
        hard = [r for r in rows if r["kind"] == "hard"]
        s_med, s_p90, s_max = agg([r["first_visible_ms"] for r in simple])
        h_med, _, _ = agg([r["first_visible_ms"] for r in hard])
        empties = sum(1 for r in rows if r["empty_visible"])
        table.append((label, s_med, s_p90, s_max, h_med, empties, len(rows), leaks, samples))
        print(f"{label:<32} {fmt(s_med):>10}ms {fmt(s_p90):>10}ms {fmt(s_max):>8}ms "
              f"{fmt(h_med):>8}ms {empties:>3}/{len(rows):<3} {leaks:>4}/{len(CASES)}")

    print("\n" + "=" * 104)
    print("VISIBLE OUTPUT SAMPLES  (does planning leak into what Nova would say?)")
    print("=" * 104)
    for label, *_rest, samples in table:
        print(f"\n  {label}")
        for key, txt in samples[:3]:
            print(f"    {key:<10} {txt!r}")

    print("\n" + "=" * 104)
    print("A variant is only a win if it lowers P90 AND worst case, keeps empties low,")
    print("AND does not leak planning into the spoken answer. Latency alone is not enough.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
