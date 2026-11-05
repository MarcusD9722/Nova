"""Audit 2026-11-02, the Medium and Low findings.

M2  plugin read tools were not retry-safe, so one 404 fenced `web.fetch` for
    the whole turn and a second URL was never tried.
M4  `memory.index_folder` counted unchanged files against its cap, so "call
    again to continue" re-scanned the same prefix forever and never advanced.
M5  birthday reminders fired at 3 AM local and said "in 3 day(s)" on the day
    itself, and a missed one was dropped by the `__nova_` filter.
M6  recurrence added 24 h in UTC, so a daily 8am became 7am permanently at the
    DST change on 1 November 2026 — the morning briefing with it.
M7  `log_activity` had no production caller, so skill learning had no input.
M8  no duplicate-call guard: `memory.recall` with one query ran six times.
M10 the fast path's prefilled think block rendered as a FINISHED assistant
    turn plus a new one, not a continuation, despite the comment saying so.
M11 `child="Mateo|Liam"` was stored as one value, losing Liam.
M12 66 failure-named events logged at debug while production runs at INFO.
M13 UTF-16 was tried before latin-1, so a cp1252 file decoded to CJK garbage.
M14 the code index blocked the event loop for 24 s.
M15 `impact_of` said "none" for every method; a project under a folder named
    `build` indexed zero files; the 600-file cap was silent; `latest.py`
    counted as a test.
M17 habit hours and mood/wellbeing days were read off UTC.
M18 `memory.remember` wrote to `note.*` while the extractor wrote `user.*`, so
    only one of them superseded and both came back on recall.
M20 `.env` was loaded after the imports that read it.

L1  "thur" meant Friday.  L2 "day before yesterday" was unreachable.
L4  MCP children were orphaned at shutdown and their stderr pipe filled.
L6  `active_project` had no writer, so every episode had project=None.
L7  the support tables named tools that do not exist, and "the second
    one" could not resolve after an email or calendar listing.
L8  "When's our anniversary?" answered "I don't have our on file".
L10 a read on a fresh speaker database 500'd.
L12 the TTS phrase cache was unbounded.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()


# ── M13: file decoding ───────────────────────────────────────────────────────

def test_a_windows_text_file_is_not_decoded_as_chinese() -> None:
    check.section("M13: cp1252 text does not come back as CJK")
    from core.file_extract import decode_text_bytes

    for label, text in (
        ("a name with an umlaut", "Zoë Sanchez, manager"),
        ("a name with an accent", "Zoë Sanchez, manager"),
        ("smart quotes", "He said “hello” to me"),
        ("an em dash", "the plan — such as it is — works"),
        ("a degree sign", "It was 72° today!"),
        ("a euro sign", "That costs €40 now"),
    ):
        try:
            raw = text.encode("cp1252")
        except UnicodeEncodeError:
            continue
        got = decode_text_bytes(raw)
        check(got == text, f"{label}: {got!r}")
        # The actual symptom: fluent-looking CJK out of an English file.
        check(got is not None and not any("一" <= ch <= "鿿" for ch in got),
              f"{label}: no CJK in the result")

    # utf-8 still wins, and a BOM is still stripped.
    check(decode_text_bytes("hi \U0001f600".encode()) == "hi \U0001f600",
          "utf-8 with an emoji is unchanged")
    check(decode_text_bytes("plain".encode("utf-8-sig")) == "plain",
          "a utf-8 BOM is stripped")
    # Real UTF-16 is still read — but only with a BOM to prove it.
    check(decode_text_bytes("genuine utf16".encode("utf-16")) == "genuine utf16",
          "UTF-16 with a BOM still decodes")
    bom_less = "ascii only".encode("utf-16-le")
    check(decode_text_bytes(bom_less) != "ascii only",
          "and BOM-less UTF-16 is NOT guessed (that guess is the bug)")


# ── M11: one person per fact ────────────────────────────────────────────────

def test_two_children_are_two_facts() -> None:
    check.section("M11: a value naming two people becomes two facts")
    from core.policy.contracts import split_people_value as split

    for attribute, value, want in (
        ("child", "Mateo|Liam", ["Mateo", "Liam"]),
        ("child", "Mateo and Liam", ["Mateo", "Liam"]),
        ("child", "Mateo, Liam, and Ava", ["Mateo", "Liam", "Ava"]),
        ("sibling", "Dana|Rick", ["Dana", "Rick"]),
        ("friend", "Sam & Dave", ["Sam", "Dave"]),
        ("child", "Mateo", ["Mateo"]),
        # `pet` uses "|" for name|species, so splitting would invent a pet.
        ("pet", "Mochi|cat", ["Mochi|cat"]),
        # Descriptive values stay whole.
        ("appearance", "tall, red hair", ["tall, red hair"]),
        ("job", "Zoe Sanchez, manager", ["Zoe Sanchez, manager"]),
        ("location", "Austin, Texas", ["Austin, Texas"]),
    ):
        got = split(attribute, value)
        check(got == want, f"{attribute}={value!r} -> {got}")


# ── M4: indexing a folder makes progress ────────────────────────────────────

async def test_indexing_a_folder_advances() -> None:
    check.section("M4: 'call again to continue' actually continues")
    async with boot() as nova:
        folder = Path(tempfile.mkdtemp(prefix="nova-m4-"))
        for i in range(25):
            (folder / f"doc{i:02d}.txt").write_text(
                f"Document number {i} about the mortgage paperwork.",
                encoding="utf-8")

        router = nova.runtime._router
        from core.tool_router import ToolCall

        seen: set[str] = set()
        rounds = []
        for call_no in range(4):
            res = await router.execute(ToolCall(
                name="memory.index_folder",
                args={"path": str(folder), "max_files": 10}))
            body = res.result if isinstance(res.result, dict) else {}
            rounds.append((body.get("indexed"), body.get("skipped_unchanged"),
                           body.get("more_files_remaining")))
            if body.get("indexed") == 0 and call_no > 0 and not body.get("more_files_remaining"):
                break

        indexed_total = sum(r[0] or 0 for r in rounds)
        check(indexed_total >= 25,
              f"all 25 files were indexed across the calls ({rounds})")
        check(rounds[1][0] not in (0, None),
              f"the SECOND call indexed new files rather than re-scanning "
              f"({rounds[1]})")
        check(not rounds[-1][2],
              f"and the last call reports nothing left ({rounds[-1]})")
        del seen


# ── M5 + M6: reminders and the clock ────────────────────────────────────────

def test_a_daily_reminder_keeps_its_hour_across_dst() -> None:
    check.section("M6: a daily 8am stays 8am through the DST change")
    from core.workers.reminder_worker import (ReminderWorker, _local_to_utc,
                                              _local_wall_clock)

    # 30 October 2026 is before the US change (1 November); step over it.
    due = _local_to_utc(datetime(2026, 10, 30, 8, 0))
    hours = []
    for _ in range(6):
        hours.append(_local_wall_clock(due).strftime("%a %H:%M"))
        due = ReminderWorker._advance(due.isoformat(), "daily")
    check(all(h.endswith("08:00") for h in hours),
          f"every occurrence is at 08:00 local ({hours})")

    due = _local_to_utc(datetime(2026, 10, 29, 8, 0))
    weekly = []
    for _ in range(4):
        weekly.append(_local_wall_clock(due).strftime("%a %H:%M"))
        due = ReminderWorker._advance(due.isoformat(), "weekly")
    check(all(h.endswith("08:00") for h in weekly) and all("Thu" in h for h in weekly),
          f"weekly keeps both the day and the hour ({weekly})")

    # A weekday series skips the weekend and still keeps its hour.
    due = _local_to_utc(datetime(2026, 10, 30, 7, 0))  # a Friday
    wd = []
    for _ in range(4):
        due = ReminderWorker._advance(due.isoformat(), "weekday")
        wd.append(_local_wall_clock(due).strftime("%a %H:%M"))
    check(all(h.endswith("07:00") for h in wd)
          and not any(h.startswith(("Sat", "Sun")) for h in wd),
          f"weekday series: no weekend, hour kept ({wd})")


def test_a_birthday_heads_up_says_what_is_true_now() -> None:
    check.section("M5: the wording is decided when it fires")
    from core.workers.reminder_worker import ReminderWorker as W

    now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    payload = "__nova_dated__:{}|Leslie|birthday"
    check(W._render_dated_details(payload.format("2026-11-04"), now)
          == "Leslie's birthday is today.", "on the day: today")
    check("tomorrow" in W._render_dated_details(payload.format("2026-11-05"), now),
          "the day before: tomorrow")
    check("in 3 day(s)" in W._render_dated_details(payload.format("2026-11-07"), now),
          "three days out: in 3 day(s)")
    check("was on" in W._render_dated_details(payload.format("2026-11-01"), now),
          "already past: was on")
    check(W._render_dated_details("Take the bins out", now) == "Take the bins out",
          "an ordinary reminder is unchanged")

    # The 3 AM firing: 9am LOCAL, converted, not 9am UTC read as local.
    from core.workers.reminder_worker import _local_to_utc, _local_wall_clock
    due = _local_to_utc(datetime.fromisoformat("2026-11-07").replace(hour=9))
    check(_local_wall_clock(due).hour == 9,
          f"a birthday heads-up is due at 9am local "
          f"({_local_wall_clock(due).strftime('%H:%M')})")


# ── M17: hours and days are Marcus's ────────────────────────────────────────

async def test_a_habit_is_labelled_in_local_time() -> None:
    check.section("M17: an 8am habit is reported as 8am")
    async with boot() as nova:
        mem = nova.memory
        # Six days of a tool used at 08:15 LOCAL.
        from core.workers.reminder_worker import _local_to_utc
        for day in range(6):
            stamp = _local_to_utc(datetime(2026, 10, 20 + day, 8, 15))
            await mem.log_tool_usage("weather.current",
                                     called_at_iso=stamp.isoformat())
        habit = await mem.detect_habit("weather.current", window_days=60,
                                       min_distinct_days=4)
        check(habit is not None, f"the pattern is found ({habit})")
        if habit:
            check(habit["hour"] == 8,
                  f"and the hour is 8, not the UTC hour ({habit['hour']})")


# ── M7: the skill learner has an input ──────────────────────────────────────

async def test_tool_calls_reach_the_activity_log() -> None:
    check.section("M7: repeated work becomes a detectable workflow")
    async with boot() as nova:
        mem = nova.memory
        before = await mem.recent_activity(limit=50)
        await nova.runtime._log_activity("weather.current")
        await nova.runtime._log_activity("memory.recall")
        after = await mem.recent_activity(limit=50)
        check(len(after) == len(before) + 2,
              f"a tool call is recorded ({len(before)} -> {len(after)})")
        check(after[-2:] == ["weather.current", "memory.recall"],
              f"in order ({after[-2:]})")

        # …and the executor is wired to it, which is what was missing.
        check(nova.runtime._tool_loop._activity_log is not None,
              "the tool loop has somewhere to log to")

        # Three repeats of a two-step workflow is a candidate.
        for _ in range(3):
            await nova.runtime._log_activity("web.search")
            await nova.runtime._log_activity("web.fetch")
        found = await mem.detect_learnable_workflow(min_repeats=3)
        check(found is not None and found["steps"] == ["web.search", "web.fetch"],
              f"the repeated workflow is detected ({found})")


# ── M10: the fast path's prompt ─────────────────────────────────────────────

def test_the_fast_path_uses_the_templates_own_switch() -> None:
    """The prefill rendered as a finished turn plus a new one. Measured."""
    check.section("M10: thinking is turned off at the template, not faked")
    import inspect

    from core import llm_runtime

    check(hasattr(llm_runtime, "_install_thinking_switch"),
          "there is a handler that sets enable_thinking")
    src = inspect.getsource(llm_runtime._install_thinking_switch)
    check("enable_thinking" in src,
          "and it passes the template's own flag")
    check("_install_thinking_switch(llama)" in inspect.getsource(
        llm_runtime.LLMRuntime._load_llama_capturing_logs
        if hasattr(llm_runtime.LLMRuntime, "_load_llama_capturing_logs")
        else llm_runtime),
        "installed on the model at load")

    # Render the real template both ways, without loading any weights.
    gguf = REPO / "model" / "Qwen3.5-9B-Q6_K.gguf"
    if not gguf.exists():
        check(False, f"the model file is not here, so this cannot be checked "
                     f"({gguf.name})")
        return
    try:
        from llama_cpp import Llama, llama_chat_format as lcf
    except ImportError:
        check(False, "llama_cpp is not importable here")
        return
    llm = Llama(model_path=str(gguf), vocab_only=True, verbose=False)
    template = (llm.metadata or {}).get("tokenizer.chat_template") or ""
    check("enable_thinking" in template,
          "the model's template HAS an enable_thinking switch")

    fmt = lcf.Jinja2ChatFormatter(template=template, eos_token="<|im_end|>",
                                  bos_token="", add_generation_prompt=True)
    msgs = [{"role": "user", "content": "What is 2+2?"}]
    fast = fmt(messages=msgs, enable_thinking=False).prompt
    deep = fmt(messages=msgs).prompt
    check(fast.endswith("<think>\n\n</think>\n\n"),
          f"thinking off: the prompt ENDS with a closed block "
          f"({fast[-30:]!r})")
    check(deep.endswith("<think>\n"),
          f"thinking on: the block is left open ({deep[-20:]!r})")

    # And the old prefill really did render as two turns.
    old = fmt(messages=[*msgs, {"role": "assistant",
                                "content": "<think>\n\n</think>\n\n"}]).prompt
    check(old.count("<|im_start|>assistant") == 2,
          f"the old prefill produced TWO assistant turns "
          f"({old.count('<|im_start|>assistant')})")
    check(old.endswith("<think>\n"),
          "with thinking reopened at the end, so it was never off")
    check(fast.count("<|im_start|>assistant") == 1,
          "the fix produces one")


def test_the_eos_token_survives_detokenisation() -> None:
    """A control token detokenizes to b'' — and an empty stop halts everything.

    Found by a LIVE RUN, after the M10 fix was committed and the test above was
    green. That test renders the template with a HARDCODED
    `eos_token="<|im_end|>"`, so it never touched the one line that resolves
    that token from model metadata — and that line is where the bug was.

    `detokenize([eos_id])` returns EMPTY BYTES for a control token. That is
    llama.cpp's normal behaviour, not an error, and it does not raise, so a
    guard that only caught exceptions handed `eos_token=""` to
    `Jinja2ChatFormatter`. The formatter puts its eos into the stop list, and
    an EMPTY STOP SEQUENCE matches at position 0: one token generated, no text,
    on every single call. Nova was mute on every chat request while all 195
    suites were green, because every one of them substitutes `ScriptedLLM`.

    So this asserts the stop list the switch actually produces — once against a
    fake that detokenizes the way llama.cpp really does, and once against the
    real model's metadata, which `vocab_only=True` reads with no VRAM.
    """
    check.section("M10: the eos the switch resolves is not an empty string")
    from core import llm_runtime

    try:
        from llama_cpp import Llama, llama_chat_format as lcf
    except ImportError:
        check(False, "llama_cpp is not importable here")
        return

    TEMPLATE = (
        "{%- for m in messages %}<|im_start|>{{ m['role'] }}\n"
        "{{ m['content'] }}<|im_end|>\n{%- endfor %}"
        "{%- if add_generation_prompt %}<|im_start|>assistant\n"
        "{%- if enable_thinking is defined and enable_thinking is false %}"
        "{{- '<think>\n\n</think>\n\n' }}{%- else %}{{- '<think>\n' }}"
        "{%- endif %}{%- endif %}"
    )

    class FakeLlama:
        """`detokenize` exactly as llama.cpp does it for a control token."""

        metadata = {"tokenizer.chat_template": TEMPLATE,
                    "tokenizer.ggml.eos_token_id": "248046"}
        chat_handler = None

        def detokenize(self, tokens, prev_tokens=None, special=False):
            return b"<|im_end|>" if special else b""

    captured: dict = {}
    real_wrap = lcf.chat_formatter_to_chat_completion_handler

    def _spy(formatter):
        # The handler the switch installs is a COMPLETION handler, so the only
        # way to see the stop list without generating anything is to catch the
        # formatter on its way in.
        captured["formatter"] = formatter
        return real_wrap(formatter)

    def _install(llama_obj) -> bool:
        captured.clear()
        lcf.chat_formatter_to_chat_completion_handler = _spy
        try:
            return llm_runtime._install_thinking_switch(llama_obj)
        finally:
            lcf.chat_formatter_to_chat_completion_handler = real_wrap

    def _stops(label: str) -> list[str]:
        fmt = captured.get("formatter")
        if fmt is None:
            check(False, f"{label}: the formatter never reached the handler")
            return []
        return list(fmt(messages=[{"role": "user", "content": "hi"}]).stop or [])

    check(_install(FakeLlama()), "the switch installs on a control-token vocab")
    stops = _stops("fake")
    check("" not in stops,
          f"no EMPTY stop sequence — it would match at position 0 and end "
          f"every generation after one token ({stops!r})")
    check(any("im_end" in s for s in stops),
          f"the real end token is what stops it ({stops!r})")

    # The same thing against the real model, which is where it was found.
    gguf = REPO / "model" / "Qwen3.5-9B-Q6_K.gguf"
    if not gguf.exists():
        check(False, f"the model file is not here, so the real vocab cannot "
                     f"be checked ({gguf.name})")
        return
    real_llm = Llama(model_path=str(gguf), vocab_only=True, verbose=False)
    eos_id = (real_llm.metadata or {}).get("tokenizer.ggml.eos_token_id")
    check(real_llm.detokenize([int(eos_id)]) == b"",
          "this model's eos really does detokenize to nothing, which is the "
          "condition that caused it")
    check(_install(real_llm), "the switch installs on the real model")
    stops = _stops("real")
    check("" not in stops,
          f"and its stop list is not an empty string either ({stops!r})")


# ── M15: the code index answers correctly ───────────────────────────────────

def test_the_code_index_tells_the_truth() -> None:
    check.section("M15: methods, test files, skip paths, truncation")
    from core.code_intel import _is_test, impact_of, index_project

    # A project that happens to live under a folder named `build`.
    for parent in ("build", "out", "env", "target", "dist"):
        base = Path(tempfile.mkdtemp(prefix="nova-m15-")) / parent / "myapp"
        base.mkdir(parents=True)
        (base / "main.py").write_text("def go():\n    return 1\n", encoding="utf-8")
        got = index_project(base)["stats"]["files"]
        check(got == 1, f"a project under a parent named {parent!r} indexes "
                        f"its files ({got})")

    # Methods are symbols, so impact analysis works on them.
    proj = Path(tempfile.mkdtemp(prefix="nova-m15b-")) / "p"
    proj.mkdir(parents=True)
    (proj / "store.py").write_text(
        "class Store:\n"
        "    def save(self, x):\n        return x\n"
        "    def load(self):\n        return 1\n", encoding="utf-8")
    (proj / "app.py").write_text(
        "from store import Store\n"
        "def main():\n"
        "    s = Store()\n"
        "    s.save(1)\n", encoding="utf-8")
    idx = index_project(proj)
    imp = impact_of(idx, "save")
    check(imp["defined_in"] == ["store.py"],
          f"a method is in the symbol table ({imp['defined_in']})")
    check(imp["impact"] != "none",
          f"and its impact is not 'none' ({imp['impact']})")
    check([r["file"] for r in imp["referenced_in"]] == ["app.py"],
          f"the file that calls it is named ({imp['referenced_in']})")
    check(impact_of(idx, "Store.save")["defined_in"] == ["store.py"],
          "Class.method resolves too")

    # Test detection by shape, not by the letters t-e-s-t.
    for path, want in (("core/latest.py", False), ("core/contest.py", False),
                       ("src/fastest.ts", False), ("tests/test_x.py", True),
                       ("tests/harness.py", True), ("src/a.test.js", True),
                       ("x_test.go", True), ("spec/thing.py", True),
                       ("core/runtime.py", False)):
        check(_is_test(path) == want, f"_is_test({path!r}) == {want}")

    # Truncation is reported.
    many = Path(tempfile.mkdtemp(prefix="nova-m15c-")) / "p"
    many.mkdir(parents=True)
    for i in range(12):
        (many / f"m{i:02d}.py").write_text("x = 1\n", encoding="utf-8")
    idx = index_project(many, max_files=5)
    check(idx["truncated"] is True, "the cap being hit is reported")
    check("5 of 12" in idx["truncation_note"],
          f"with the real totals ({idx['truncation_note']!r})")
    check(index_project(many, max_files=50)["truncated"] is False,
          "and a complete index is not marked truncated")


# ── M20: .env is loaded before the settings that read it ────────────────────

def test_dotenv_is_loaded_before_the_core_imports() -> None:
    check.section("M20: .env reaches the settings read at import time")
    src = (REPO / "backend" / "app.py").read_text(encoding="utf-8")
    load_at = src.find("_load_dotenv(")
    first_core_import = min(
        (src.find(f"\nfrom {mod}") for mod in ("core.", "memory.", "plugins.")
         if src.find(f"\nfrom {mod}") != -1), default=-1)
    check(load_at != -1 and first_core_import != -1,
          "both the load and the core imports are there")
    check(load_at < first_core_import,
          f"and .env is loaded FIRST (load at {load_at}, "
          f"first core import at {first_core_import})")
    # The six settings the finding named are read at module scope, so this
    # ordering is the only thing that makes them configurable.
    for mod, name in (("core/project_builder.py", "NOVA_PROJECT_FILE_TOKENS"),
                      ("core/tools/selector.py", "NOVA_TOOL_CANDIDATES")):
        text = (REPO / mod).read_text(encoding="utf-8")
        check(name in text, f"{name} is still read at import in {mod}")


# ── M12: real failures are visible at INFO ──────────────────────────────────

def test_real_failures_log_above_debug() -> None:
    check.section("M12: a silent failure is not a hidden one")
    import re as _re

    pattern = _re.compile(
        r'logger\.debug\("([a-z_]*(?:failed|invalid|error|unavailable|timeout))"')
    allowed = {
        "error_log_load_failed", "permission_audit_read_failed",
        "vision_close_failed",
    }
    offenders: list[str] = []
    for folder in ("core", "memory", "backend", "plugins"):
        for path in (REPO / folder).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            for m in pattern.finditer(path.read_text(encoding="utf-8",
                                                     errors="replace")):
                if m.group(1) not in allowed:
                    offenders.append(f"{path.relative_to(REPO)}:{m.group(1)}")
    check(not offenders,
          f"no failure event is hidden at debug ({offenders[:5]})")
    # The three the finding named by name.
    for mod, event in (("core/policy/summarizer.py", "summarizer_invalid"),
                       ("core/orchestrator/agent.py", "tool_decider_failed"),
                       ("core/understanding.py", "understanding_call_failed")):
        text = (REPO / mod).read_text(encoding="utf-8")
        check(f'logger.warning("{event}"' in text,
              f"{event} logs at warning")


async def test_a_remembered_attribute_supersedes_itself() -> None:
    """Two namespaces for one fact meant one of them never went away."""
    check.section("M18: 'remember I live in X' is superseded by a move")
    async with boot() as nova:
        from core.tool_router import ToolCall

        router = nova.runtime._router
        stored = []
        for place in ("Dallas", "Austin"):
            res = await router.execute(ToolCall(
                name="memory.remember",
                args={"fact": f"I live in {place}", "topic": "location"}))
            body = res.result if isinstance(res.result, dict) else {}
            stored.append(body.get("stored_as"))
        check(stored == ["user.location", "user.location"],
              f"an attribute-shaped topic is stored as an attribute ({stored})")

        rows = [f.value for f in await nova.memory.get_facts(
            entity="user", attribute="location", limit=10)]
        check(rows == ["I live in Austin"],
              f"only the current one survives ({rows})")
        notes = [f.value for f in await nova.memory.get_facts(
            entity="note", attribute="location", limit=10)]
        check(not notes, f"and nothing is left in the note namespace ({notes})")

        hits = await nova.memory.search(q="where do I live", limit=8)
        check(not any("Dallas" in h.text for h in hits),
              "recall does not offer the old city alongside the new one")

        # A topic that is NOT an attribute still goes to notes, where several
        # values are legitimate.
        for thing in ("the spare key is under the mat",
                      "the gate code is 4471"):
            await router.execute(ToolCall(name="memory.remember",
                                          args={"fact": thing, "topic": "house"}))
        house = [f.value for f in await nova.memory.get_facts(
            entity="note", attribute="house", limit=10)]
        check(len(house) == 2,
              f"a free-form note still accumulates ({len(house)})")


# ── The Low findings ────────────────────────────────────────────────────────

def test_the_low_date_phrases() -> None:
    check.section("L1 + L2: 'thur' and 'the day before yesterday'")
    from core.dates import parse_date_range, parse_reminder_time

    now = datetime(2026, 11, 4, 15, 0)   # a Wednesday
    # L1: "thur" was mapped to 4 (Friday).
    parsed = parse_reminder_time("remind me every thur at 8am", now=now)
    check(parsed is not None and parsed[0].astimezone().strftime("%a") == "Thu",
          f"'every thur' is Thursday "
          f"({parsed[0].astimezone().strftime('%a %H:%M') if parsed else None})")
    for word, weekday in (("thu", "Thu"), ("thurs", "Thu"), ("tues", "Tue"),
                          ("wed", "Wed"), ("fri", "Fri")):
        p = parse_reminder_time(f"remind me every {word} at 8am", now=now)
        check(p is not None and p[0].astimezone().strftime("%a") == weekday,
              f"'every {word}' -> {p[0].astimezone().strftime('%a') if p else None}")

    # L2: "day before yesterday" was unreachable behind "yesterday".
    r = parse_date_range("what did we do the day before yesterday", now=now)
    check(r is not None and r[0].strftime("%Y-%m-%d") == "2026-11-02",
          f"the day before yesterday is Monday the 2nd "
          f"({r[0].strftime('%a %Y-%m-%d') if r else None})")
    r = parse_date_range("what did we do yesterday", now=now)
    check(r is not None and r[0].strftime("%Y-%m-%d") == "2026-11-03",
          "and plain 'yesterday' is still Tuesday the 3rd")


def test_our_anniversary_is_a_question_she_can_answer() -> None:
    check.section("L8: 'When's our anniversary?'")
    from core.personal_dates import parse_date_query

    for text, attribute in (("When's our anniversary?", "anniversary"),
                            ("when's our wedding anniversary?", "anniversary"),
                            ("when is our next anniversary", "anniversary"),
                            ("when is our 10th anniversary", "anniversary"),
                            ("when is my birthday", "birthday"),
                            ("when is my next birthday", "birthday")):
        q = parse_date_query(text)
        check(q is not None and q.attribute == attribute and q.subjects == [""],
              f"{text!r} -> the SPEAKER's {attribute} "
              f"({q.subjects if q else None})")
    # Somebody else's still resolves to them.
    q = parse_date_query("when is Leslie's birthday")
    check(q is not None and q.subjects == ["Leslie"],
          f"a named person is unaffected ({q.subjects if q else None})")


def test_the_tool_names_in_the_support_tables_exist() -> None:
    check.section("L7: no table refers to a tool that does not exist")
    from core.tools.selector import _RULES
    from memory.artifacts import _EXTERNAL_TOOLS, _TOOL_FRESHNESS, result_items

    gone = {"time.now", "gmail.list", "gmail.read", "calendar.list",
            "weather.forecast"}
    named = set(_TOOL_FRESHNESS) | set(_EXTERNAL_TOOLS)
    for rx, tools in _RULES:
        named |= set(tools)
    check(not (named & gone),
          f"no stale name is referenced ({sorted(named & gone)})")
    check("system.time" in _TOOL_FRESHNESS and "email.recent" in _TOOL_FRESHNESS,
          "the real names are there instead")
    # "the second one" can resolve after the tools where it matters most.
    for key in ("emails", "events", "messages", "reminders"):
        items = result_items({key: [{"id": 1}, {"id": 2}]})
        check(len(items) == 2, f"a list under {key!r} resolves ({len(items)})")


def test_an_mcp_child_is_cleaned_up_and_its_stderr_drained() -> None:
    check.section("L4: no orphaned servers, no blocked pipe")
    import inspect

    from backend import app as backend_app
    from core.mcp import session as mcp_session

    shutdown = inspect.getsource(backend_app._shutdown)
    check("STATE.mcp.close()" in shutdown.replace("await ", ""),
          "shutdown closes the MCP manager")
    check("mcp_task" in shutdown,
          "and cancels the startup task if it is still connecting")
    src = inspect.getsource(mcp_session.StdioTransport)
    check("_drain_stderr" in src,
          "the child's stderr is drained rather than left to fill")
    check("self._stderr_task" in src and "task.cancel()" in src,
          "and the drain is stopped on close")


async def test_the_speaker_registry_creates_its_own_schema() -> None:
    """The one real 5xx in the route sweep, on a fresh database."""
    check.section("L10: a fresh database does not 500")
    from core.speaker.registry import SpeakerRegistry

    db = Path(tempfile.mkdtemp(prefix="nova-l10-")) / "sub" / "speaker.sqlite3"
    reg = SpeakerRegistry(db)
    try:
        rows = await reg.all()
        check(rows == [], f"all() works with nothing initialised ({rows})")
        check(await reg.matchable() == [], "matchable() too")
        check(await reg.get("nope") is None, "and get()")
        failed = ""
    except Exception as e:  # noqa: BLE001
        failed = f"{type(e).__name__}: {e}"
        check(False, f"a read on a fresh database raised: {failed}")
    check(not failed, "no 'no such table: speaker_profiles'")


async def test_an_episode_knows_which_project_it_belongs_to() -> None:
    check.section("L6: active_project has a writer")
    async with boot() as nova:
        rt = nova.runtime
        await rt._project_builder.start(name="Serpent", brief="a snake game")
        ctx = rt._working.get("l6-probe")
        await rt._refresh_active_project(ctx, "hello")
        check(ctx.active_project == "serpent",
              f"the pointer is written from last_active ({ctx.active_project!r})")

        # A project named in the message wins over the stored pointer.
        await rt._project_builder.start(name="flappy-bird", brief="a bird game")
        await rt._refresh_active_project(ctx, "how is serpent going")
        check(ctx.active_project == "serpent",
              f"a named project wins ({ctx.active_project!r})")
        check("Active project: serpent" in ctx.grounding_line()
              if hasattr(ctx, "grounding_line") else True,
              "and it reaches the grounding line")


def test_the_phrase_cache_is_bounded() -> None:
    check.section("L12: the TTS phrase cache cannot grow forever")
    import inspect

    from backend import app as backend_app

    src = inspect.getsource(backend_app)
    idx = src.find("STATE.tts_phrase_cache[cache_key] = audio")
    check(idx != -1, "the cache is still written")
    window = src[idx:idx + 400]
    check("_TTS_CACHE_MAX" in window,
          "and bounded right where it is written")


async def main() -> None:
    test_a_windows_text_file_is_not_decoded_as_chinese()
    test_two_children_are_two_facts()
    test_a_daily_reminder_keeps_its_hour_across_dst()
    test_a_birthday_heads_up_says_what_is_true_now()
    test_the_fast_path_uses_the_templates_own_switch()
    test_the_eos_token_survives_detokenisation()
    test_the_code_index_tells_the_truth()
    test_dotenv_is_loaded_before_the_core_imports()
    test_real_failures_log_above_debug()
    await test_indexing_a_folder_advances()
    await test_a_habit_is_labelled_in_local_time()
    await test_tool_calls_reach_the_activity_log()
    await test_a_remembered_attribute_supersedes_itself()
    test_the_low_date_phrases()
    test_our_anniversary_is_a_question_she_can_answer()
    test_the_tool_names_in_the_support_tables_exist()
    test_an_mcp_child_is_cleaned_up_and_its_stderr_drained()
    test_the_phrase_cache_is_bounded()
    await test_the_speaker_registry_creates_its_own_schema()
    await test_an_episode_knows_which_project_it_belongs_to()
    check.finish()


if __name__ == "__main__":
    run(main)
