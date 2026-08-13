"""Measured benchmarks for the JARVIS V2 changes.

Only reports things this machine can actually measure without a live model or
GPU synthesis. Anything requiring llama.cpp generation or real XTTS audio is
NOT estimated here — see docs/JARVIS_V2_BENCHMARKS.md for what remains
unmeasured and why.

Run:  venv\\Scripts\\python.exe tests\\bench_jarvis_v2.py
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools.selector import ToolEmbeddingCache, ToolSelector
from core.voice.chunker import SpeechChunker
from core.voice.speech_text import to_spoken
from memory.recall_gate import should_recall

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tool_selector_jv2 import DATASET, TOOLS  # noqa: E402


def approx_tokens(text: str) -> int:
    """Rough token count. Good enough for a ratio, and stated as approximate."""
    return max(1, len(text) // 4)


def v1_split(buffer: str):
    """The pre-V2 splitter, verbatim from backend/app.py before this round."""
    parts = re.split(r"(?<=[.!?])\s+", buffer)
    if len(parts) <= 1:
        if len(buffer) > 260:
            cut = buffer.rfind(" ", 60, 260)
            if cut > 0:
                return [buffer[:cut].strip()], buffer[cut + 1:]
        return [], buffer
    return [p.strip() for p in parts[:-1] if p.strip()], parts[-1]


REPLIES = [
    "Right — here are three that fit. The Seagate Exos X28 is 28 TB and the cheapest at "
    "$429, though it is the loudest of the three by a fair margin.",
    "Okay, I found the problem with your server configuration, and there are three things "
    "we should change before testing it again.",
    "Dr. Chen's review puts the WD Gold at 3.5 sones, which is quieter than the Exos, and "
    "the warranty runs five years.",
    "Good morning. It is currently 31 degrees at Hunter with light snow expected after "
    "two, so the early runs should be good.",
]


def bench_first_chunk():
    print("\n== Time-to-first-speakable-chunk (proxy: characters buffered) ==")
    print("   Lower is better: the voice cannot start until the first chunk exists.")
    print(f"   {'reply':<8} {'V1 chars':>9} {'V2 chars':>9} {'change':>9}")
    v1_total = v2_total = 0
    for i, reply in enumerate(REPLIES, 1):
        # V1: stream until the old splitter yields something.
        buf, v1_first = "", None
        for ch in reply:
            buf += ch
            done, buf = v1_split(buf)
            if done:
                v1_first = len(done[0])
                break
        if v1_first is None:
            v1_first = len(reply)

        c = SpeechChunker()
        v2_first = None
        for ch in reply:
            out = c.feed(ch)
            if out:
                v2_first = len(out[0])
                break
        if v2_first is None:
            v2_first = len(reply)

        v1_total += v1_first
        v2_total += v2_first
        delta = (v2_first - v1_first) / v1_first * 100 if v1_first else 0
        # A V1 "win" under ~12 chars is not a win: it means V1 split on an
        # abbreviation and would have XTTS say "Doctor." as a whole utterance,
        # then pause. Flag it rather than counting it as lower latency.
        flag = "  <- V1 split on an abbreviation" if v1_first < 12 else ""
        print(f"   {i:<8} {v1_first:>9} {v2_first:>9} {delta:>8.0f}%{flag}")

    change = (v2_total - v1_total) / v1_total * 100
    print(f"   {'mean':<8} {v1_total / len(REPLIES):>9.0f} {v2_total / len(REPLIES):>9.0f} "
          f"{change:>8.0f}%")
    return v1_total / len(REPLIES), v2_total / len(REPLIES)


def bench_chunk_quality():
    print("\n== Chunk correctness on text V1 mis-split ==")
    cases = [
        "The Exos holds 3.5 TB per platter. That is a lot.",
        "Dr. Chen reviewed it. He liked it.",
        "Check e.g. the WD Gold. It is quieter.",
        "Open README.md and read it. Then tell me.",
    ]
    v1_bad = v2_bad = 0
    for text in cases:
        done, rest = v1_split(text)
        v1_parts = done + ([rest.strip()] if rest.strip() else [])
        c = SpeechChunker(first_min_chars=10_000, clause_after=10_000, max_chars=10_000)
        v2_parts = c.feed(text) + c.flush()
        if len(v1_parts) != 2:
            v1_bad += 1
        if len(v2_parts) != 2:
            v2_bad += 1
        print(f"   {text[:44]:<46} V1={len(v1_parts)} V2={len(v2_parts)} (want 2)")
    print(f"   mis-splits: V1={v1_bad}/{len(cases)}  V2={v2_bad}/{len(cases)}")


def _run_selector(sel, label, full_tokens):
    queries = [q for q, _, _ in DATASET]
    n = len(queries)

    cold_start = time.perf_counter()
    sel.select(queries[0], TOOLS)          # first call also builds the vector cache
    cold_ms = (time.perf_counter() - cold_start) * 1000

    total_tools = total_tokens = 0
    start = time.perf_counter()
    for q in queries:
        result = sel.select(q, TOOLS)
        total_tools += len(result.tools)
        total_tokens += approx_tokens("\n".join(f"- {t}: {TOOLS[t]}" for t in result.tools))
    elapsed = time.perf_counter() - start

    print(f"   -- {label}")
    print(f"      tools shown (mean)        {total_tools / n:.1f} of {len(TOOLS)}")
    print(f"      catalogue (mean)          ~{total_tokens / n:.0f} tokens "
          f"({1 - (total_tokens / n) / full_tokens:.0%} smaller)")
    print(f"      first call                {cold_ms:.1f} ms")
    print(f"      per turn thereafter       {elapsed / n * 1000:.2f} ms")
    print(f"      stages                    {sel.stats}")
    print(f"      per TURN @ step_budget=6  ~{full_tokens * 6} -> "
          f"~{total_tokens / n * 6:.0f} tokens")
    return total_tokens / n


def bench_selector():
    print("\n== Tool selection ==")
    full_catalog = "\n".join(f"- {n}: {d}" for n, d in sorted(TOOLS.items()))
    full_tokens = approx_tokens(full_catalog)
    print(f"   registry size                {len(TOOLS)} tools")
    print(f"   full catalogue               ~{full_tokens} tokens per decide() prompt")

    # Cold process: embeddings are not loaded yet, so selection ranks lexically
    # and widens. This is genuinely what the first turn after boot sees.
    _run_selector(ToolSelector(cache=ToolEmbeddingCache(enabled=False)),
                  "lexical only (embeddings not loaded / disabled)", full_tokens)

    # Steady state: bge-small is resident, which it will be in any real session
    # because memory uses it too.
    from memory.embeddings import embed_texts, embedding_available

    if embedding_available():
        _run_selector(ToolSelector(cache=ToolEmbeddingCache(embed=embed_texts,
                                                            model_id="bench")),
                      "semantic (bge-small resident)", full_tokens)
    else:
        print("   -- semantic pass SKIPPED: embedding model unavailable (not estimated)")


def bench_recall_gate():
    print("\n== Recall gate ==")
    queries = [
        ("what about the second one?", dict(has_result_set=True, item_count=3)),
        ("good morning", {}),
        ("what snowboard boots do I own?", {}),
        ("what did we decide last month?", {}),
        ("thanks", {}),
    ]
    N = 20_000
    start = time.perf_counter()
    for _ in range(N // len(queries)):
        for q, kw in queries:
            should_recall(q, recent_text="we were talking about drives for the media server",
                          **kw)
    elapsed = time.perf_counter() - start
    per = elapsed / (N // len(queries) * len(queries)) * 1e6
    print(f"   {N} decisions in {elapsed:.3f}s  ->  {per:.1f} microseconds each")

    skipped = sum(1 for q, kw in queries
                  if not should_recall(q, recent_text="drives for the media server", **kw).recall)
    print(f"   skipped {skipped}/{len(queries)} of this sample "
          f"(each skip avoids one MemoryUnifier.search)")


def bench_spoken_text():
    print("\n== Spoken text conversion ==")
    sample = ("## Options\n1. **Seagate Exos** — 28 TB, $429. See "
              "[specs](https://example.com/a/b/c).\n2. **WD Gold** — 26 TB, quieter.\n"
              "```python\nprint('hi')\n```\n")
    out = to_spoken(sample)
    start = time.perf_counter()
    for _ in range(5000):
        to_spoken(sample)
    elapsed = time.perf_counter() - start
    print(f"   input  ({len(sample)} chars): {sample[:56]!r}...")
    print(f"   spoken ({len(out)} chars): {out[:88]!r}...")
    print(f"   5000 conversions in {elapsed:.3f}s ({elapsed / 5000 * 1e6:.1f} us each)")


if __name__ == "__main__":
    print("JARVIS V2 measured benchmarks")
    print("=" * 68)
    bench_first_chunk()
    bench_chunk_quality()
    bench_selector()
    bench_recall_gate()
    bench_spoken_text()
    print("\nNOTE: no live LLM generation and no real XTTS synthesis is measured here.")
    print("See docs/JARVIS_V2_BENCHMARKS.md for what remains unmeasured.")
