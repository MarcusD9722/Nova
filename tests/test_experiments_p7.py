"""Phase 7 / #15: autonomous experimentation.

The comparison engine ranks variants and RECOMMENDS — it never applies a change,
and declines on thin data or a slim margin. Pure engine + the facts-based store.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_EXPERIMENTS", "1")

from core.experiments import compare_variants
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def trials(variant, metrics_list):
    return [{"variant": variant, "metrics": m} for m in metrics_list]


def main():
    # ── A clearly better variant is recommended (and always needs approval) ──
    data = (
        trials("A", [{"accuracy": 0.9, "latency_s": 1.0}] * 4)
        + trials("B", [{"accuracy": 0.6, "latency_s": 2.0}] * 4)
    )
    res = compare_variants(data)
    check(res["verdict"] == "adopt", f"a clearly better variant is adopted (got {res['verdict']})")
    check(res["winner"] == "A", "the higher-accuracy/lower-latency variant wins")
    check(res["requires_approval"] is True, "recommendation ALWAYS requires approval (never auto-applies)")
    check(res["ranking"][0]["variant"] == "A", "winner ranks first")
    check(0.0 <= res["confidence"] <= 1.0, "confidence in range")

    # ── lower-better metric handled correctly ──
    lat = trials("fast", [{"latency_s": 0.5}] * 4) + trials("slow", [{"latency_s": 3.0}] * 4)
    check(compare_variants(lat)["winner"] == "fast", "lower latency wins (lower-better inverted)")

    # ── thin data -> inconclusive (never nudges on noise) ──
    thin = trials("A", [{"accuracy": 0.9}]) + trials("B", [{"accuracy": 0.5}])
    r_thin = compare_variants(thin, min_samples=3)
    check(r_thin["verdict"] == "inconclusive", "too few samples -> inconclusive")
    check(r_thin["winner"] is None, "no winner declared on thin data")

    # ── slim margin -> inconclusive ──
    close = trials("A", [{"accuracy": 0.80}] * 5) + trials("B", [{"accuracy": 0.79}] * 5)
    check(compare_variants(close, min_margin=0.1)["verdict"] == "inconclusive", "slim margin -> keep current approach")

    # ── one variant -> can't compare ──
    check(compare_variants(trials("only", [{"accuracy": 1.0}] * 5))["verdict"] == "inconclusive",
          "a single variant can't be compared")

    # ── Facts-based store round-trip ──
    async def _store():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            mem = MemoryUnifier(Path(td), enable_chroma=False)
            await mem.initialize()
            exp = await mem.record_experiment("retrieval tweak", "does term expansion help recall?")
            check(bool(exp), "experiment recorded, id returned")
            for _ in range(4):
                await mem.add_experiment_trial(exp, "expanded", {"accuracy": 0.85, "latency_s": 1.2})
                await mem.add_experiment_trial(exp, "baseline", {"accuracy": 0.7, "latency_s": 1.0})
            analysis = await mem.analyze_experiment(exp)
            check(analysis["verdict"] in ("adopt", "inconclusive"), "stored trials analyze")
            check(analysis["requires_approval"] is True, "stored analysis still requires approval")
            listed = await mem.list_experiments()
            check(listed and listed[0]["trials"] == 8, f"experiment lists trial count (got {listed[0]['trials'] if listed else None})")

            # flag off -> refuses to record
            os.environ["NOVA_EXPERIMENTS"] = "0"
            check(await mem.record_experiment("x") is None, "flag off -> experiments disabled")
            os.environ["NOVA_EXPERIMENTS"] = "1"

    asyncio.run(_store())

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
