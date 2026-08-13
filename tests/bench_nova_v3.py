"""V3 P2: the durable Nova performance benchmark.

Ten scenarios covering the paths that actually differ from each other — fast
path, memory, tools, follow-up, multi-tool, artifacts, reasoning, project
recall, freshness, and a long streamed reply. Run it before and after any change
that could plausibly touch latency.

Real components throughout: the real LLMRuntime on the real GGUF, the real
MemoryUnifier, the real recall gate, the real ToolSelector, the real isolated
XTTS worker. Memory lives in a temp directory so a benchmark run never pollutes
Marcus's actual memory.

WHAT THIS CANNOT MEASURE
Everything upstream of the transcript needs a microphone and a browser: VAD
onset, endpoint latency, upload transport, and playback start. Those are
reported as NOT MEASURED rather than estimated. The V3 brief is explicit that
fabricating them is worse than omitting them.

Run:  venv\\Scripts\\python.exe tests\\bench_nova_v3.py [--no-tts]
"""

import asyncio
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_TTS_DEVICE", "auto")

REPO = Path(__file__).resolve().parent.parent
VOICE = REPO / "voices" / "nova.wav"

SYSTEM = (
    "You are Nova — Marcus's AI companion and assistant. Talk like a real person. "
    "Keep it conversational — a sentence or a few.\n\n"
    "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
    "any analysis, planning, notes, or a reasoning block — just say your reply directly."
)

# Mirrors the shape of Nova's real registry closely enough that selector
# behaviour is representative rather than a toy.
TOOLS = {
    "time.now": "Get the current date and time.",
    "weather.current": "Current weather conditions for a city or location.",
    "weather.forecast": "Weather forecast for the coming days at a location.",
    "maps.directions": "Driving directions, distance and travel time between two places.",
    "maps.places_nearby": "Find places near a location, such as restaurants or shops.",
    "web.search": "Search the web for current information and news.",
    "web.fetch": "Fetch and read the contents of a web page URL.",
    "memory.remember": "Save a fact, preference or detail to long-term memory.",
    "memory.recall": "Look up something previously remembered about the user.",
    "memory.correct": "Correct a previously stored fact that turned out to be wrong.",
    "reminder.create": "Create a reminder or timer for a future time.",
    "project.improve": "Make a change to an existing project in the projects folder.",
    "project.status": "Report the status of a project build.",
    "image.generate": "Generate an image from a text description.",
    "self.read_code": "Read Nova's own source code.",
    "calendar.list": "List upcoming calendar events.",
    "gmail.list": "List recent emails from Gmail.",
    "thoughts.recall": "Recall previously recorded thoughts.",
    "plan.status": "Check progress on a saved plan.",
    "world.recall": "Recall general world knowledge previously stored.",
}

DRIVES = [
    {"title": "Seagate Exos X28", "capacity": "28 TB", "price": "$429", "warranty": "5 years"},
    {"title": "WD Gold", "capacity": "26 TB", "price": "$459", "warranty": "5 years"},
    {"title": "IronWolf Pro", "capacity": "24 TB", "price": "$389", "warranty": "3 years"},
]


@dataclass
class Scenario:
    key: str
    prompt: str
    what: str
    has_result_set: bool = False
    speak: bool = False
    # Matches NOVA_MAX_TOKENS in production. A smaller budget is NOT a smaller
    # answer with this model — the hidden think block eats it and the visible
    # reply strips to nothing. Measured: at 512 this benchmark reported 3/10
    # empty replies and 8 wasted generations, which looked like a Nova
    # regression and was purely an artefact of the harness.
    max_tokens: int = 1536


SCENARIOS = [
    Scenario("greeting", "Good morning.", "no-tool fast path"),
    Scenario("memory", "What snowboard boots do I own?", "personal memory retrieval"),
    Scenario("tool", "What's the weather tomorrow?", "native tool selection"),
    Scenario("followup", "What about Saturday?", "contextual follow-up"),
    Scenario("multitool", "Check tomorrow's weather and the traffic for my drive.",
             "multi-tool orchestration"),
    Scenario("ordinal", "What about the second one?", "ordinal artifact reference",
             has_result_set=True),
    Scenario("reasoning", "Why does running two CUDA consumers in one process cause "
                          "an illegal memory access? Two sentences.", "complex reasoning"),
    Scenario("project", "Where did we leave off with my Jellyfin project?",
             "historical project recall"),
    Scenario("freshness", "What's the current price of that drive?",
             "freshness-sensitive request", has_result_set=True),
    Scenario("long_tts", "Explain in about four sentences what RAID 1 does and when to use it.",
             "long reply with streaming TTS", speak=True, max_tokens=2048),
]


