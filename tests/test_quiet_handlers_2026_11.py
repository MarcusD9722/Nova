"""The quiet handlers layer 1 found, and what each one says now.

Test program layer 2, item 1 (docs/TEST_PROGRAM.md). Layer 1's recorder ran
every suite that reaches an `except` block that neither logs nor re-raises,
and wrote down what each block actually caught. Thirteen of them could let a
real failure vanish. In almost every one the suites only ever hit the
harmless case — a missing file, an empty queue — so nothing proved the bad
case was handled at all. This suite proves it, one handler at a time:

  * the harmless case stays quiet (a missing file is not news);
  * the bad case is now SAID, under its own event name, with the reason;
  * and the behaviour around it is unchanged, or changed on purpose where
    the old behaviour was itself the bug.

Two of the thirteen hid something worse than silence, and both are pinned
here:

  * a `cloud_spend.json` holding valid JSON that is not an object (`null`, a
    list) raised AttributeError out of `CloudRuntime()`. RuntimeManager builds
    that unconditionally, cloud enabled or not, so one bad file stopped Nova
    booting. The boot is run for real below.
  * a promotion journal that could not be read, with no backup beside it,
    was never cleared, and `_unfinished_promotion` refuses while the file
    exists. Every later rebuild of the semantic index was refused, on every
    boot, with a reason that pointed nowhere.

Log events are observed by swapping each module's own `logger` for a
recorder, the same way tests/test_deep_mode_silent_fallbacks.py does: this
project's filtering bound logger can drop a captured event before any
processor sees it, and then a "was it logged" check would pass vacuously.

Run:  venv\\Scripts\\python.exe tests\\test_quiet_handlers_2026_11.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, ScriptedLLM, boot, run  # noqa: E402

import core.cloud_runtime as cloud_mod  # noqa: E402
import core.completion_artifacts as scaffold_mod  # noqa: E402
import core.conversation_state as state_mod  # noqa: E402
import core.project_builder as builder_mod  # noqa: E402
import core.workers.memory_ingest as ingest_mod  # noqa: E402
import memory.backends.chroma_backend as chroma_mod  # noqa: E402
import memory.unifier as unifier_mod  # noqa: E402

check = Checks()


class _Recorder:
    """Stands in for a module's logger and remembers what it was told."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        if level.startswith("_"):
            raise AttributeError(level)

        def fn(event: str, **kw: Any) -> None:
            self.events.append((level, event, kw))
        return fn

    def named(self, event: str) -> list[tuple[str, dict[str, Any]]]:
        return [(lvl, kw) for lvl, name, kw in self.events if name == event]

    def names(self) -> list[str]:
        return [name for _lvl, name, _kw in self.events]


@contextlib.contextmanager
def listening(module):
    rec = _Recorder()
    original = module.logger
    module.logger = rec
    try:
        yield rec
    finally:
        module.logger = original


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"nova-qh-{prefix}-"))


def _err(rec: _Recorder, event: str) -> str:
    got = rec.named(event)
    return str(got[0][1].get("error", "")) if got else ""


# ── 1. the cloud spend file ─────────────────────────────────────────────────

def _cloud_at(path: Path):
    saved = os.environ.get("NOVA_CLOUD_SPEND_PATH")
    os.environ["NOVA_CLOUD_SPEND_PATH"] = str(path)
    try:
        return cloud_mod.CloudRuntime()
    finally:
        if saved is None:
            os.environ.pop("NOVA_CLOUD_SPEND_PATH", None)
        else:
            os.environ["NOVA_CLOUD_SPEND_PATH"] = saved


def _today_counts(cloud) -> tuple[int, int]:
    today = cloud.status()["today"]
    return int(today["prompt_tokens"]), int(today["completion_tokens"])


