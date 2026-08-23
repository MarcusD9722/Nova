"""One Nova, several things happening at once, nothing crossing over.

`backend/state.py` keeps ONE process-wide RuntimeManager (`STATE.runtime`), so
every concurrent chat, story and background goal shares it. Each defect
independent review found was a case of per-turn work stored somewhere shared —
so this suite runs the paths together and asserts ownership.

Barriers and events only; no sleep is used as a synchronisation mechanism.

Run:  venv\\Scripts\\python.exe tests\\test_cross_turn_concurrency.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, ScriptedLLM, run  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

APOLOGY = "Sorry — I came up empty on that one."


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NullSem:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _NoLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None


async def _runtime(td: str, llm=None):
    from core.runtime import RuntimeManager
    from core.tooling import build_tool_router

    root = Path(td)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    mem_dir = root / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    m = MemoryUnifier(mem_dir, enable_chroma=False)
    await m.initialize()
    router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
    rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                        llm=llm or _NoLLM(), router=router, memory_dir=mem_dir)
    return rt, m


class _Barriered:
    """A model that meets another turn at a barrier, mid-generation."""

    def __init__(self, body: str, barrier: asyncio.Barrier | None) -> None:
        self._body = body
        self._barrier = barrier
        self._synced = False
        self.semaphore = _NullSem()

    class _Runtime:
        def __init__(self, o):
            self._o = o

        async def chat_stream(self, messages, **kw):
            mid = len(self._o._body) // 2
            yield self._o._body[:mid]
            if self._o._barrier is not None and not self._o._synced:
                self._o._synced = True
                await self._o._barrier.wait()
            yield self._o._body[mid:]

    @property
    def runtime(self):
        return _Barriered._Runtime(self)


class _EmptyThenRescue:
    """Produces nothing while streaming; its chat() rescue works."""

    def __init__(self, rescue: str) -> None:
        self._rescue = rescue
        self.semaphore = _NullSem()

    class _Runtime:
        def __init__(self, o):
            self._o = o

        async def chat_stream(self, messages, **kw):
            return
            yield ""      # pragma: no cover

    @property
    def runtime(self):
        return _EmptyThenRescue._Runtime(self)


async def _reply(rt, model, *, text, previous=None, previous_user=""):
    tokens: list[str] = []
    final = ""
    async for ev in rt._stream_guarded_reply(
        model, [{"role": "user", "content": text}], budget=512,
        user_text=text, previous_replies=previous or [],
        previous_user_text=previous_user,
    ):
        if ev.get("type") == "token":
            tokens.append(ev["text"])
        elif ev.get("type") == "reply_final":
            final = ev.get("text", "")
    return "".join(tokens), final


async def test_two_ordinary_replies_side_by_side():
    check.section("C7: two concurrent replies keep their own text")

    A, B = "A" * 500, "B" * 500
    with _tmp() as td:
        rt, m = await _runtime(td)
        barrier = asyncio.Barrier(2)
        (a_seen, a_fin), (b_seen, b_fin) = await asyncio.gather(
            _reply(rt, _Barriered(A, barrier), text="alpha"),
            _reply(rt, _Barriered(B, barrier), text="beta"))

        check(set(a_seen) == {"A"} and a_fin == A, f"turn A is all A ({len(a_fin)})")
        check(set(b_seen) == {"B"} and b_fin == B, f"turn B is all B ({len(b_fin)})")
        crossings = sum(1 for t in (a_fin, b_fin) if len(set(t)) != 1)
        check(crossings == 0, f"cross-contamination count: {crossings}")


async def test_two_stories_side_by_side():
    check.section("C7: two concurrent stories keep their own text")

    class _Seg:
        def __init__(self, tag):
            self.tag = tag
            self.calls = 0

        async def chat_stream_ex(self, messages, **kw):
            self.calls += 1
            first = self.calls == 1
            body = (f"{self.tag} one. " * 10) if first else (f"{self.tag} two. " * 6)
            for i in range(0, len(body), 16):
                yield {"type": "token", "text": body[i:i + 16]}
                await asyncio.sleep(0)
            yield {"type": "done", "finish_reason": "length" if first else "stop"}

    with _tmp() as td:
        rt, m = await _runtime(td)

        async def tell(tag):
            seen, final = [], ""
            async for ev in rt._stream_story([{"role": "user", "content": "s"}],
                                             budget=256, llm=_Seg(tag)):
                if ev.get("type") == "token":
                    seen.append(ev["text"])
                elif ev.get("type") == "story_final":
                    final = ev.get("text", "")
            return "".join(seen), final

        (a_seen, a_fin), (b_seen, b_fin) = await asyncio.gather(
            tell("Auk"), tell("Bison"))

        check("Bison" not in a_fin and "Auk one" in a_fin and "Auk two" in a_fin,
              f"story A is complete and pure ({len(a_fin)} chars)")
        check("Auk" not in b_fin and "Bison one" in b_fin and "Bison two" in b_fin,
              f"story B is complete and pure ({len(b_fin)} chars)")


async def test_the_repeat_guard_while_another_chat_runs():
    check.section("C7: a guard rejection does not disturb the other turn")

    PRIOR = ("I can help with remembering things, building projects, reading "
             "code, searching the web and keeping track of reminders for you.")

    class _Replay:
        def __init__(self, barrier):
            self.calls = 0
            self.semaphore = _NullSem()
            self._barrier = barrier

        class _Runtime:
            def __init__(self, o):
                self._o = o

            async def chat_stream(self, messages, **kw):
                self._o.calls += 1
                text = PRIOR if self._o.calls == 1 else "A genuinely fresh reply."
                for i in range(0, len(text), 16):
                    yield text[i:i + 16]
                    await asyncio.sleep(0)

        @property
        def runtime(self):
            return _Replay._Runtime(self)

    with _tmp() as td:
        rt, m = await _runtime(td)
        other = "C" * 400
        replay = _Replay(None)

        (guarded_seen, guarded_fin), (other_seen, other_fin) = await asyncio.gather(
            _reply(rt, replay, text="She picked the themes already.",
                   previous=[PRIOR], previous_user="What are you capable of?"),
            _reply(rt, _Barriered(other, None), text="unrelated"))

        check(replay.calls == 2, f"the replay was rejected and retried ({replay.calls})")
        check("fresh reply" in guarded_fin,
              f"the guarded turn got its new answer ({guarded_fin[:34]!r})")
        check(PRIOR[:40] not in guarded_seen, "and the stale text never showed")
        check(other_fin == other and set(other_seen) == {"C"},
              f"while the other turn was untouched ({len(other_fin)})")


async def test_an_empty_turn_does_not_salvage_the_other_turn():
    check.section("C7: one empty generation does not disturb a healthy turn")

    with _tmp() as td:
        rt, m = await _runtime(td)
        healthy = "D" * 400

        async def empty_turn():
            seen, final = [], ""
            async for ev in rt._stream_guarded_reply(
                _EmptyThenRescue("rescued"), [{"role": "user", "content": "x"}],
                budget=512, user_text="x", previous_replies=[],
            ):
                if ev.get("type") == "token":
                    seen.append(ev["text"])
                elif ev.get("type") == "reply_final":
                    final = ev.get("text", "")
            return "".join(seen), final

        (empty_seen, empty_fin), (ok_seen, ok_fin) = await asyncio.gather(
            empty_turn(), _reply(rt, _Barriered(healthy, None), text="fine"))

        check(empty_fin == "" and empty_seen == "",
              f"the empty turn reports empty, for ITSELF ({empty_fin!r})")
        check(ok_fin == healthy,
              f"and the healthy turn keeps its whole answer ({len(ok_fin)})")
        check(APOLOGY not in ok_fin,
              "the healthy turn was not dragged into a salvage")
        check(set(ok_seen) == {"D"}, f"with no foreign text ({sorted(set(ok_seen))})")


async def test_a_goal_race_runs_beside_live_chat():
    check.section("C7: a stale goal decision cannot disturb a live chat")

    from core.agent_supervisor import AgentSupervisor, SupervisorConfig
    from core.tool_router import ToolRouter

    with _tmp() as td:
        rt, m = await _runtime(td)

        entered, release = asyncio.Event(), asyncio.Event()
        spy: list[int] = []

        async def spy_tool(_a):
            spy.append(1)
            return {"ok": True}

        llm = ScriptedLLM()
        llm.default_reply = '{"type":"tool","name":"demo.spy","args":{}}'
        sup = AgentSupervisor(
            memory=m, llm=llm, router=ToolRouter({"demo.spy": spy_tool}, {}),
            tool_descriptions={"demo.spy": "spy"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                 max_steps_per_goal=6))
        original = sup._decide_next
        calls = {"n": 0}

        async def gated(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                await release.wait()
                return await original(**kw)
            return {"type": "__inert_for_test__"}

        sup._decide_next = gated

        gid = await m.create_goal(project_name="alpha", title="t", objective="o",
                                  success_criteria="c")
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=15.0)
            await m.cancel_goal(goal_id=gid)
            await m.resume_goal(goal_id=gid)

            # A live chat turn runs WHILE the stale decision is released.
            chat_body = "E" * 400
            release.set()
            seen, final = await _reply(rt, _Barriered(chat_body, None),
                                       text="how are you")
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await m.list_goal_tasks(goal_id=str(gid), limit=50)
                if any("discarded" in (t.get("last_error") or "") for t in rows):
                    break
        finally:
            await sup.stop()

        check(final == chat_body and set(seen) == {"E"},
              f"the chat turn is unaffected ({len(final)} chars)")
        check(not spy, f"the stale goal tool never ran ({len(spy)})")
        rows = await m.list_goal_tasks(goal_id=str(gid), limit=50)
        check(any("discarded" in (t.get("last_error") or "") for t in rows),
              "and the stale decision was discarded")
        check(all(t["project_name"] == "alpha" for t in rows),
              "with every task still in its own project")


async def test_answers_do_not_cross_conversations():
    check.section("C7: two conversation ids keep their own history")

    with _tmp() as td:
        rt, m = await _runtime(td)
        conv_a, conv_b = uuid4(), uuid4()

        await rt._state_store.record_turn(conversation_id=conv_a,
                                          user_message="alpha question",
                                          assistant_reply="ALPHA ANSWER",
                                          follow_up_question=None,
                                          mode="smalltalk")
        await rt._state_store.record_turn(conversation_id=conv_b,
                                          user_message="beta question",
                                          assistant_reply="BETA ANSWER",
                                          follow_up_question=None,
                                          mode="smalltalk")

        a = await rt._state_store.load(conv_a)
        b = await rt._state_store.load(conv_b)
        check("ALPHA ANSWER" in a.last_assistant_replies
              and "BETA ANSWER" not in a.last_assistant_replies,
              f"conversation A holds only its own ({a.last_assistant_replies})")
        check("BETA ANSWER" in b.last_assistant_replies
              and "ALPHA ANSWER" not in b.last_assistant_replies,
              f"conversation B holds only its own ({b.last_assistant_replies})")


async def main():
    await test_two_ordinary_replies_side_by_side()
    await test_two_stories_side_by_side()
    await test_the_repeat_guard_while_another_chat_runs()
    await test_an_empty_turn_does_not_salvage_the_other_turn()
    await test_a_goal_race_runs_beside_live_chat()
    await test_answers_do_not_cross_conversations()
    check.finish()


if __name__ == "__main__":
    run(main)
