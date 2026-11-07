"""Memory functions no suite ran, and what each was written to do.

Test program layer 2, item 2 (docs/TEST_PROGRAM.md). Layer 1's coverage map
found whole behaviours of memory that nothing executed. This suite proves what
each one is MEANT to do, taken from its docstring and from what Marcus asked
for, never from what it currently does:

  forget         deletes exactly what the speaker named and nothing else:
                 their own memory only, every meaningful word matched,
                 bookkeeping untouched, capped, and the reply told what went;
  recent turns   the reflection pass reads the current speaker's exchanges,
                 never a guest's words relabelled as Marcus's;
  reverification directly-observed facts gone stale, oldest first;
  reminders      created, listed, due, moved, completed and cancelled, each
                 answering truthfully whether it did anything;
  supersession   a new value replaces an old one, or nothing changes:
                 never both lost;
  ingest         one fact that fails to save never takes the others with it.

Five of these were broken, and each is pinned here:

  * forget matched nothing on its own docstring's request, "Please forget my
    locker code, I don't want you storing that", because "please", "forget",
    "want" and "storing" all had to appear in the stored fact;
  * the owner's forget also deleted a GUEST's matching fact;
  * replacing a single-valued fact deleted the old value before writing the
    new one, so a failed write left neither;
  * reflection read every speaker's turns as Marcus's, and wrote lessons about
    him from them;
  * reminder.list applied its limit before hiding Nova's bookkeeping
    reminders, so his could be cut off under "These are the only reminders
    that exist", or answered "Nothing is scheduled" while things were.

Run:  venv\\Scripts\\python.exe tests\\test_memory_functions_2026_11.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, ScriptedLLM, run  # noqa: E402

import core.workers.memory_ingest as ingest_mod  # noqa: E402
import core.workers.reminder_worker as reminder_worker_mod  # noqa: E402
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
    return Path(tempfile.mkdtemp(prefix=f"nova-mf-{prefix}-"))


def _guest(pid: str = "p-alice", name: str = "Alice"):
    """A known non-owner speaker, built the way the speaker suites build one."""
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        role = "guest"
    return TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id=pid, display_name=name, attempted=True),
        profile=_P())


async def _memory(root: Path):
    from memory.unifier import MemoryUnifier
    mem = MemoryUnifier(root, enable_chroma=False)
    await mem.initialize()
    return mem


async def _values(mem, entity: str, attribute: str) -> list[str]:
    return sorted(f.value for f in await mem.get_facts(entity=entity, attribute=attribute, limit=50))


# ── forget ──────────────────────────────────────────────────────────────────

class _FailingChroma:
    async def delete_ids(self, ids: list[str]) -> None:
        raise RuntimeError("chroma is down")


async def test_forget() -> None:
    check.section("forget: exactly what was named, the speaker's own, and said")
    from core.turn_identity import TurnIdentity, active_turn

    root = _tmp("forget")
    try:
        mem = await _memory(root)
        alice = _guest()
        alice_root = alice.memory_entity
        check(bool(alice_root) and alice_root.startswith("speaker:"),
              f"the guest helper gives Alice her own namespace ({alice_root})")

        async def seed() -> None:
            await mem.add_fact(entity="user", attribute="locker_code", value="4417", confidence=0.9)
            await mem.add_fact(entity="user", attribute="gym_locker_location", value="second floor", confidence=0.9)
            await mem.add_fact(entity="user", attribute="wifi_code", value="hunter2", confidence=0.9)
            await mem.add_fact(entity="user", attribute="favorite_color", value="teal", confidence=0.9)
            await mem.add_fact(entity=alice_root, attribute="locker_code", value="9999", confidence=0.9)
            # The P5.1d child shape, `lesson:speaker:p-alice`: still hers.
            await mem.add_fact(entity=f"lesson:{alice_root}", attribute="locker_code_note",
                               value="ask before reading it out", confidence=0.9)
            await mem.add_fact(entity="executive", attribute="locker_code_check", value="due", confidence=0.9)

        await seed()
        out = await mem.forget("locker code", dry_run=True)
        check(out.get("ok") and out.get("dry_run") and out.get("would_delete") == 1
              and out.get("deleted") == 0,
              f"dry run: says what WOULD go, deletes nothing ({out.get('would_delete')})")
        check(await _values(mem, "user", "locker_code") == ["4417"], "and the fact is still there")

        out = await mem.forget("locker code")
        items = out.get("items") or []
        check(out.get("ok") and out.get("deleted") == 1
              and [(i["attribute"], i["value"]) for i in items] == [("locker_code", "4417")],
              f"the owner's forget removes exactly his locker code ({[(i['attribute'], i['value']) for i in items]})")
        check(await _values(mem, "user", "locker_code") == [], "it is gone from memory")
        check(await _values(mem, "user", "gym_locker_location") == ["second floor"]
              and await _values(mem, "user", "wifi_code") == ["hunter2"],
              "a fact matching only SOME of the words stays (locker without code, code without locker)")
        check(await _values(mem, alice_root, "locker_code") == ["9999"],
              "Alice's locker code is not his to forget")
        check(await _values(mem, f"lesson:{alice_root}", "locker_code_note") == ["ask before reading it out"],
              "nor is anything under the older shape of her namespace")
        check(await _values(mem, "executive", "locker_code_check") == ["due"],
              "and Nova's own bookkeeping is never a candidate")

        await mem.add_fact(entity="user", attribute="locker_code", value="4417", confidence=0.9)
        out = await mem.forget("Please forget my locker code, I don't want you storing that")
        check(out.get("deleted") == 1 and await _values(mem, "user", "locker_code") == [],
              f"the request from forget's own docstring, said in full, works ({out.get('deleted')} deleted)")

        for vague in ("forget it", "please forget that", ""):
            out = await mem.forget(vague)
            check(out.get("ok") is False and out.get("error") == "too_vague",
                  f"{vague!r}: refused as too vague, nothing deleted on a guess")
        out = await mem.forget("passport number")
        check(out.get("ok") and out.get("deleted") == 0 and "couldn't find" in str(out.get("detail")),
              "nothing matching: says so plainly, deletes nothing")

        with active_turn(TurnIdentity.voice_unverified()):
            out = await mem.forget("favorite color")
        check(out.get("error") == "unverified_speaker"
              and await _values(mem, "user", "favorite_color") == ["teal"],
              "an unidentified voice cannot delete anything")

        await mem.add_fact(entity="user", attribute="locker_code", value="4417", confidence=0.9)
        with active_turn(alice):
            out = await mem.forget("locker code")
        check(out.get("deleted") == 2 and await _values(mem, alice_root, "locker_code") == []
              and await _values(mem, f"lesson:{alice_root}", "locker_code_note") == []
              and await _values(mem, "user", "locker_code") == ["4417"],
              f"Alice's forget removes HER locker code and her older-shape note, and leaves "
              f"Marcus's ({out.get('deleted')} deleted)")

        for i in range(5):
            await mem.add_fact(entity="user", attribute=f"parking_spot_{i}", value=f"level {i}", confidence=0.9)
        out = await mem.forget("parking spot", limit=3)
        left = [a for a in (f"parking_spot_{i}" for i in range(5)) if await _values(mem, "user", a)]
        check(out.get("deleted") == 3 and len(left) == 2,
              f"capped: limit=3 deletes three of five ({out.get('deleted')} deleted, {len(left)} left)")

        audit = (root / "json" / "audit.jsonl")
        if not audit.exists():
            audit = next(iter(root.rglob("audit.jsonl")), audit)
        text = audit.read_text(encoding="utf-8") if audit.exists() else ""
        check('"op":"forget"' in text and "locker" in text,
              "every deletion is written to the audit log")

        mem._chroma = _FailingChroma()
        try:
            with listening(unifier_mod) as rec:
                out = await mem.forget("favorite color")
        finally:
            mem._chroma = None
        ev = rec.named("semantic_delete_failed")
        check(out.get("deleted") == 1 and await _values(mem, "user", "favorite_color") == [],
              "with the semantic index down, the fact is still deleted")
        check(len(ev) == 1 and ev[0][1].get("where") == "forget",
              "and the copy left in the semantic index is said, not hidden")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_forget_through_the_tool() -> None:
    check.section("memory.forget: the tool the model calls")
    from core.tooling import build_tool_router

    root = _tmp("forget-tool")
    try:
        mem = await _memory(root / "memory")
        (root / "projects").mkdir()
        router = build_tool_router(repo_root=root, projects_dir=root / "projects", memory=mem)
        await mem.add_fact(entity="user", attribute="locker_code", value="4417", confidence=0.9)
        tool = router._tools["memory.forget"]
        out = await tool({"what": ""})
        check(out.get("error") == "missing_what", "no subject: asks which thing")
        out = await tool({"what": "forget my locker code", "dry_run": True})
        check(out.get("would_delete") == 1 and await _values(mem, "user", "locker_code") == ["4417"],
              "the tool's own example phrasing finds it, and preview deletes nothing")
        out = await tool({"what": "forget my locker code"})
        check(out.get("deleted") == 1 and out["items"][0]["value"] == "4417",
              "and deleting returns what went, so the reply can say exactly that")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── recent turns, for reflection ────────────────────────────────────────────

async def test_recent_turns() -> None:
    check.section("recent_turns_text: the current speaker's exchanges only")
    from core.turn_identity import TurnIdentity, active_turn

    root = _tmp("turns")
    try:
        mem = await _memory(root)
        conv = uuid4()
        await mem.ingest_turn(conv, "user", "I prefer short answers from now on.")
        await mem.ingest_turn(conv, "assistant", "Got it, short answers.")
        with active_turn(_guest()):
            await mem.ingest_turn(conv, "user", "Stop being so chatty with me.")
            await mem.ingest_turn(conv, "assistant", "Okay Alice, I'll keep it brief.")
        with active_turn(TurnIdentity.voice_unverified()):
            await mem.ingest_turn(conv, "user", "Who is this talking?")
        await mem.ingest_turn(conv, "user", "   ")
        await mem.ingest_turn(conv, "user", "Thanks, that helps.")

        owner = await mem.recent_turns_text(limit=30)
        lines = owner.splitlines()
        check(lines == ["Marcus: I prefer short answers from now on.",
                        "Nova: Got it, short answers.",
                        "Marcus: Thanks, that helps."],
              f"the owner's transcript: his exchanges, in order, blanks dropped ({lines})")
        check("chatty" not in owner and "Alice" not in owner and "Who is this" not in owner,
              "a guest's words and an unidentified voice never appear as Marcus's")

        with active_turn(_guest()):
            hers = await mem.recent_turns_text(limit=30)
        check(hers.splitlines() == ["Alice: Stop being so chatty with me.",
                                    "Nova: Okay Alice, I'll keep it brief."],
              f"Alice's transcript is hers, labelled as hers ({hers.splitlines()})")
        with active_turn(TurnIdentity.voice_unverified()):
            nobody = await mem.recent_turns_text(limit=30)
        check(nobody == "", "an unidentified voice reads nothing")

        for i in range(12):
            await mem.ingest_turn(conv, "user", f"message number {i:02d}")
        last5 = (await mem.recent_turns_text(limit=5)).splitlines()
        check(last5 == [f"Marcus: message number {i:02d}" for i in range(7, 12)],
              f"limit keeps the most recent, still in order ({last5[:1]}...{last5[-1:]})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_reflection_reads_only_marcus() -> None:
    check.section("the reflection pass: it learns lessons about Marcus from Marcus")
    from core.turn_identity import active_turn
    from core.workers.self_improve import SelfImproveWorker

    root = _tmp("reflect")
    try:
        mem = await _memory(root)
        conv = uuid4()
        with active_turn(_guest()):
            for i in range(6):
                await mem.ingest_turn(conv, "user", f"Stop being so chatty, I said it {i} times already.")
                await mem.ingest_turn(conv, "assistant", "Okay Alice, I'll keep it brief from now on.")
        await mem.ingest_turn(conv, "user", "What's the weather like?")
        worker = SelfImproveWorker.__new__(SelfImproveWorker)
        worker._memory = mem
        transcript = await worker._recent_transcript()
        check("chatty" not in transcript and "Alice" not in transcript,
              f"the transcript reflection reads has no guest in it ({len(transcript)} chars)")
        check(len(transcript) < 200,
              "so the guest's twelve turns cannot push it over the reflection threshold")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── facts needing re-verification ───────────────────────────────────────────

async def _age(mem, fact_id, days: float, *, confirmed: bool = True) -> None:
    import aiosqlite
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(mem._sqlite._db_path) as db:
        if confirmed:
            await db.execute("UPDATE facts SET last_confirmed_at=?, created_at=? WHERE id=?",
                             (when, when, str(fact_id)))
        else:
            await db.execute("UPDATE facts SET last_confirmed_at=NULL, created_at=? WHERE id=?",
                             (when, str(fact_id)))
        await db.commit()


async def test_reverification() -> None:
    check.section("facts_needing_reverification: observed facts gone stale, oldest first")
    root = _tmp("reverify")
    saved = os.environ.get("NOVA_MEMORY_PROVENANCE")
    os.environ["NOVA_MEMORY_PROVENANCE"] = "1"
    try:
        mem = await _memory(root)
        old = await mem.add_fact(entity="user", attribute="job", value="welder", confidence=0.9,
                                 source="user", verification_status="stated")
        older = await mem.add_fact(entity="user", attribute="hometown", value="Tulsa", confidence=0.9,
                                   source="user", verification_status="stated")
        fresh = await mem.add_fact(entity="user", attribute="vehicle", value="truck", confidence=0.9,
                                   source="user", verification_status="stated")
        guess = await mem.add_fact(entity="user", attribute="employer", value="Acme", confidence=0.6,
                                   source="inferred", verification_status="inferred")
        await _age(mem, old, 200)
        await _age(mem, older, 400)
        await _age(mem, fresh, 10)
        await _age(mem, guess, 500, confirmed=False)

        rows = await mem.facts_needing_reverification(older_than_days=180)
        got = [r["attribute"] for r in rows]
        check(got == ["hometown", "job"],
              f"stated facts unconfirmed for 180+ days, oldest first; fresh ones and guesses left out ({got})")
        check([r["attribute"] for r in await mem.facts_needing_reverification(older_than_days=180, limit=1)]
              == ["hometown"], "limit keeps the oldest")
        await mem.confirm_fact(str(older))
        got2 = [r["attribute"] for r in await mem.facts_needing_reverification(older_than_days=180)]
        check(got2 == ["job"], f"confirming a fact takes it off the queue ({got2})")
    finally:
        if saved is None:
            os.environ.pop("NOVA_MEMORY_PROVENANCE", None)
        else:
            os.environ["NOVA_MEMORY_PROVENANCE"] = saved
        shutil.rmtree(root, ignore_errors=True)


# ── reminders ───────────────────────────────────────────────────────────────

async def test_reminders() -> None:
    check.section("reminders: each step answers truthfully whether it did anything")
    root = _tmp("reminders")
    try:
        mem = await _memory(root)
        now = datetime.now(timezone.utc)
        past = await mem.create_reminder(title="call the dentist",
                                         due_at_iso=(now - timedelta(minutes=5)).isoformat())
        future = await mem.create_reminder(title="water the plants",
                                           due_at_iso=(now + timedelta(hours=3)).isoformat())
        # Due in ONE hour, written in Marcus's own offset. As text, the -06:00
        # form sorts BEFORE "now" in UTC, so it fired an hour early unless the
        # time is stored as UTC.
        local = (now + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-6)))
        offset = await mem.create_reminder(title="take the pizza out", due_at_iso=local.isoformat())

        due = [r["title"] for r in await mem.due_reminders()]
        check(due == ["call the dentist"],
              f"only what is due now is due: not the 3-hour one, not the 1-hour one in -06:00 ({due})")
        pending = await mem.list_reminders(status="pending")
        stored = {r["title"]: r["due_at"] for r in pending}
        check(stored.get("take the pizza out", "").endswith("+00:00"),
              f"a time given with an offset is stored as UTC ({stored.get('take the pizza out')})")
        check([r["title"] for r in pending] == ["call the dentist", "take the pizza out", "water the plants"],
              "and the list sorts by the real time")

        for bad in ("tomorrow", "2026-11-07T09:00:00", ""):
            raised = ""
            try:
                await mem.create_reminder(title="bad", due_at_iso=bad)
            except ValueError as e:
                raised = str(e)
            check(bool(raised), f"{bad!r}: refused up front, never stored to silently not fire ({raised[:50]})")
        check(not [r for r in await mem.list_reminders(status=None) if r["title"] == "bad"],
              "none of them was stored")

        moved = await mem.reschedule_reminder(reminder_id=str(past),
                                              next_due_at_iso=(now + timedelta(days=1)).isoformat())
        check(moved is True and "call the dentist" not in [r["title"] for r in await mem.due_reminders()],
              "reschedule moves it, and says it did")
        check(await mem.reschedule_reminder(reminder_id=str(uuid4()),
                                            next_due_at_iso=now.isoformat()) is False,
              "reschedule of a reminder that does not exist says False")
        later = (now + timedelta(days=2)).astimezone(timezone(timedelta(hours=-6)))
        await mem.reschedule_reminder(reminder_id=str(past), next_due_at_iso=later.isoformat())
        moved_to = {r["title"]: r["due_at"] for r in await mem.list_reminders(status="pending")}
        check(moved_to.get("call the dentist", "").endswith("+00:00"),
              f"a new time given with an offset is stored as UTC ({moved_to.get('call the dentist')})")
        raised = ""
        try:
            await mem.reschedule_reminder(reminder_id=str(past), next_due_at_iso="next week")
        except ValueError as e:
            raised = str(e)
        check(bool(raised), "and a new time that is not a time is refused")

        await mem.complete_reminder(reminder_id=str(future))
        fired = [r["title"] for r in await mem.list_reminders(status="fired")]
        check(fired == ["water the plants"], "completing marks it fired")
        check(await mem.cancel_reminder(reminder_id=str(offset)) is True, "cancelling a pending one: True")
        check(await mem.cancel_reminder(reminder_id=str(offset)) is False, "cancelling it again: False")
        check(await mem.cancel_reminder(reminder_id=str(future)) is False, "cancelling a fired one: False")
        check(await mem.cancel_reminder(reminder_id=str(uuid4())) is False, "cancelling a made-up id: False")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_reminder_tools() -> None:
    check.section("reminder.list / reminder.cancel: what the model is told")
    from core.tooling import build_tool_router

    root = _tmp("reminder-tools")
    try:
        mem = await _memory(root / "memory")
        (root / "projects").mkdir()
        router = build_tool_router(repo_root=root, projects_dir=root / "projects", memory=mem)
        listing, cancel = router._tools["reminder.list"], router._tools["reminder.cancel"]
        now = datetime.now(timezone.utc)

        out = await listing({})
        check(out.get("count") == 0 and "Nothing is scheduled" in str(out.get("note")),
              "nothing set: says so")

        # Nova's own bookkeeping, due BEFORE anything of his.
        for i in range(25):
            await mem.create_reminder(title=f"__nova_birthday_friend{i}_2026-12-01__",
                                      due_at_iso=(now + timedelta(minutes=10 + i)).isoformat())
        mine = []
        for i in range(3):
            mine.append(await mem.create_reminder(title=f"his reminder {i}",
                                                  due_at_iso=(now + timedelta(days=2, hours=i)).isoformat()))
        out = await listing({})
        titles = [r["title"] for r in out.get("reminders") or []]
        check(titles == ["his reminder 0", "his reminder 1", "his reminder 2"],
              f"his reminders are listed even behind 25 of Nova's own ({titles})")
        check(out.get("count") == 3 and "only reminders" in str(out.get("note")),
              "and only then is 'these are the only reminders' said")

        out = await listing({"limit": 2})
        check(out.get("count") == 2 and out.get("total") == 3 and "Say that there are more" in str(out.get("note")),
              f"a cut-short list says there are more ({out.get('count')} of {out.get('total')})")

        rid = str(mine[0])
        out = await cancel({"reminder_id": rid})
        check(out.get("ok") and out.get("cancelled"), "cancel by id works")
        out = await cancel({"reminder_id": rid})
        check(out.get("ok") is False and out.get("error") == "no_such_reminder",
              "and a second cancel says nothing was cancelled")
        out = await cancel({})
        check(out.get("error") == "missing_reminder_id", "no id: asks for one")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_reminder_worker_reschedule() -> None:
    check.section("the reminder worker: a reschedule that moved nothing is said")
    from core.workers.reminder_worker import ReminderWorker

    class _Memory:
        def __init__(self, rows):
            self.rows = rows
            self.completed: list[str] = []

        async def due_reminders(self, *, limit: int = 20):
            return self.rows

        async def reschedule_reminder(self, *, reminder_id: str, next_due_at_iso: str) -> bool:
            return False

        async def complete_reminder(self, *, reminder_id: str) -> None:
            self.completed.append(reminder_id)

    now = datetime.now(timezone.utc)
    rows = [{"reminder_id": "r-on-time", "title": "stretch", "details": "stretch",
             "due_at": (now - timedelta(seconds=5)).isoformat(), "recurrence": "daily"},
            {"reminder_id": "r-missed", "title": "stand up", "details": "stand up",
             "due_at": (now - timedelta(days=2)).isoformat(), "recurrence": "daily"}]
    worker = ReminderWorker.__new__(ReminderWorker)
    worker._memory = _Memory(rows)
    worker._briefing_time = ""
    with listening(reminder_worker_mod) as rec:
        try:
            await worker._tick()
            raised = ""
        except Exception as e:  # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
    missed = sorted(kw.get("reminder_id") for _l, kw in rec.named("reminder_reschedule_missed"))
    check(not raised and missed == ["r-missed", "r-on-time"],
          f"both reschedule paths report a reschedule that matched no reminder ({raised or missed})")


# ── supersession ────────────────────────────────────────────────────────────

async def test_supersession() -> None:
    check.section("add_fact: a new value replaces the old one, or nothing changes")
    root = _tmp("supersede")
    try:
        mem = await _memory(root)
        await mem.add_fact(entity="user", attribute="favorite_color", value="teal", confidence=0.9)
        await mem.add_fact(entity="user", attribute="favorite_color", value="green", confidence=0.9)
        check(await _values(mem, "user", "favorite_color") == ["green"],
              "a single-valued fact is replaced, not stacked")

        real_add = mem._sqlite.add_fact

        async def _locked(*_a: Any, **_kw: Any):
            raise RuntimeError("database is locked")

        mem._sqlite.add_fact = _locked
        raised = ""
        try:
            await mem.add_fact(entity="user", attribute="favorite_color", value="blue", confidence=0.9)
        except RuntimeError as e:
            raised = str(e)
        mem._sqlite.add_fact = real_add
        latest = await mem.get_latest_fact(entity="user", attribute="favorite_color")
        check(raised == "database is locked", "a write that fails still raises to its caller")
        check(latest is not None and latest.value == "green",
              f"and the old value SURVIVES it ({latest.value if latest else 'nothing left'})")

        real_delete = mem._sqlite.delete_facts_by_ids

        async def _no_delete(_ids):
            raise RuntimeError("delete failed")

        mem._sqlite.delete_facts_by_ids = _no_delete
        with listening(unifier_mod) as rec:
            await mem.add_fact(entity="user", attribute="favorite_color", value="red", confidence=0.9)
        mem._sqlite.delete_facts_by_ids = real_delete
        latest = await mem.get_latest_fact(entity="user", attribute="favorite_color")
        check(latest is not None and latest.value == "red",
              "if retiring the old value fails, the new one is still stored and read first")
        check(len(rec.named("fact_supersede_failed")) == 1, "and the leftover is said")
        await mem.add_fact(entity="user", attribute="favorite_color", value="purple", confidence=0.9)
        check(await _values(mem, "user", "favorite_color") == ["purple"],
              "the next write retires every older value")

        await mem.add_fact(entity="user", attribute="friend", value="Sam", confidence=0.8)
        await mem.add_fact(entity="user", attribute="friend", value="Sam", confidence=0.8)
        await mem.add_fact(entity="user", attribute="friend", value="Jo", confidence=0.8)
        check(await _values(mem, "user", "friend") == ["Jo", "Sam"],
              "a list-valued fact keeps each value once: a repeat reinforces, a new one adds")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── the ingest worker's extractor loop ──────────────────────────────────────

async def test_ingest_one_fact_at_a_time() -> None:
    check.section("ingest: one fact that fails to save never takes the others")
    from core.conversation_state import ConversationStateStore
    from core.events import MemoryIngestEvent
    from core.policy.memory_extractor import MemoryExtractorLLM
    from core.policy.summarizer import SummarizerLLM
    from core.turn_identity import TurnIdentity
    from core.workers.memory_ingest import MemoryIngestWorker
    from memory.backends.diskcache_backend import DiskCacheBackend

    root = _tmp("ingest")
    cache = DiskCacheBackend(root / "dc")
    try:
        mem = await _memory(root)
        llm = ScriptedLLM()
        llm.default_reply = ('{"facts":['
                             '{"entity":"user","attribute":"job","value":"welder","confidence":0.9,"persist":true},'
                             '{"entity":"user","attribute":"hometown","value":"Tulsa","confidence":0.9,"persist":true},'
                             '{"entity":"user","attribute":"vehicle","value":"truck","confidence":0.9,"persist":true}]}')
        sem = asyncio.Semaphore(1)
        worker = MemoryIngestWorker(
            memory=mem, extractor=MemoryExtractorLLM(llm, llm_semaphore=sem),
            summarizer=SummarizerLLM(llm, llm_semaphore=sem),
            state=ConversationStateStore(cache), queue=asyncio.Queue(),
            summarize_queue=asyncio.Queue(), summary_every_n=50)
        real_add = mem.add_fact

        async def _flaky(**kw: Any):
            if kw.get("attribute") == "hometown":
                raise RuntimeError("database is locked")
            return await real_add(**kw)

        mem.add_fact = _flaky
        ev = MemoryIngestEvent(conversation_id=uuid4(),
                               user_message="I'm a welder from Tulsa and I drive a truck.",
                               assistant_message="", timestamp=datetime.now(timezone.utc))
        raised = ""
        with listening(ingest_mod) as rec:
            try:
                await worker._ingest_scoped(ev, TurnIdentity.typed())
            except Exception as e:  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
        mem.add_fact = real_add
        check(not raised, f"the event completes ({raised or 'no raise'})")
        check(await _values(mem, "user", "job") == ["welder"]
              and await _values(mem, "user", "vehicle") == ["truck"],
              "the facts before AND after the failed one are saved")
        failed = rec.named("memory_fact_write_failed")
        check(len(failed) == 1 and failed[0][0] == "exception"
              and failed[0][1].get("attribute") == "hometown",
              "and the one that failed is reported by name, at error level")
    finally:
        await cache.close()
        shutil.rmtree(root, ignore_errors=True)


async def test_reconcile_says_why_it_skipped() -> None:
    check.section("_reconcile: best-effort, but never silently")
    from core.workers.memory_ingest import MemoryIngestWorker

    class _Mem:
        def __init__(self, exc=None, candidates=None):
            self.exc, self.candidates = exc, candidates or []

        async def find_conflict_candidates(self, *, entity: str, value: str):
            if self.exc:
                raise self.exc
            return self.candidates

    class _Cand:
        def __init__(self, value):
            self.value = value
            self.id = uuid4()

    worker = MemoryIngestWorker.__new__(MemoryIngestWorker)
    worker._memory = _Mem(exc=RuntimeError("search index busy"))
    with listening(ingest_mod) as rec:
        await worker._reconcile(entity="user", attribute="hobby", value="I hate running")
    check("search index busy" in str((rec.named("reconcile_candidates_failed") or [("", {})])[0][1].get("error")),
          "a failed candidate lookup is said")

    worker._memory = _Mem(candidates=[_Cand("I love running")])

    async def _judge(_prompt: str) -> str:
        return "I think they contradict, probably."

    worker._llm_judge = _judge
    with listening(ingest_mod) as rec:
        await worker._reconcile(entity="user", attribute="hobby", value="I hate running")
    check(len(rec.named("reconcile_judge_unparseable")) == 1,
          "a verdict that cannot be read is said, not taken as 'nothing contradicts'")


async def main() -> None:
    await test_forget()
    await test_forget_through_the_tool()
    await test_recent_turns()
    await test_reflection_reads_only_marcus()
    await test_reverification()
    await test_reminders()
    await test_reminder_tools()
    await test_reminder_worker_reschedule()
    await test_supersession()
    await test_ingest_one_fact_at_a_time()
    await test_reconcile_says_why_it_skipped()
    check.finish()


if __name__ == "__main__":
    run(main)