def test_spend_file() -> None:
    check.section("1. cloud_spend.json: missing is quiet, damage is said, nothing crashes")
    root = _tmp("spend")
    spend = root / "cloud_spend.json"
    today = datetime.now().strftime("%Y-%m-%d")
    # No cap for this section, whatever .env says: a cap below the counts
    # written here would add a `cloud_cap_restored_as_reached` event.
    saved_cap = os.environ.get("NOVA_CLOUD_DAILY_TOKEN_CAP")
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = "0"
    try:
        with listening(cloud_mod) as rec:
            cloud = _cloud_at(spend)
        check(_today_counts(cloud) == (0, 0) and not rec.events,
              "no file yet: counts from zero and says nothing (the normal first run)")

        spend.write_text(json.dumps({"day": today, "prompt_tokens": 120,
                                     "completion_tokens": 30}), encoding="utf-8")
        with listening(cloud_mod) as rec:
            cloud = _cloud_at(spend)
        check(_today_counts(cloud) == (120, 30) and not rec.events,
              "today's file: the counts come back, and nothing is said")

        spend.write_text(json.dumps({"day": "1999-01-01", "prompt_tokens": 9,
                                     "completion_tokens": 9}), encoding="utf-8")
        with listening(cloud_mod) as rec:
            cloud = _cloud_at(spend)
        check(_today_counts(cloud) == (0, 0) and not rec.events,
              "yesterday's file: the day has rolled, zero, nothing said")

        damaged = [
            ("truncated JSON", b'{"day": "20', "JSONDecodeError"),
            ("null", b"null", "found NoneType"),
            ("a list", b"[1, 2]", "found list"),
            ("not UTF-8", b'\xff\xfe{"day": 1}', "UnicodeDecodeError"),
        ]
        for label, raw, expect in damaged:
            spend.write_bytes(raw)
            crashed = ""
            with listening(cloud_mod) as rec:
                try:
                    cloud = _cloud_at(spend)
                except Exception as e:  # noqa: BLE001
                    crashed = f"{type(e).__name__}: {e}"
            check(not crashed, f"{label}: CloudRuntime() is built ({crashed or 'no crash'})")
            if crashed:
                continue
            ev = rec.named("cloud_spend_unreadable")
            check(len(ev) == 1 and expect in str(ev[0][1].get("error"))
                  and ev[0][1].get("path") == str(spend),
                  f"{label}: said once, naming the file and why ({_err(rec, 'cloud_spend_unreadable')[:70]})")
            check(_today_counts(cloud) == (0, 0) and not cloud.status()["today"]["cap_reached"],
                  f"{label}: and counted from zero, as Marcus decided (no fail-closed)")

        # All or nothing: a half-read file is neither the file's count nor zero.
        for label, body in (
            ("prompt count not a number", {"day": today, "prompt_tokens": "lots",
                                           "completion_tokens": 5}),
            ("completion count not a number", {"day": today, "prompt_tokens": 7,
                                               "completion_tokens": "lots"}),
        ):
            spend.write_text(json.dumps(body), encoding="utf-8")
            with listening(cloud_mod) as rec:
                cloud = _cloud_at(spend)
            check(_today_counts(cloud) == (0, 0),
                  f"{label}: both counts are zero, not half of them ({_today_counts(cloud)})")
            check("not numbers" in _err(rec, "cloud_spend_unreadable"),
                  f"{label}: and it is said")
    finally:
        if saved_cap is None:
            os.environ.pop("NOVA_CLOUD_DAILY_TOKEN_CAP", None)
        else:
            os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = saved_cap
        shutil.rmtree(root, ignore_errors=True)


async def test_boot_with_a_bad_spend_file() -> None:
    check.section("1b. Nova boots with a cloud_spend.json holding null")
    root = _tmp("boot")
    (root / "memory_data").mkdir(parents=True, exist_ok=True)
    spend = root / "memory_data" / "cloud_spend.json"
    spend.write_text("null", encoding="utf-8")
    booted, failure = False, ""
    try:
        with listening(cloud_mod) as rec:
            try:
                async with boot(root=root, env={"NOVA_CLOUD_SPEND_PATH": str(spend)}) as nova:
                    booted = nova.runtime is not None and nova.brain is not None
            except Exception as e:  # noqa: BLE001
                failure = f"{type(e).__name__}: {str(e)[:120]}"
        check(booted, f"the real startup completes ({failure or 'booted'})")
        check("found NoneType" in _err(rec, "cloud_spend_unreadable"),
              "and the bad file is reported while it boots")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 2. the scaffold manifest ────────────────────────────────────────────────

