"""U4: LLM-driven expression — deterministic *what*, fluid *how*.

The load-bearing property: the model changes only the WORDING. Detection,
ranking, confidence gating and workflow detection stay deterministic, and every
failure falls back to the original template verbatim.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_LLM_EXPRESSION", "1")

from core.expression import Expression
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply, self.raises = reply, raises
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.reply


async def main():
    # ── rephrase: uses the model, but falls back to the template on anything odd ──
    tmpl = "'File the invoice' is overdue — want to handle it now or reschedule it?"
    e = Expression(FakeLLM("That invoice is past due — want to knock it out or push it?"))
    out = await e.rephrase(tmpl)
    check(out != tmpl and "invoice" in out.lower(), f"rephrased naturally (got {out!r})")

    for name, llm in [("model raises", FakeLLM(raises=True)), ("empty reply", FakeLLM(""))]:
        check(await Expression(llm).rephrase(tmpl) == tmpl, f"template kept verbatim when {name}")

    check(await Expression(FakeLLM("Here's a nicer version: do the thing")).rephrase(tmpl) == tmpl,
          "leaked framing ('Here's...') rejected -> template kept")
    check(await Expression(FakeLLM("x" * 500)).rephrase(tmpl) == tmpl, "over-long output rejected")
    check(await Expression(FakeLLM("line one\n\nline two")).rephrase(tmpl) == tmpl,
          "paragraph dump rejected (nudges stay one-liners)")

    # ── flag off: never calls the model ──
    os.environ["NOVA_LLM_EXPRESSION"] = "0"
    off_llm = FakeLLM("rewritten")
    check(await Expression(off_llm).rephrase(tmpl) == tmpl, "flag off -> template used")
    check(off_llm.calls == 0, "flag off -> model never called")
    os.environ["NOVA_LLM_EXPRESSION"] = "1"

    # ── name_for: short names only ──
    steps = ["open_downloads", "rename_file", "move_to_invoices"]
    check(await Expression(FakeLLM("Invoice Filing")).name_for(steps, fallback="fb") == "Invoice Filing",
          "workflow gets a human name")
    check(await Expression(FakeLLM("This is a very long name with far too many words in it")).name_for(
        steps, fallback="fb") == "fb", "over-wordy name rejected -> fallback")
    check(await Expression(None).name_for(steps, fallback="fb") == "fb", "no model -> fallback name")

    # ── read_signal: only accepts an allowed label ──
    r = Expression(FakeLLM("elevated"))
    check(await r.read_signal("been up late, feeling buried", labels=["low", "some", "elevated"],
                              fallback="low") == "elevated", "signal read from evidence")
    check(await Expression(FakeLLM("banana")).read_signal("x", labels=["low", "elevated"],
                                                          fallback="low") == "low",
          "out-of-set label rejected -> fallback")

    # ── Executive: the DECISION is untouched, only wording changes ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        overdue_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        await mem.create_reminder(title="File the invoice", due_at_iso=overdue_at)

        base = await mem.executive_recommendations(throttle=False)
        check(base and base[0]["kind"] == "deadline", "overdue detected (deterministic)")
        base_keys = [r["key"] for r in base]
        base_conf = [r["confidence"] for r in base]

        mem._exec_cache = None
        mem.set_expression(Expression(FakeLLM("That invoice is past due now — want to deal with it?")))
        phrased = await mem.executive_recommendations(throttle=False)

        check([r["key"] for r in phrased] == base_keys, "SAME items surface (detection unchanged)")
        check([r["confidence"] for r in phrased] == base_conf, "SAME confidences (gate unchanged)")
        check(phrased[0]["message"] != base[0]["message"], "only the WORDING changed")
        check(phrased[0]["rationale"] == base[0]["rationale"], "rationale (the why) left factual")

        # a broken model must not break the nudge
        mem._exec_cache = None
        mem.set_expression(Expression(FakeLLM(raises=True)))
        safe = await mem.executive_recommendations(throttle=False)
        check(safe[0]["message"] == base[0]["message"], "broken model -> original template survives")

    # ── Skill detection stays deterministic; only the NAME is model-provided ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        wf = ["open_downloads", "rename_file", "move_to_invoices"]
        for step in (["boot"] + wf + ["email"] + wf + ["chat"] + wf):
            await mem.log_activity(step)

        plain = await mem.detect_learnable_workflow(min_repeats=3)
        check(plain and plain["steps"] == wf, "workflow detected deterministically")
        check("suggested_name" not in plain, "no name proposed without a model")

        mem.set_expression(Expression(FakeLLM("Invoice Filing")))
        named = await mem.detect_learnable_workflow(min_repeats=3)
        check(named["steps"] == wf and named["occurrences"] == plain["occurrences"],
              "detection identical with a model wired in")
        check(named.get("suggested_name") == "Invoice Filing", "model proposes a human workflow name")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
