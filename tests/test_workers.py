"""Full-audit coverage for the background workers (previously none).

These run unattended. Nobody is watching when they fail, so a worker that
quietly stops doing its job looks exactly like a worker with nothing to do —
the same category that produced the lost-turn and silent-capture findings.

Real MemoryUnifier (SQLite, chroma off) underneath; the LLM side is scripted.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, ScriptedLLM

from core.conversation_state import ConversationStateStore
from core.events import MemoryIngestEvent, SummarizeHintEvent
from core.policy.autonomy_planner import AutonomyPlannerLLM
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.summarizer import SummarizerLLM
from core.tool_router import ToolRouter
from core.workers.autonomy_supervisor import AutonomySupervisorWorker
from core.workers.lifecycle import stop_worker
from core.workers.memory_ingest import MemoryIngestWorker
from memory.backends.diskcache_backend import DiskCacheBackend
from memory.unifier import MemoryUnifier

check = Checks()


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def build_ingest(tmp: Path, extract_reply: str, summary_reply: str = ""):
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()
    sem = asyncio.Semaphore(1)

    ex_llm = ScriptedLLM()
    ex_llm.default_reply = extract_reply
    sum_llm = ScriptedLLM()
    sum_llm.default_reply = summary_reply or '{"summary":"They talked about the fort."}'

    state = ConversationStateStore(DiskCacheBackend(tmp / "dc"))
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    sq: asyncio.Queue = asyncio.Queue(maxsize=50)
    worker = MemoryIngestWorker(
        memory=mem,
        extractor=MemoryExtractorLLM(ex_llm, llm_semaphore=sem),
        summarizer=SummarizerLLM(sum_llm, llm_semaphore=sem),
        state=state, queue=q, summarize_queue=sq, summary_every_n=2,
    )
    return mem, worker, q, sq, state


async def test_memory_ingest(tmp: Path) -> None:
    check.section("MemoryIngestWorker — the happy path")
    facts_json = ('{"facts":[{"entity":"user","attribute":"spouse","value":"Leslie",'
                  '"confidence":0.9,"persist":true}]}')
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w1", facts_json)
    conv = uuid4()

    worker.start()
    await q.put(MemoryIngestEvent(conversation_id=conv, user_message="my wife is Leslie",
                                 assistant_message="Noted.", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(q.join(), timeout=10)

    rows = await mem._sqlite.recent_turns(conv, limit=10)
    contents = [r["content"] for r in rows]
    check(any("my wife is Leslie" in c for c in contents), "the user turn is persisted")
    check(any("Noted." in c for c in contents), "the assistant turn is persisted")

    fact = await mem.get_latest_fact(entity="user", attribute="spouse")
    check(fact is not None and fact.value == "Leslie", "the extracted fact is persisted")
    await worker.stop()

    check.section("MemoryIngestWorker — filters")
    low = '{"facts":[{"entity":"user","attribute":"spouse","value":"Nope","confidence":0.4,"persist":true}]}'
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w2", low)
    worker.start()
    await q.put(MemoryIngestEvent(conversation_id=uuid4(), user_message="maybe my wife is Nope",
                                 assistant_message="", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(q.join(), timeout=10)
    check(await mem.get_latest_fact(entity="user", attribute="spouse") is None,
          "a fact below the 0.55 confidence floor is not persisted")
    await worker.stop()

    nopersist = '{"facts":[{"entity":"user","attribute":"spouse","value":"Temp","confidence":0.9,"persist":false}]}'
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w3", nopersist)
    worker.start()
    await q.put(MemoryIngestEvent(conversation_id=uuid4(), user_message="x",
                                 assistant_message="", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(q.join(), timeout=10)
    check(await mem.get_latest_fact(entity="user", attribute="spouse") is None,
          "persist:false is honored")
    await worker.stop()

    check.section("MemoryIngestWorker — policy facts and bad input")
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w4", '{"facts":[]}')
    worker.start()
    await q.put(MemoryIngestEvent(
        conversation_id=uuid4(), user_message="hi", assistant_message="",
        timestamp=_now(),
        policy_memory_facts=[
            {"entity": "user", "attribute": "location", "value": "Austin", "confidence": 0.9},
            {"entity": "", "attribute": "broken", "value": "x"},          # skipped: no entity
            {"entity": "user", "attribute": "pet", "value": "Mochi", "confidence": "not-a-number"},
            {"entity": "user", "attribute": "friend", "value": "Sam", "confidence": 0.8},
        ]))
    await asyncio.wait_for(q.join(), timeout=10)
    loc = await mem.get_latest_fact(entity="user", attribute="location")
    friend = await mem.get_latest_fact(entity="user", attribute="friend")
    check(loc is not None and loc.value == "Austin", "a valid policy fact is persisted")
    check(friend is not None and friend.value == "Sam",
          "a malformed policy fact does not stop the ones after it")
    await worker.stop()

    check.section("MemoryIngestWorker — survives a broken extractor")
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w5", '{"facts":[]}')

    async def _boom(**_kw):
        raise RuntimeError("extractor exploded")

    worker._extractor.extract = _boom
    worker.start()
    conv = uuid4()
    await q.put(MemoryIngestEvent(conversation_id=conv, user_message="still record me",
                                 assistant_message="ok", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(q.join(), timeout=10)
    await q.put(MemoryIngestEvent(conversation_id=conv, user_message="and me too",
                                 assistant_message="ok", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(q.join(), timeout=10)
    check(worker._task is not None and not worker._task.done(),
          "the worker keeps running after an extractor exception")
    await worker.stop()

    check.section("MemoryIngestWorker — summarization")
    mem, worker, q, sq, state = await build_ingest(tmp / "w6", '{"facts":[]}')
    conv = uuid4()
    await state.record_turn(conversation_id=conv, user_message="we built a fort",
                            assistant_reply="sounds fun", follow_up_question=None, mode="chat")
    worker.start()
    await sq.put(SummarizeHintEvent(conversation_id=conv, timestamp=_now(), reason="test"))
    for _ in range(40):
        if await mem.get_latest_fact(entity=f"conversation:{conv}", attribute="summary"):
            break
        await asyncio.sleep(0.25)
    summary = await mem.get_latest_fact(entity=f"conversation:{conv}", attribute="summary")
    check(summary is not None and "fort" in summary.value, f"a rolling summary is written ({summary})")
    day = _now().strftime("%Y-%m-%d")
    digest = await mem.get_latest_fact(entity=f"conversation:{conv}:digest", attribute=day)
    check(digest is not None and day in digest.value,
          "a DATED digest is also written so history is not overwritten")
    await worker.stop()


async def test_ingest_drains_on_stop(tmp: Path) -> None:
    """Turns queued but not yet handled must not be thrown away at shutdown —
    they are the most recent things said, and losing them is invisible."""
    check.section("MemoryIngestWorker — queued turns survive shutdown")
    mem, worker, q, _sq, _state = await build_ingest(tmp / "w7", '{"facts":[]}')
    conv = uuid4()
    for i in range(5):
        await q.put(MemoryIngestEvent(conversation_id=conv, user_message=f"queued message {i}",
                                      assistant_message="ok", timestamp=_now(), policy_memory_facts=[]))
    worker.start()
    await asyncio.sleep(0.05)   # let it pick up at most the first one
    await worker.stop()

    rows = await mem._sqlite.recent_turns(conv, limit=50)
    got = sum(1 for r in rows if "queued message" in str(r["content"]))
    check(got == 5, f"all 5 queued turns reached long-term memory ({got}/5)")


async def test_autonomy_supervisor(tmp: Path) -> None:
    check.section("AutonomySupervisorWorker")
    mem = MemoryUnifier(tmp / "auto", enable_chroma=False)
    await mem.initialize()
    router = ToolRouter({"system.noop": lambda a: asyncio.sleep(0, result={"ok": True})}, {})
    sem = asyncio.Semaphore(1)
    llm = ScriptedLLM()
    llm.default_reply = '{"action":"idle","reason":"nothing to do","tool_calls":[],"new_tasks":[]}'
    worker = AutonomySupervisorWorker(
        memory=mem, planner=AutonomyPlannerLLM(llm, llm_semaphore=sem), router=router, tick_seconds=0.05)

    tid = await mem.enqueue_task(title="Think about dinner", details="d", priority=3,
                                 project_name="temp", initiated_by_user=True)
    worker.start()
    for _ in range(60):
        tasks = await mem.list_tasks(status="done", limit=20)
        if tasks:
            break
        await asyncio.sleep(0.25)
    done = await mem.list_tasks(status="done", limit=20)
    check(len(done) >= 1, f"an idle plan marks the task done ({len(done)} done)")
    await worker.stop()

    check.section("AutonomySupervisorWorker — a crashing plan must not orphan the task")
    mem2 = MemoryUnifier(tmp / "auto2", enable_chroma=False)
    await mem2.initialize()
    worker2 = AutonomySupervisorWorker(
        memory=mem2, planner=AutonomyPlannerLLM(llm, llm_semaphore=sem), router=router, tick_seconds=0.05)

    async def _explode(**_kw):
        raise RuntimeError("planner exploded")

    worker2._planner.plan = _explode
    await mem2.enqueue_task(title="Doomed task", details="d", priority=3,
                            project_name="temp", initiated_by_user=True)
    worker2.start()
    await asyncio.sleep(2.5)
    await worker2.stop()

    # Claimed-but-never-finished is the failure: the task sits "running"
    # forever, invisible, and is never retried within the session.
    running = await mem2.list_tasks(status="running", limit=20)
    failed = await mem2.list_tasks(status="failed", limit=20)
    done2 = await mem2.list_tasks(status="done", limit=20)
    check(not running, f"the task is not left stuck in 'running' ({len(running)} stuck)")
    check(bool(failed or done2), f"it is resolved honestly instead (failed={len(failed)} done={len(done2)})")


async def test_log_worker_error() -> None:
    """The error handler is the safest code in a worker — it runs when things
    are already wrong. This one used to be able to kill the worker: on a
    Windows cp1252 console, structlog's rich traceback emits box characters,
    so logger.exception() raised UnicodeEncodeError from INSIDE the except
    block and MemoryIngestWorker._run died. All long-term memory writes then
    stopped for the rest of the session, silently."""
    check.section("log_worker_error never raises")
    from core.workers.lifecycle import log_worker_error

    class ExplodingLogger:
        def exception(self, *_a, **_kw):
            raise UnicodeEncodeError("charmap", "┌", 0, 1, "cannot encode")

    log_worker_error(ExplodingLogger(), "test_event", RuntimeError("original problem"))
    check(True, "a logger that raises UnicodeEncodeError does not propagate")

    class TotallyBroken:
        def exception(self, *_a, **_kw):
            raise RuntimeError("logging subsystem is down")

    log_worker_error(TotallyBroken(), "test_event", RuntimeError("original problem"))
    check(True, "any logging failure at all is contained")

    calls = []

    class GoodLogger:
        def exception(self, event, **kw):
            calls.append((event, kw))

    log_worker_error(GoodLogger(), "worked", ValueError("boom"), task_id="t1")
    check(calls and calls[0][0] == "worked", "the normal path still logs the event")
    check("boom" in str(calls[0][1]), "and carries the real error text")
    check(calls[0][1].get("task_id") == "t1", "and any extra fields")


async def test_lifecycle() -> None:
    check.section("stop_worker")
    check(await stop_worker(None) is None, "stopping a None task is a no-op")

    stop = asyncio.Event()

    async def polite():
        while not stop.is_set():
            await asyncio.sleep(0.02)
        return "clean"

    t = asyncio.create_task(polite())
    await asyncio.sleep(0.05)
    stop.set()
    await stop_worker(t, name="polite", grace_s=2.0)
    check(t.done() and not t.cancelled(), "a cooperative worker exits cleanly, never cancelled")
    check(t.result() == "clean", "its return value is preserved (it really finished)")

    async def stubborn():
        while True:
            await asyncio.sleep(0.05)

    t2 = asyncio.create_task(stubborn())
    await asyncio.sleep(0.05)
    await stop_worker(t2, name="stubborn", grace_s=0.2)
    check(t2.done(), "a worker that ignores the stop event is force-cancelled")
    check(t2.cancelled(), "and is genuinely cancelled, not silently left running")

    async def already():
        return 1

    t3 = asyncio.create_task(already())
    await asyncio.sleep(0.05)
    await stop_worker(t3, name="finished")
    check(t3.done(), "an already-finished task is handled without raising")

    async def raiser():
        raise RuntimeError("worker died on its own")

    t4 = asyncio.create_task(raiser())
    await asyncio.sleep(0.05)
    await stop_worker(t4, name="raiser")
    check(True, "a worker that died on its own does not break shutdown")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_memory_ingest(tmp)
        await test_ingest_drains_on_stop(tmp)
        await test_autonomy_supervisor(tmp)
        await test_log_worker_error()
        await test_lifecycle()
    check.finish()


asyncio.run(main())
