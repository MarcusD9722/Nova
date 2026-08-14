"""V3 P2.5 step 1: is Nova's TTFT actually inference latency, or hidden reasoning?

P2 reported TTFT of 114ms - 33,164ms, but it measured `chat_stream`, which only
yields tokens the think filter has already let through. That number is
time-to-first-VISIBLE-token, which conflates two completely different things:

  model compute TTFT      how long llama.cpp takes to emit its first token at all
  visible-response delay  how long Nova then withholds output because the model
                          is inside a <think> block

If the second dominates, this is a reasoning-policy problem and no amount of
inference tuning will touch it. This script measures them separately by driving
llama.cpp directly and applying the same filter logic by hand.

Also varies the ONE thing V2 already proved matters — reasoning mode — across
five candidates, at production token budget, changing one variable at a time.

Run:  venv\\Scripts\\python.exe tests\\bench_ttft_v3.py [reps]
"""

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent

BASE_SYSTEM = (
    "You are Nova — Marcus's AI companion and assistant. Talk like a real person. "
    "Keep it conversational — a sentence or a few.\n\n"
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, notes, or a reasoning block — just say your reply directly."
)

# The P2 scenarios that went pathological, plus the two that stayed fast, so a
# fix cannot be declared on the slow cases while quietly ruining the fast ones.
CASES = [
    ("greeting", "Good morning.", "simple"),
    ("weather", "What's the weather tomorrow?", "simple"),
    ("memory", "What snowboard boots do I own?", "simple"),
    ("ordinal", "What about the second one?", "simple"),
    ("reasoning", "Why does running two CUDA consumers in one process cause an "
                  "illegal memory access? Two sentences.", "hard"),
    ("coding", "I need to add a retry with exponential backoff to an async HTTP "
               "client in Python. What's the cleanest approach?", "hard"),
]

PROD_MAX_TOKENS = 1536          # NOVA_MAX_TOKENS. Never silently changed.


def variants():
    """Five reasoning-mode candidates. Exactly one variable differs per row."""
    return [
        # (label, system_prompt, thinking_switch, max_tokens)
        ("A prod (thinking=True)", BASE_SYSTEM, True, PROD_MAX_TOKENS),
        ("B /no_think switch", BASE_SYSTEM, False, PROD_MAX_TOKENS),
        ("C short budget", BASE_SYSTEM, True, 384),
        ("D prompt-only direct", BASE_SYSTEM + "\n\nAnswer immediately and directly. "
         "Do not deliberate.", True, PROD_MAX_TOKENS),
        ("E deep (no direct instruction)",
         "You are Nova — Marcus's AI companion and assistant.", True, PROD_MAX_TOKENS),
    ]


def apply_no_think(messages, thinking):
    """Mirror of core/llm_runtime._apply_no_think, so this measures the real
    mechanism rather than an approximation of it."""
    if thinking:
        return messages
    msgs = [dict(m) for m in messages]
    for i, m in enumerate(msgs):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            msgs[i]["content"] = (m["content"].rstrip() + "\n\n/no_think").strip()
            return msgs
    return [{"role": "system", "content": "/no_think"}, *msgs]


def run_once(llama, system, user, thinking, max_tokens):
    """One generation, instrumented at the raw stream.

    Returns the fields the brief asks for, with model-compute TTFT and
    visible-response latency kept strictly apart.
    """
    import contextlib
    import io

    messages = apply_no_think(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        thinking)

    sink = io.StringIO()
    t0 = time.perf_counter()
    first_raw = None
    first_visible = None
    raw_tokens = 0
    visible_chars = 0
    raw_text = []
    finish = None

    in_think = False
    buf = ""

    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        stream = llama.create_chat_completion(
            messages=messages, max_tokens=int(max_tokens), temperature=0.4,
            stop=["\n\nUser:", "\n\nAssistant:"], top_k=40, top_p=0.9,
            repeat_penalty=1.15, stream=True,
        )
        for chunk in stream:
            try:
                choice = (chunk.get("choices") or [{}])[0]
                tok = (choice.get("delta") or {}).get("content")
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            except Exception:
                tok = None
            if not tok:
                continue
            if first_raw is None:
                first_raw = time.perf_counter() - t0
            raw_tokens += 1
            raw_text.append(tok)

            # Same boundary logic as _ThinkStreamFilter, inline so the visible
            # timestamp is observed rather than reconstructed afterwards.
            buf += tok
            while True:
                if not in_think:
                    i = buf.find("<think>")
                    if i == -1:
                        safe = len(buf) - 6
                        if safe > 0:
                            if buf[:safe].strip() and first_visible is None:
                                first_visible = time.perf_counter() - t0
                            visible_chars += len(buf[:safe])
                            buf = buf[safe:]
                        break
                    if i > 0:
                        if buf[:i].strip() and first_visible is None:
                            first_visible = time.perf_counter() - t0
                        visible_chars += i
                    buf = buf[i + 7:]
                    in_think = True
                else:
                    j = buf.find("</think>")
                    if j == -1:
                        if len(buf) > 7:
                            buf = buf[-7:]
                        break
                    buf = buf[j + 8:]
                    in_think = False

    total = time.perf_counter() - t0
    raw = "".join(raw_text)
    # Hidden characters = everything the model produced that never surfaced.
    hidden_chars = max(0, len(raw) - visible_chars)
    return {
        "first_raw_ms": (first_raw * 1000) if first_raw is not None else None,
        "first_visible_ms": (first_visible * 1000) if first_visible is not None else None,
        "total_ms": total * 1000,
        "raw_tokens": raw_tokens,
        "raw_chars": len(raw),
        "visible_chars": visible_chars,
        "hidden_chars": hidden_chars,
        "opened_think": raw.count("<think>"),
        "closed_think": raw.count("</think>"),
        "finish": finish,
        "empty_visible": visible_chars == 0,
    }


def agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None
    s = sorted(vals)
    return (statistics.median(s), s[min(len(s) - 1, round(0.9 * (len(s) - 1)))], s[-1])


def fmt(v):
    return "—" if v is None else f"{v:.0f}"


async def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    from core.llm_runtime import LLMRuntime

    print("Nova V3 P2.5 — is TTFT inference, or hidden reasoning?")
    print("=" * 104)
    print(f"reps={reps}  max_tokens={PROD_MAX_TOKENS} (production)  temperature=0.4\n")

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    llama = llm._llama
    run_once(llama, BASE_SYSTEM, "Hi.", True, 128)          # warm

    # ── Part 1: where does the time go, on the production configuration? ─────
    print("=" * 104)
    print("1. PRODUCTION CONFIG — model compute vs hidden reasoning")
    print("=" * 104)
    print(f"{'case':<11} {'kind':<7} {'1st raw':>9} {'1st visible':>12} {'hidden':>9} "
          f"{'visible':>9} {'think':>7} {'total':>9} {'finish':>10}")

    prod_rows = {}
    for key, prompt, kind in CASES:
        runs = [run_once(llama, BASE_SYSTEM, prompt, True, PROD_MAX_TOKENS)
                for _ in range(reps)]
        prod_rows[key] = runs
        med_raw = statistics.median([r["first_raw_ms"] for r in runs if r["first_raw_ms"]])
        vis = [r["first_visible_ms"] for r in runs if r["first_visible_ms"]]
        med_vis = statistics.median(vis) if vis else None
        med_hidden = statistics.median([r["hidden_chars"] for r in runs])
        med_visible = statistics.median([r["visible_chars"] for r in runs])
        opened = statistics.median([r["opened_think"] for r in runs])
        med_total = statistics.median([r["total_ms"] for r in runs])
        finishes = {r["finish"] for r in runs}
        print(f"{key:<11} {kind:<7} {fmt(med_raw):>8}ms {fmt(med_vis):>11}ms "
              f"{med_hidden:>9.0f} {med_visible:>9.0f} {opened:>7.0f} "
              f"{med_total:>8.0f}ms {'/'.join(str(f) for f in finishes):>10}")

    all_raw = [r["first_raw_ms"] for runs in prod_rows.values() for r in runs
               if r["first_raw_ms"]]
    all_vis = [r["first_visible_ms"] for runs in prod_rows.values() for r in runs
               if r["first_visible_ms"]]
    print(f"\n  model compute TTFT      median {fmt(statistics.median(all_raw))}ms")
    if all_vis:
        print(f"  first VISIBLE token     median {fmt(statistics.median(all_vis))}ms")
        print(f"  delay attributable to hidden reasoning: "
              f"{statistics.median(all_vis) - statistics.median(all_raw):.0f}ms median")

    # ── Part 2: reasoning-mode candidates ────────────────────────────────────
    print("\n" + "=" * 104)
    print("2. REASONING MODE CANDIDATES  (one variable changed per row)")
    print("=" * 104)

    results = {}
    for label, system, thinking, budget in variants():
        rows = []
        for key, prompt, kind in CASES:
            for _ in range(reps):
                r = run_once(llama, system, prompt, thinking, budget)
                r["case"] = key
                r["kind"] = kind
                rows.append(r)
        results[label] = rows

        simple = [r for r in rows if r["kind"] == "simple"]
        hard = [r for r in rows if r["kind"] == "hard"]
        s_med, s_p90, s_max = agg([r["first_visible_ms"] for r in simple])
        h_med, _, _ = agg([r["first_visible_ms"] for r in hard])
        empties = sum(1 for r in rows if r["empty_visible"])
        hid = statistics.median([r["hidden_chars"] for r in rows])
        vis = statistics.median([r["visible_chars"] for r in rows])
        print(f"\n  {label}")
        print(f"    SIMPLE visible : median {fmt(s_med):>7}ms  P90 {fmt(s_p90):>7}ms  "
              f"worst {fmt(s_max):>7}ms")
        print(f"    HARD   visible : median {fmt(h_med):>7}ms")
        print(f"    hidden chars   : median {hid:.0f}   visible chars: median {vis:.0f}")
        print(f"    empty visible  : {empties}/{len(rows)}")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print("3. CANDIDATE COMPARISON — simple turns (the pathology) vs hard turns")
    print("=" * 104)
    print(f"{'candidate':<32} {'simple med':>11} {'simple P90':>11} {'simple worst':>13} "
          f"{'hard med':>10} {'empty':>7}")
    for label, rows in results.items():
        simple = [r for r in rows if r["kind"] == "simple"]
        hard = [r for r in rows if r["kind"] == "hard"]
        s_med, s_p90, s_max = agg([r["first_visible_ms"] for r in simple])
        h_med, _, _ = agg([r["first_visible_ms"] for r in hard])
        empties = sum(1 for r in rows if r["empty_visible"])
        print(f"{label:<32} {fmt(s_med):>10}ms {fmt(s_p90):>10}ms {fmt(s_max):>12}ms "
              f"{fmt(h_med):>9}ms {empties:>3}/{len(rows):<3}")

    out = REPO / "docs" / "_nova_v3_ttft_raw.json"
    out.write_text(json.dumps(
        {k: [{kk: vv for kk, vv in r.items()} for r in v] for k, v in results.items()},
        indent=2), encoding="utf-8")
    print(f"\n  raw rows written to {out.relative_to(REPO)}")
    print("\n  P90 and worst-case are the metrics that matter. A candidate that")
    print("  improves the median while leaving a 30s tail has not fixed anything.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