@dataclass
class Result:
    key: str
    what: str
    gate_recall: bool | None = None
    gate_reason: str = ""
    memory_ms: float | None = None
    memory_hits: int = 0
    selector_ms: float | None = None
    tools_shown: int | None = None
    selector_stage: str = ""
    artifact_ms: float | None = None
    artifact_hit: str | None = None
    llm_calls: int = 0
    prompt_chars: int = 0
    ttft_ms: float | None = None
    first_chunk_ms: float | None = None
    tts_queue_ms: float | None = None
    tts_first_ms: float | None = None
    total_ms: float = 0.0
    reply_chars: int = 0
    retries: int = 0
    empty: bool = False
    vram_gb: float | None = None
    rss_mb: float | None = None
    errors: list[str] = field(default_factory=list)


def vram():
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        return (total - free) / 1e9
    except Exception:
        return None


def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        return None


def ms(v):
    return "—" if v is None else f"{v:.0f}"


async def main():
    no_tts = "--no-tts" in sys.argv

    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    from core.llm_runtime import LLMRuntime
    from core.tools.selector import ToolSelector
    from core.voice.chunker import SpeechChunker
    from core.voice.speech_text import to_spoken
    from memory.artifacts import ArtifactStore
    from memory.recall_gate import should_recall
    from memory.unifier import MemoryUnifier
    from memory.working_context import WorkingContextStore

    print("Nova V3 P2 — formal performance benchmark")
    print("=" * 100)

    tmp = tempfile.mkdtemp(prefix="nova-v3-bench-")
    print(f"\nBooting real components (memory in {tmp}) ...")

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()

    memory = MemoryUnifier(Path(tmp), enable_chroma=False)
    await memory.initialize()
    # Seed the facts the memory/project scenarios are supposed to find. Without
    # this, "what boots do I own" would measure an empty-database lookup and
    # report a flatteringly fast number for a query that found nothing.
    await memory.add_fact(entity="user", attribute="snowboard_boots",
                          value="Burton Photon, size 10", confidence=0.95)
    await memory.add_fact(entity="user", attribute="boot_size", value="10", confidence=0.95)
    await memory.add_fact(entity="project:jellyfin", attribute="status",
                          value="hardware transcoding configured; next step is remote access",
                          confidence=0.9)

    selector = ToolSelector()
    contexts = WorkingContextStore()
    artifacts = ArtifactStore()
    conv = "bench-v3"
    ctx = contexts.get(conv)

    engine = None
    if not no_tts:
        from services.tts_client import IsolatedTtsEngine
        engine = IsolatedTtsEngine(
            cfg={"device": os.getenv("NOVA_TTS_DEVICE", "auto"), "allow_cpu_fallback": False},
            start_timeout_s=600.0, synth_timeout_s=180.0,
        )
        if not await engine.ensure_started():
            print(f"  XTTS unavailable ({engine.last_error}) — TTS stages will be omitted")
            engine = None
        else:
            print(f"  XTTS ready on {engine.device!r}")

    # Prime the artifact store so the ordinal/freshness scenarios resolve against
    # a real result set rather than nothing.
    parent = artifacts.add_result_set(
        conversation_id=conv, turn_id="seed", summary="three 28 TB-class drives",
        items=DRIVES, source_tool="web.search", query="28 TB NAS drives")
    ctx.set_result_set(parent.artifact_id)
    ctx.record_user("Find me three 28 TB drives for the media server.")
    ctx.record_assistant("Here are three options: the Seagate Exos X28, the WD Gold, "
                         "and the IronWolf Pro.")

    print(f"  model + memory + selector ready   VRAM {vram():.2f} GB\n" if vram()
          else "  ready\n")

    results: list[Result] = []

    for sc in SCENARIOS:
        r = Result(key=sc.key, what=sc.what)
        t_turn = time.perf_counter()
        usage_before = llm.usage_stats

        # ── artifact / ordinal resolution ────────────────────────────────────
        t0 = time.perf_counter()
        items = artifacts.active_items(conv) if sc.has_result_set else []
        hit = artifacts.resolve(sc.prompt, conv) if items else None
        r.artifact_ms = (time.perf_counter() - t0) * 1000
        r.artifact_hit = hit.title if hit else None

        # ── recall gate ──────────────────────────────────────────────────────
        t0 = time.perf_counter()
        gate = should_recall(sc.prompt, recent_text=ctx.recent_text(),
                             has_result_set=bool(items), item_count=len(items))
        gate_ms = (time.perf_counter() - t0) * 1000
        r.gate_recall = gate.recall
        r.gate_reason = gate.reason

        # ── memory retrieval (only when the gate allows it) ──────────────────
        stable_mem = ""
        if gate.recall:
            t0 = time.perf_counter()
            hits = await memory.search(q=sc.prompt, limit=8)
            r.memory_ms = (time.perf_counter() - t0) * 1000
            r.memory_hits = len(hits)
            stable_mem = "\n".join(h.text for h in hits if h.kind != "turn")
        else:
            r.memory_ms = gate_ms   # the gate IS the cost when it skips

        # ── tool selection ───────────────────────────────────────────────────
        t0 = time.perf_counter()
        sel = selector.select(sc.prompt, TOOLS, context=ctx.recent_text()[:400])
        r.selector_ms = (time.perf_counter() - t0) * 1000
        r.tools_shown = len(sel.tools)
        r.selector_stage = sel.stage

        # ── prompt assembly ──────────────────────────────────────────────────
        parts = [SYSTEM]
        if stable_mem:
            parts.append(f"Things you remember:\n{stable_mem}")
        if hit:
            parts.append(f"Marcus is referring to item {hit.item_index}: {hit.title} "
                         f"({hit.payload})")
        if ctx.recent_text():
            parts.append(f"Recent messages:\n{ctx.recent_text()}")
        system_prompt = "\n\n".join(parts)
        r.prompt_chars = len(system_prompt)

        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": sc.prompt}]

        # ── generation + optional streamed TTS ───────────────────────────────
        chunker = SpeechChunker()
        first_spoken: str | None = None
        t_gen = time.perf_counter()
        tokens: list[str] = []
        try:
            async for tok in llm.chat_stream(messages, max_tokens=sc.max_tokens,
                                             temperature=0.4, thinking=True):
                if r.ttft_ms is None:
                    r.ttft_ms = (time.perf_counter() - t_gen) * 1000
                tokens.append(tok)
                if first_spoken is None:
                    for chunk in chunker.feed(tok):
                        spoken = to_spoken(chunk)
                        if spoken:
                            first_spoken = spoken
                            r.first_chunk_ms = (time.perf_counter() - t_gen) * 1000
                            break
        except Exception as e:  # noqa: BLE001
            r.errors.append(f"{type(e).__name__}: {e}")

        if first_spoken is None:
            for chunk in chunker.flush():
                spoken = to_spoken(chunk)
                if spoken:
                    first_spoken = spoken
                    r.first_chunk_ms = (time.perf_counter() - t_gen) * 1000
                    break

        reply = "".join(tokens).strip()
        r.reply_chars = len(reply)
        r.empty = not reply
        r.llm_calls = 1

        if sc.speak and engine is not None and first_spoken:
            t0 = time.perf_counter()
            try:
                wav = await engine.synthesize(first_spoken, speaker_wav=str(VOICE),
                                              turn_id=f"bench-{sc.key}")
                r.tts_queue_ms = 0.0     # sequential here; no queue depth to wait on
                r.tts_first_ms = (time.perf_counter() - t0) * 1000
                _ = len(wav)
            except Exception as e:  # noqa: BLE001
                r.errors.append(f"tts: {type(e).__name__}: {e}")

        r.total_ms = (time.perf_counter() - t_turn) * 1000
        usage_after = llm.usage_stats
        r.retries = int(usage_after["empty_retries"]) - int(usage_before["empty_retries"])
        r.vram_gb = vram()
        r.rss_mb = rss_mb()

        ctx.record_user(sc.prompt)
        if reply:
            ctx.record_assistant(reply)
        results.append(r)
        print(f"  {sc.key:<11} {r.total_ms:>7.0f}ms  ttft {ms(r.ttft_ms):>6}ms  "
              f"tools {r.tools_shown:<3} gate={'R' if r.gate_recall else 'skip'}"
              + (f"  retries {r.retries}" if r.retries else "")
              + ("  EMPTY" if r.empty else ""))

    # ── report ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("PER-SCENARIO")
    print("=" * 100)
    hdr = (f"{'scenario':<11} {'gate':<5} {'mem':>6} {'sel':>6} {'tools':>5} "
           f"{'art':>6} {'ttft':>7} {'chunk':>7} {'tts1':>7} {'total':>8} {'chars':>6} {'try':>4}")
    print(hdr)
    for r in results:
        print(f"{r.key:<11} {'R' if r.gate_recall else 'skip':<5} "
              f"{ms(r.memory_ms):>6} {ms(r.selector_ms):>6} {r.tools_shown:>5} "
              f"{ms(r.artifact_ms):>6} {ms(r.ttft_ms):>7} {ms(r.first_chunk_ms):>7} "
              f"{ms(r.tts_first_ms):>7} {r.total_ms:>8.0f} {r.reply_chars:>6} {r.retries:>4}")

    def agg(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return "—", "—", "—"
        return (f"{statistics.mean(vals):.0f}", f"{statistics.median(vals):.0f}",
                f"{sorted(vals)[min(len(vals) - 1, round(0.9 * (len(vals) - 1)))]:.0f}")

    print("\n" + "=" * 100)
    print("AGGREGATE (ms)")
    print("=" * 100)
    print(f"{'metric':<22} {'mean':>8} {'median':>8} {'P90':>8}")
    for label, vals in [
        ("recall gate + memory", [r.memory_ms for r in results]),
        ("tool selection", [r.selector_ms for r in results]),
        ("artifact resolution", [r.artifact_ms for r in results]),
        ("TTFT", [r.ttft_ms for r in results]),
        ("first speakable chunk", [r.first_chunk_ms for r in results]),
        ("XTTS first chunk", [r.tts_first_ms for r in results]),
        ("total turn", [r.total_ms for r in results]),
    ]:
        m, med, p90 = agg(vals)
        print(f"{label:<22} {m:>8} {med:>8} {p90:>8}")

    tools_shown = [r.tools_shown for r in results if r.tools_shown is not None]
    print(f"\n  tools exposed per turn : mean {statistics.mean(tools_shown):.1f} "
          f"of {len(TOOLS)} registered")
    print(f"  gate skips             : {sum(1 for r in results if r.gate_recall is False)}"
          f"/{len(results)}")
    print(f"  LLM calls per turn     : {statistics.mean([r.llm_calls for r in results]):.1f}")
    print(f"  wasted generations     : {sum(r.retries for r in results)}")
    print(f"  empty replies          : {sum(1 for r in results if r.empty)}/{len(results)}")
    print(f"  errors                 : {sum(len(r.errors) for r in results)}")
    for r in results:
        for e in r.errors[:1]:
            print(f"    {r.key}: {e}")
    if results[-1].vram_gb:
        print(f"  VRAM at end            : {results[-1].vram_gb:.2f} GB")
    if results[-1].rss_mb:
        print(f"  RSS at end             : {results[-1].rss_mb:.0f} MB")

    print("\n" + "=" * 100)
    print("NOT MEASURED — requires a microphone and a live browser session")
    print("=" * 100)
    for line in [
        "VAD / speech-onset latency",
        "endpoint (trailing-silence) latency in a real room",
        "frontend audio finalization and upload transport",
        "backend -> frontend TTS delivery and playback start",
        "end of user speech -> first audible word (the headline metric)",
        "barge-in stop latency (P0: IMPLEMENTED — LIVE ACCEPTANCE PENDING)",
    ]:
        print(f"  - {line}")
    print("\nThese are omitted rather than estimated. See tests/live_barge_in_harness.md")

    if engine is not None:
        await engine.stop()
    try:
        await memory.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