def test_scaffold_manifest() -> None:
    check.section("2. .nova/scaffold.json: unreadable is said, never raises, never lost silently")
    from core.completion_artifacts import declare_scaffold, declared_scaffold, implementation_files

    root = _tmp("scaffold")
    try:
        proj = root / "p1"
        proj.mkdir()
        (proj / "main.py").write_text("print(1)\n", encoding="utf-8")
        (proj / "nova_check.py").write_text("x = 1\n", encoding="utf-8")
        manifest = proj / ".nova" / "scaffold.json"

        with listening(scaffold_mod) as rec:
            check(declared_scaffold(proj) == set() and not rec.events,
                  "no manifest: nothing declared, nothing said")
            declare_scaffold(proj, ["nova_check.py"])
            files = implementation_files(proj)
        check(files == ["main.py"] and not rec.events,
              f"a declared check file is outside the fence, quietly ({files})")
        check(not (proj / ".nova" / "scaffold.json.tmp").exists(),
              "and the atomic write leaves no temp file behind")

        manifest.write_bytes(b'{"paths": ["nova_check.py"')
        with listening(scaffold_mod) as rec:
            files = implementation_files(proj)
        check(files == ["main.py", "nova_check.py"],
              f"a truncated manifest declares nothing: the safe direction ({files})")
        check("JSONDecodeError" in _err(rec, "scaffold_manifest_unreadable"),
              "and it is said, with why")

        manifest.write_bytes(b'\xff\xfe{"paths": []}')
        raised = ""
        with listening(scaffold_mod) as rec:
            try:
                implementation_files(proj)
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {str(e)[:80]}"
        check(not raised, f"a manifest that is not UTF-8 no longer raises ({raised or 'no raise'})")
        check("UnicodeDecodeError" in _err(rec, "scaffold_manifest_unreadable"),
              "and it is said")

        manifest.write_text(json.dumps({"paths": "nova_check.py"}), encoding="utf-8")
        with listening(scaffold_mod) as rec:
            got = declared_scaffold(proj)
        check(got == set() and "found str" in _err(rec, "scaffold_manifest_unreadable"),
              "paths that are not a list: nothing declared, and said")

        manifest.write_bytes(b'{"paths": ["nova_check.py", "nova_old')
        old_bytes = manifest.read_bytes()
        with listening(scaffold_mod) as rec:
            out = declare_scaffold(proj, ["nova_check2.py"])
        kept = proj / ".nova" / "scaffold.json.unreadable"
        check(out == {"nova_check2.py"}, f"declaring over an unreadable manifest still works ({sorted(out)})")
        check(json.loads(manifest.read_text(encoding="utf-8")) == {"paths": ["nova_check2.py"]},
              "and the new manifest is readable")
        check(kept.exists() and kept.read_bytes() == old_bytes,
              "the unreadable manifest's bytes are kept beside it, unchanged")
        ev = rec.named("scaffold_manifest_replaced")
        check(len(ev) == 1 and ev[0][1].get("old_copy") == str(kept),
              "and the loss is said, naming where the old copy went")
        check(not rec.named("scaffold_manifest_unreadable"),
              "one event per incident: declare does not also report a plain read")

        # A crash between writing and swapping must leave the old manifest whole.
        manifest.write_text(json.dumps({"paths": ["a.py"]}), encoding="utf-8")
        real_replace = os.replace

        def _crash(*_a: Any, **_kw: Any) -> None:
            raise OSError("simulated crash before the swap")

        scaffold_mod.os.replace = _crash
        try:
            with contextlib.suppress(OSError):
                declare_scaffold(proj, ["b.py"])
        finally:
            scaffold_mod.os.replace = real_replace
        with listening(scaffold_mod) as rec:
            after = declared_scaffold(proj)
        check(after == {"a.py"} and not rec.events,
              f"a crash before the swap leaves the old manifest intact ({sorted(after)})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 3. the conversation state ───────────────────────────────────────────────

async def test_conversation_state() -> None:
    check.section("3. ConversationState: damage is said, and a bad field no longer raises")
    from core.conversation_state import ConversationState, ConversationStateStore
    from memory.backends.diskcache_backend import DiskCacheBackend

    good = {"last_user_messages": ["hi"], "last_assistant_replies": ["hello"],
            "last_follow_up_questions": [], "last_mode": "chat"}
    with listening(state_mod) as rec:
        a = ConversationState.from_obj(None)
        b = ConversationState.from_obj(good)
        c = ConversationState.from_obj(json.dumps(good))
    check(a.last_user_messages == [] and b.last_user_messages == ["hi"]
          and c.last_assistant_replies == ["hello"] and b.last_mode == "chat",
          "nothing stored, a stored object, and legacy JSON text all load")
    check(not rec.events, "and none of them is reported")

    for label, obj, expect in (
        ("text that is not JSON", "not json", "not JSON"),
        ("JSON null", "null", "found NoneType"),
        ("a JSON list", "[1, 2]", "found list"),
        ("a number", 42, "found int"),
    ):
        with listening(state_mod) as rec:
            st = ConversationState.from_obj(obj)
        check(st.last_user_messages == [] and expect in _err(rec, "conversation_state_unreadable"),
              f"{label}: empty, and said ({_err(rec, 'conversation_state_unreadable')[:50]})")

    raised = ""
    with listening(state_mod) as rec:
        try:
            st = ConversationState.from_obj({"last_user_messages": 7,
                                             "last_assistant_replies": "hello"})
        except Exception as e:  # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
    check(not raised, f"a field holding a number does not raise ({raised or 'no raise'})")
    if not raised:
        check(st.last_user_messages == [] and st.last_assistant_replies == [],
              f"and a field holding text is not split into characters ({st.last_assistant_replies[:3]})")
        fields = sorted(kw.get("field", "") for _l, kw in rec.named("conversation_state_unreadable"))
        check(fields == ["last_assistant_replies", "last_user_messages"],
              f"each damaged field is named ({fields})")

    # Through the real store and cache: what load() actually reads.
    root = _tmp("state")
    cache = DiskCacheBackend(root / "dc")
    try:
        store = ConversationStateStore(cache)
        cid = uuid4()
        await cache.set(store._key(cid), {"last_user_messages": 7,
                                          "last_assistant_replies": ["kept"]}, ttl_s=60)
        raised = ""
        with listening(state_mod) as rec:
            try:
                st = await store.load(cid)
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
        check(not raised and st.last_assistant_replies == ["kept"] and st.last_user_messages == [],
              f"store.load keeps what is intact and drops what is not ({raised or 'loaded'})")
        check(bool(rec.named("conversation_state_unreadable")), "and says so")
        await store.record_turn(conversation_id=cid, user_message="hello again",
                                assistant_reply="hey", follow_up_question=None, mode="chat")
        st2 = await store.load(cid)
        check(st2.last_user_messages == ["hello again"],
              "the next turn records over it normally")
    finally:
        await cache.close()
        shutil.rmtree(root, ignore_errors=True)


# ── 4, 5, 10. the project builder ───────────────────────────────────────────

class _Local:
    pass


class _RaisingRouter:
    def for_role(self, role: str):
        raise RuntimeError("router exploded")


async def test_builder() -> None:
    check.section("4. ProjectBuilder._handle: a router that raises is said")
    from core.orchestrator.model_router import ModelHandle, ModelRouter
    from core.project_builder import ProjectBuilder

    root = _tmp("builder")
    try:
        local, cloud = _Local(), _Local()
        sem, csem = asyncio.Semaphore(1), asyncio.Semaphore(4)
        pb = ProjectBuilder(projects_dir=root, llm=local, llm_semaphore=sem, memory=None,
                            models=_RaisingRouter())
        with listening(builder_mod) as rec:
            r, s = pb._handle("coder")
        check(r is local and s is sem, "it still builds, on the local model")
        ev = rec.named("builder_model_route_failed")
        check(len(ev) == 1 and ev[0][1].get("role") == "coder"
              and "RuntimeError: router exploded" in str(ev[0][1].get("error"))
              and ev[0][1].get("fell_back_to") == "local",
              "and says which role, why, and where it went")

        router = ModelRouter({"primary": ModelHandle("primary", local, sem),
                              "cloud": ModelHandle("cloud", cloud, csem)},
                             default="primary", role_map={"coder": "cloud"})
        pb2 = ProjectBuilder(projects_dir=root, llm=local, llm_semaphore=sem, memory=None,
                             models=router)
        pb3 = ProjectBuilder(projects_dir=root, llm=local, llm_semaphore=sem, memory=None)
        with listening(builder_mod) as rec:
            routed = pb2._handle("coder")[0]
            unrouted = pb3._handle("coder")[0]
        check(routed is cloud and unrouted is local and not rec.events,
              "a working router, and no router at all, say nothing")

        check.section("5. ProjectBuilder._read_project_md: missing is quiet, unreadable is said")
        projects = root / "projects"
        (projects / "demo").mkdir(parents=True)
        pb4 = ProjectBuilder(projects_dir=projects, llm=local, llm_semaphore=sem, memory=None)
        with listening(builder_mod) as rec:
            empty = pb4._read_project_md("demo")
        check(empty == "" and not rec.events, "no PROJECT.md yet: empty, nothing said")
        (projects / "demo" / "PROJECT.md").write_text("# Demo\n", encoding="utf-8")
        check(pb4._read_project_md("demo") == "# Demo\n", "a PROJECT.md is read")
        (projects / "locked" / "PROJECT.md").mkdir(parents=True)   # exists, cannot be read as a file
        with listening(builder_mod) as rec:
            out = pb4._read_project_md("locked")
        ev = rec.named("project_md_unreadable")
        check(out == "" and len(ev) == 1 and ev[0][1].get("project") == "locked",
              f"one that cannot be opened: still empty, but said ({_err(rec, 'project_md_unreadable')[:60]})")

        check.section("10. a test review that does not compile is discarded AND recorded")
        proj = projects / "adder"
        proj.mkdir()
        (proj / "main.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        original_test = ("from main import add\n\n\ndef test_add():\n"
                         "    assert add(2, 3) == 5\n")
        broken_review = ("from main import add\n\n\ndef test_add(:\n"
                         "    assert add(2, 3) == 5\n")
        good_review = original_test + "\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n"

        async def _generate(review: str) -> tuple[str | None, _Recorder, str]:
            replies = [original_test, review]
            pb5 = ProjectBuilder(projects_dir=projects, llm=local, llm_semaphore=sem, memory=None)

            async def _scripted(prompt: str, *_a: Any, **_kw: Any) -> str:
                return replies.pop(0)

            pb5._llm_file = _scripted
            pb5._run_test_file = lambda *_a, **_kw: None     # the tests "pass"
            pb5._run_check = lambda *_a, **_kw: None         # and the program launches
            test_file = proj / "test_main.py"
            test_file.unlink(missing_ok=True)
            with listening(builder_mod) as rec:
                out = await pb5._generate_and_run_tests("adder", proj, ["main.py"], "adds numbers")
            written = (test_file.read_text(encoding="utf-8") if test_file.exists()
                       else "<no test file was kept>")
            return out, rec, written

        out, rec, written = await _generate(broken_review)
        ev = rec.named("builder_test_review_discarded")
        check(written == original_test, "a review that broke the test is not used: the original is kept")
        check(len(ev) == 1 and ev[0][1].get("file") == "test_main.py"
              and ev[0][1].get("project") == "adder"
              and "SyntaxError" in str(ev[0][1].get("error")),
              "and that is recorded, with the compile error")
        check(out == "Logic tests passed (test_main.py).", f"the run carries on as before ({out})")

        out, rec, written = await _generate(good_review)
        check(written == good_review and not rec.named("builder_test_review_discarded"),
              "a review that compiles is used, and nothing is recorded")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 6, 8. the memory ingest worker ──────────────────────────────────────────

async def _ingest_worker(root: Path):
    from core.conversation_state import ConversationStateStore
    from core.policy.memory_extractor import MemoryExtractorLLM
    from core.policy.summarizer import SummarizerLLM
    from core.workers.memory_ingest import MemoryIngestWorker
    from memory.backends.diskcache_backend import DiskCacheBackend
    from memory.unifier import MemoryUnifier

    mem = MemoryUnifier(root, enable_chroma=False)
    await mem.initialize()
    sem = asyncio.Semaphore(1)
    llm = ScriptedLLM()
    llm.default_reply = '{"facts":[]}'
    cache = DiskCacheBackend(root / "dc")
    worker = MemoryIngestWorker(
        memory=mem, extractor=MemoryExtractorLLM(llm, llm_semaphore=sem),
        summarizer=SummarizerLLM(llm, llm_semaphore=sem),
        state=ConversationStateStore(cache), queue=asyncio.Queue(),
        summarize_queue=asyncio.Queue(), summary_every_n=2)
    return mem, worker, cache


class _BrokenQueue:
    def get_nowait(self):
        raise RuntimeError("queue broken")


async def test_ingest_worker() -> None:
    check.section("6. policy facts: malformed and failed writes are said, the rest still land")
    from core.events import MemoryIngestEvent
    from core.turn_identity import TurnIdentity

    root = _tmp("ingest")
    mem, worker, cache = await _ingest_worker(root)
    try:
        real_add = mem.add_fact

        async def _flaky_add(**kw: Any):
            if kw.get("attribute") == "allergy":
                raise RuntimeError("database is locked")
            return await real_add(**kw)

        mem.add_fact = _flaky_add
        ev = MemoryIngestEvent(
            conversation_id=uuid4(), user_message="", assistant_message="",
            timestamp=datetime.now(timezone.utc),
            policy_memory_facts=[
                {"entity": "user", "attribute": "pet", "value": "Mochi", "confidence": "not-a-number"},
                {"entity": "", "attribute": "broken", "value": "x"},
                "not a fact at all",
                {"entity": "user", "attribute": "allergy", "value": "peanuts", "confidence": 0.9},
                {"entity": "user", "attribute": "temp", "value": "x", "persist": False},
                {"entity": "user", "attribute": "friend", "value": "Sam", "confidence": 0.8},
            ])
        raised = ""
        with listening(ingest_mod) as rec:
            try:
                await worker._ingest_scoped(ev, TurnIdentity.typed())
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
        mem.add_fact = real_add
        check(not raised, f"one bad or failing fact does not stop the event ({raised or 'no raise'})")

        friend = await mem.get_latest_fact(entity="user", attribute="friend")
        allergy = await mem.get_latest_fact(entity="user", attribute="allergy")
        temp = await mem.get_latest_fact(entity="user", attribute="temp")
        check(friend is not None and friend.value == "Sam",
              "the valid fact after every failure is still stored")
        check(allergy is None and temp is None, "the failed write and the persist:false fact are not")

        malformed = rec.named("policy_fact_malformed")
        attrs = sorted(kw.get("attribute", "") for _l, kw in malformed)
        check(len(malformed) == 3 and attrs == ["", "broken", "pet"],
              f"each malformed fact is said, by attribute where it has one ({attrs})")
        check(any("ValueError" in str(kw.get("error")) for _l, kw in malformed)
              and any("missing entity" in str(kw.get("error")) for _l, kw in malformed)
              and any("AttributeError" in str(kw.get("error")) for _l, kw in malformed),
              "with why: a bad number, a missing field, not a mapping at all")
        failed = rec.named("policy_fact_write_failed")
        check(len(failed) == 1 and failed[0][0] == "exception"
              and failed[0][1].get("attribute") == "allergy"
              and "database is locked" in str(failed[0][1].get("error")),
              "the write that failed is reported at error level, by attribute")
        check(not any(kw.get("attribute") == "temp" for _lvl, _name, kw in rec.events),
              "persist:false is a choice, not a failure: nothing is said about it")

        check.section("8. summarize hints: only an empty queue is quiet")
        with listening(ingest_mod) as rec:
            await worker._drain_summarize_hints(max_items=2)
        check(not rec.events, "an empty queue says nothing")
        worker._sq = _BrokenQueue()
        raised = ""
        with listening(ingest_mod) as rec:
            try:
                await worker._drain_summarize_hints(max_items=1)
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
        ev2 = rec.named("summarize_hint_read_failed")
        check(not raised, f"anything else does not end the worker's loop ({raised or 'no raise'})")
        check(len(ev2) == 1 and ev2[0][0] == "exception" and "queue broken" in str(ev2[0][1].get("error")),
              "and is reported at error level")
    finally:
        await cache.close()
        shutil.rmtree(root, ignore_errors=True)


# ── 7, 9. the Chroma backend ────────────────────────────────────────────────

async def test_chroma_journal() -> None:
    check.section("7. the promotion journal: unreadable is said, once, and no longer blocks rebuilds")
    from memory.backends.chroma_backend import ChromaMemoryBackend, SemanticRecoveryError

    root = _tmp("journal")
    try:
        be = ChromaMemoryBackend(root / "a")
        jp = be._journal_path()
        jp.parent.mkdir(parents=True, exist_ok=True)
        with listening(chroma_mod) as rec:
            check(be._read_journal() is None and not rec.events,
                  "no journal: None, nothing said")
            jp.write_bytes(b'{"state": "promo')
            first = be._read_journal()
            be._read_journal()
            not_json = rec.named("semantic_journal_unreadable")
            jp.write_bytes(b'\xff\xfe\x00{')
            be._read_journal()
            jp.write_text("[1, 2]", encoding="utf-8")
            be._read_journal()
            jp.write_text(json.dumps({"state": "promoting"}), encoding="utf-8")
            entry = be._read_journal()
        check(first is None and len(not_json) == 1 and "not JSON" in str(not_json[0][1].get("error"))
              and not_json[0][1].get("path") == str(jp),
              "truncated JSON: None, said once even when read twice")
        errors = [str(kw.get("error")) for _l, kw in rec.named("semantic_journal_unreadable")]
        check(len(errors) == 3 and "UnicodeDecodeError" in errors[1] and "found list" in errors[2],
              f"each new reason is said: not UTF-8, not an object ({len(errors)} said)")
        check(entry == {"state": "promoting"}, "a readable journal is returned as it is")

        # The rebuild block: an unreadable journal with NO backup beside it.
        be2 = ChromaMemoryBackend(root / "b")
        jp2 = be2._journal_path()
        jp2.parent.mkdir(parents=True, exist_ok=True)
        jp2.write_bytes(b'{"state": "promo')
        started, refused = False, ""
        with listening(chroma_mod) as rec:
            await be2.count()                 # opening runs recovery
            cleared = not jp2.exists()
            try:
                await be2.begin_staged_rebuild()
                started = bool(be2._writing_staged)
            except SemanticRecoveryError as e:
                refused = str(e)[:90]
            except Exception as e:  # noqa: BLE001
                refused = f"{type(e).__name__}: {str(e)[:80]}"
            await be2.abort_staged_rebuild()
        check(cleared, "with no backup, an unreadable journal is residue and is cleared")
        check(started, f"so the next rebuild can start (it used to be refused on every boot: {refused or 'started'})")
        check(bool(rec.named("semantic_journal_unreadable")), "and the damage is still said")

        # A journal that cannot be removed keeps blocking rebuilds: say so.
        be3 = ChromaMemoryBackend(root / "c")
        jp3 = be3._journal_path()
        jp3.mkdir(parents=True)                 # a directory where the file should be
        (jp3 / "x").write_text("x", encoding="utf-8")
        raised = ""
        with listening(chroma_mod) as rec:
            try:
                be3._write_journal(None)
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
        ev = rec.named("semantic_journal_not_removed")
        check(not raised and len(ev) == 1 and ev[0][1].get("path") == str(jp3),
              f"a journal that cannot be removed is said, not raised ({raised or _err(rec, 'semantic_journal_not_removed')[:50]})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_chroma_staging() -> None:
    check.section("9. the staging collection: delete only what exists, never hide a failure")
    from memory.backends.chroma_backend import ChromaMemoryBackend

    root = _tmp("staging")
    try:
        be = ChromaMemoryBackend(root / "s")
        await be.count()                                   # open the client and live
        staging = be._staging_name()
        real_delete = be._client.delete_collection
        calls: list[str] = []

        def _counting(name: str, *a: Any, **kw: Any):
            calls.append(name)
            return real_delete(name, *a, **kw)

        def _failing(name: str, *_a: Any, **_kw: Any):
            calls.append(name)
            raise RuntimeError("disk busy")

        be._client.delete_collection = _counting
        raised = ""
        with listening(chroma_mod) as rec:
            try:
                await be.begin_staged_rebuild()
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {str(e)[:80]}"
        check(not raised and be._writing_staged and calls == [] and not rec.events,
              f"no leftover staging: nothing to delete, nothing said ({raised or calls})")

        with listening(chroma_mod) as rec:
            await be.abort_staged_rebuild()
        check(calls == [staging] and not rec.events,
              "abort deletes the staging it created, quietly")
        calls.clear()
        with listening(chroma_mod) as rec:
            await be.abort_staged_rebuild()
        check(calls == [] and not rec.events, "abort with nothing staged deletes nothing")

        # A leftover from a crashed rebuild that CANNOT be deleted.
        await asyncio.to_thread(be._client.get_or_create_collection, name=staging,
                                embedding_function=be._emb)
        be._client.delete_collection = _failing
        raised = ""
        try:
            await be.begin_staged_rebuild()
        except RuntimeError as e:
            raised = str(e)
        check("disk busy" in raised,
              f"a leftover staging that cannot be deleted stops the rebuild with the real reason ({raised or 'no raise'})")
        check(not be._writing_staged, "and nothing is built on the leftover garbage")

        with listening(chroma_mod) as rec:
            await be.abort_staged_rebuild()          # must not raise over the real failure
        ev = rec.named("semantic_staging_cleanup_failed")
        check(len(ev) == 1 and ev[0][1].get("collection") == staging
              and "disk busy" in str(ev[0][1].get("error")),
              "abort that cannot delete says so instead of raising")
        be._client.delete_collection = real_delete
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 11. the unifier's semantic index check ──────────────────────────────────

class _CountRaises:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def count(self) -> int:
        raise self._exc


class _CountIs:
    def __init__(self, n: int) -> None:
        self._n = n

    async def count(self) -> int:
        return self._n


async def test_semantic_index_check() -> None:
    check.section("11. _ensure_semantic_index: a refusal is named, a count failure is said")
    from memory.backends.chroma_backend import ChromaMemoryBackend, SemanticRecoveryError
    from memory.unifier import MemoryUnifier

    root = _tmp("unifier")
    try:
        mem = MemoryUnifier(root / "u", enable_chroma=False)
        await mem.initialize()
        await mem.add_fact(entity="user", attribute="editor", value="neovim", confidence=0.9)
        rebuilds: list[int] = []

        async def _fake_rebuild() -> dict[str, Any]:
            rebuilds.append(1)
            return {}

        mem.rebuild_semantic_index = _fake_rebuild

        mem._chroma = _CountRaises(SemanticRecoveryError("injected refusal"))
        with listening(unifier_mod) as rec:
            await mem._ensure_semantic_index()
        check("injected refusal" in _err(rec, "semantic_index_unavailable"),
              "a recovery refusal is reported as what it is")
        check(rebuilds == [] and not rec.named("semantic_index_count_failed"),
              "and no rebuild is started: it would run the same recovery and refuse")

        mem._chroma = _CountRaises(RuntimeError("count broke"))
        with listening(unifier_mod) as rec:
            await mem._ensure_semantic_index()
        check("RuntimeError: count broke" in _err(rec, "semantic_index_count_failed"),
              "any other count failure is said")
        check(rebuilds == [1], "and still treated as empty, so the staged rebuild can repair it")

        rebuilds.clear()
        for n, want in ((3, []), (0, [1])):
            mem._chroma = _CountIs(n)
            with listening(unifier_mod) as rec:
                await mem._ensure_semantic_index()
            check(rebuilds == want and not rec.events,
                  f"a count of {n}: {'rebuild' if want else 'no rebuild'}, and nothing said")
            rebuilds.clear()

        # The real objects: the refusal T1 of test_semantic_vector_space_p10
        # injects, seen from the unifier.
        td = root / "real"
        be = ChromaMemoryBackend(td / "chroma")
        await asyncio.to_thread(be._ensure)
        col = await asyncio.to_thread(be._client.get_collection, be._collection_name,
                                      embedding_function=be._emb)
        await asyncio.to_thread(col.modify, name=be._backup_name())
        real_restore = ChromaMemoryBackend._restore_backup
        ChromaMemoryBackend._restore_backup = lambda self, live, backup: (False, "injected restore failure")
        try:
            real = MemoryUnifier(td)
            await real.initialize()
            await real.add_fact(entity="user", attribute="editor", value="neovim", confidence=0.9)
            calls: list[int] = []
            real_rebuild = real.rebuild_semantic_index

            async def _watch() -> dict[str, Any]:
                calls.append(1)
                return await real_rebuild()

            real.rebuild_semantic_index = _watch
            with listening(unifier_mod) as rec:
                await real._ensure_semantic_index()
            check("could not be restored" in _err(rec, "semantic_index_unavailable"),
                  "the real refusal reaches the unifier under its own name")
            check(calls == [] and not rec.named("semantic_rebuild_aborted"),
                  "and no doomed rebuild runs to report it late as an abort")
        finally:
            ChromaMemoryBackend._restore_backup = real_restore
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    test_spend_file()
    await test_boot_with_a_bad_spend_file()
    test_scaffold_manifest()
    await test_conversation_state()
    await test_builder()
    await test_ingest_worker()
    await test_chroma_journal()
    await test_chroma_staging()
    await test_semantic_index_check()
    check.finish()


if __name__ == "__main__":
    run(main)
