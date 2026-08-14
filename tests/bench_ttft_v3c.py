"""V3 P2.5 step 3: template-level reasoning control.

Steps 1 and 2 established:

  * model compute TTFT is 35ms; the 10-33s is hidden reasoning, stripped
  * NO prompt rewording beats production. Five variants were tried and the
    shipped prompt had the best simple median and P90. The promising-looking
    candidate from step 1 did not reproduce — it was variance at n=18.

So prompt-level control does not work on this model. The remaining lever is the
chat template itself: Qwen3 opens a <think> block because the template tells it
to. Two mechanisms can bypass that without touching the prompt text:

  PREFILL   seed the assistant turn with an already-closed think block, so the
            model continues from after it instead of opening its own.
  TEMPLATE  if llama-cpp exposes an enable_thinking-style flag, use it.

This measures both against production, on the same cases, at production budget,
and checks that visible answers are still real answers rather than fast
nonsense.

Run:  venv\\Scripts\\python.exe tests\\bench_ttft_v3c.py [reps]
"""

import asyncio
import contextlib
import io
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from bench_ttft_v3 import CASES, PROD_MAX_TOKENS, agg, fmt  # noqa: E402

SYSTEM = (
    "You are Nova — Marcus's AI companion and assistant. Talk like a real person. "
    "Keep it conversational — a sentence or a few.\n\n"
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, notes, or a reasoning block — just say your reply directly."
)

CLOSED_THINK = "<think>\n\n</think>\n\n"


def strip_think(raw):
    return re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.S).strip()


def timed_stream(llama, messages, budget, prefill=None):
    """Stream and time first-raw / first-visible, optionally with a prefilled
    (already-closed) think block on the assistant turn."""
    msgs = list(messages)
    if prefill:
        msgs = msgs + [{"role": "assistant", "content": prefill}]

    sink = io.StringIO()
    t0 = time.perf_counter()
    first_raw = first_vis = None
    parts = []
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        stream = llama.create_chat_completion(
            messages=msgs, max_tokens=int(budget), temperature=0.4,
            stop=["\n\nUser:", "\n\nAssistant:"], top_k=40, top_p=0.9,
            repeat_penalty=1.15, stream=True)
        for chunk in stream:
            try:
                tok = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
            except Exception:
                tok = None
            if not tok:
                continue
            if first_raw is None:
                first_raw = time.perf_counter() - t0
            parts.append(tok)
            if first_vis is None:
                # With a prefilled closed block there is no <think> to wait for,
                # so the first non-blank token IS the visible answer.
                sofar = "".join(parts)
                if prefill and sofar.strip():
                    first_vis = time.perf_counter() - t0
                elif not prefill and strip_think(sofar):
                    first_vis = time.perf_counter() - t0
    raw = "".join(parts)
    visible = raw.strip() if prefill else strip_think(raw)
    return {
        "first_raw_ms": first_raw * 1000 if first_raw else None,
        "first_visible_ms": first_vis * 1000 if first_vis else None,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "visible": visible,
        "empty": not visible,
        "opened_think": raw.count("<think>"),
    }


async def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    from core.llm_runtime import LLMRuntime

    print("Nova V3 P2.5 — template-level reasoning control")
    print("=" * 104)
    print(f"reps={reps}  max_tokens={PROD_MAX_TOKENS}  same prompt throughout\n")

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    llama = llm._llama

    base = [{"role": "system", "content": SYSTEM}]
    timed_stream(llama, base + [{"role": "user", "content": "Hi."}], 128)   # warm

    modes = [
        ("production (model opens <think>)", None),
        ("PREFILL closed think block", CLOSED_THINK),
    ]

    print(f"{'mode':<36} {'simple med':>11} {'simple P90':>11} {'worst':>9} "
          f"{'hard med':>9} {'empty':>8} {'think':>6}")

    collected = {}
    for label, prefill in modes:
        rows = []
        for key, prompt, kind in CASES:
            for _ in range(reps):
                r = timed_stream(llama, base + [{"role": "user", "content": prompt}],
                                 PROD_MAX_TOKENS, prefill=prefill)
                r["case"], r["kind"] = key, kind
                rows.append(r)
        collected[label] = rows
        simple = [r for r in rows if r["kind"] == "simple"]
        hard = [r for r in rows if r["kind"] == "hard"]
        s_med, s_p90, s_max = agg([r["first_visible_ms"] for r in simple])
        h_med, _, _ = agg([r["first_visible_ms"] for r in hard])
        empties = sum(1 for r in rows if r["empty"])
        opened = sum(r["opened_think"] for r in rows)
        print(f"{label:<36} {fmt(s_med):>10}ms {fmt(s_p90):>10}ms {fmt(s_max):>8}ms "
              f"{fmt(h_med):>8}ms {empties:>3}/{len(rows):<3} {opened:>6}")

    print("\n" + "=" * 104)
    print("ANSWER QUALITY  (fast is worthless if the answer is not an answer)")
    print("=" * 104)
    for label, rows in collected.items():
        print(f"\n  {label}")
        seen = set()
        for r in rows:
            if r["case"] in seen:
                continue
            seen.add(r["case"])
            print(f"    {r['case']:<10} {r['visible'][:100]!r}")

    print("\n" + "=" * 104)
    print("If PREFILL eliminates the think block AND keeps answers real, it is the")
    print("mechanism worth wiring behind a FAST reasoning contract.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
