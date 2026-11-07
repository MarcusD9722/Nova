"""Every quiet handler in memory/unifier.py, and what each one says now.

Test program layer 2, item 2 (docs/TEST_PROGRAM.md). Layer 1 counted 56
`except` blocks in the unifier that neither logged nor re-raised, and no suite
had ever run 55 of them. Each one is now one of four things, and this suite
proves it by making each failure actually happen:

  * a best-effort SECTION of a larger answer -- the digital twin, the
    executive layer, the catch-up summary -- that fails is left out AND said
    (`memory_section_failed`, naming which section);
  * a stored JSON value that no longer parses falls back AND is said
    (`memory_blob_unreadable`), and where the next write would overwrite it,
    its old bytes are copied into the audit log first;
  * the nine guards for an unimportable `core.turn_identity`, which fall back
    to single-owner memory, say so every time (`memory_identity_unavailable`);
  * the rest were reviewed and are right as they are -- a parse helper whose
    answer IS the exception -- and carry `# quiet-ok: <reason>`, narrowed to
    the exception they mean.

Three bugs turned up behind them, pinned here:

  * five writers stored `json.dumps(x)[:20000]`. Past the limit that is not
    JSON, so the next read fell back to "nothing" and the next write saved
    that: the activity log and experiment trials kept resetting, a large plan
    read back as no plan, and a skill edited often enough vanished;
  * three read-modify-write paths replaced an unreadable value without a
    trace: a person's attributes, an experiment's trials, the activity log;
  * consolidation built "insights about Marcus" from every speaker's turns.

Run:  venv\\Scripts\\python.exe tests\\test_memory_handlers_2026_11.py
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
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

from harness import Checks, run  # noqa: E402

import memory.unifier as unifier_mod  # noqa: E402

check = Checks()


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        if level.startswith("_"):
            raise AttributeError(level)

        def fn(event: str, **kw: Any) -> None:
            self.events.append((level, event, kw))
        return fn

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for _lvl, name, kw in self.events if name == event]

    def wheres(self) -> list[str]:
        return [str(kw.get("where")) for kw in self.named("memory_section_failed")]

    def blobs(self) -> list[tuple[str, str]]:
        return [(str(kw.get("what")), str(kw.get("key"))) for kw in self.named("memory_blob_unreadable")]


@contextlib.contextmanager
def listening():
    rec = _Recorder()
    original = unifier_mod.logger
    unifier_mod.logger = rec
    try:
        yield rec
    finally:
        unifier_mod.logger = original


@contextlib.contextmanager
def failing(obj: Any, name: str, when=None):
    """Make `obj.name(...)` raise (only when `when(*args, **kw)` holds, if given)."""
    had = name in vars(obj)
    real = getattr(obj, name)

    async def boom(*a: Any, **kw: Any):
        if when is None or when(*a, **kw):
            raise RuntimeError(f"injected {name} failure")
        return await real(*a, **kw)

    setattr(obj, name, boom)
    try:
        yield
    finally:
        if had:
            setattr(obj, name, real)
        else:
            delattr(obj, name)


@contextlib.contextmanager
def env(**values: str):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"nova-mh-{prefix}-"))


async def _memory(root: Path):
    from memory.unifier import MemoryUnifier
    mem = MemoryUnifier(root, enable_chroma=False)
    await mem.initialize()
    return mem


def _audit(root: Path) -> list[dict[str, Any]]:
    out = []
    for path in root.rglob("audit.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


class _Expression:
    """A wired-in model whose every call fails."""
    available = True

    async def rephrase(self, *_a: Any, **_kw: Any) -> str:
        raise RuntimeError("model down")

    async def name_for(self, *_a: Any, **_kw: Any) -> str:
        raise RuntimeError("model down")

    async def read_signal(self, *_a: Any, **_kw: Any) -> str:
        raise RuntimeError("model down")


class _Chroma:
    def __init__(self, count: int = 0, query_fails: bool = False) -> None:
        self._count, self._query_fails = count, query_fails

    async def count(self) -> int:
        return self._count

    async def query(self, *_a: Any, **_kw: Any):
        if self._query_fails:
            raise RuntimeError("chroma is down")
        return []


# ── 1. nothing quiet is left unreviewed ─────────────────────────────────────

def test_nothing_quiet_is_left() -> None:
    check.section("memory/unifier.py: every quiet handler is heard or marked deliberate")
    spec = importlib.util.spec_from_file_location("cm", REPO / "tests" / "coverage_map.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cm", cm)
    spec.loader.exec_module(cm)
    src = (REPO / "memory" / "unifier.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    kinds = cm.handler_kinds(src, tree, cm.collect_log_helpers([tree]))
    check(len(kinds) >= 56, f"the classifier saw at least the 56 handlers reviewed ({len(kinds)})")
    left = sorted(line for line, kind in kinds.items() if kind in ("quiet", "logs-debug"))
    check(left == [], f"no quiet or debug-only handler is left unreviewed (lines {left[:8]})")
    check(sum(k == "deliberate" for k in kinds.values()) >= 10,
          "and the reviewed ones say why they are quiet")


# ── 2. the identity guards ──────────────────────────────────────────────────

def test_identity_guards() -> None:
    check.section("the nine identity guards: single-owner fallback, and said each time")
    u = unifier_mod
    hit = object()
    calls = [
        ("_personal_tail", lambda: u._personal_tail("Speaker:p-alice:note"), "speaker:p-alice:note"),
        ("_read_scope_entity", u._read_scope_entity, "user"),
        ("_read_scope_key", u._read_scope_key, "owner"),
        ("scoped_entity", lambda: u.scoped_entity("lesson"), "lesson"),
        ("_scope_is_owner", u._scope_is_owner, True),
        ("_turn_source_meta", u._turn_source_meta, ("typed", "")),
        ("_turn_speaker_meta", lambda: u._turn_speaker_meta("user"), ("Marcus", "user")),
        ("_scope_subject", u._scope_subject, ("Marcus", "he")),
        ("_filter_hits_for_scope", lambda: u._filter_hits_for_scope([hit]), [hit]),
    ]
    saved = sys.modules.get("core.turn_identity")
    sys.modules["core.turn_identity"] = None          # every `from ... import` fails
    try:
        for name, fn, want in calls:
            with listening() as rec:
                got = fn()
            said = rec.named("memory_identity_unavailable")
            check(got == want and len(said) == 1 and said[0].get("fallback") == "single owner",
                  f"{name}: falls back to {want!r} and says so ({len(said)} said)")
    finally:
        if saved is None:
            sys.modules.pop("core.turn_identity", None)
        else:
            sys.modules["core.turn_identity"] = saved
    with listening() as rec:
        u._read_scope_entity()
    check(not rec.events, "with the module importable, nothing is said")


# ── 3. stored values that no longer parse ───────────────────────────────────

async def test_unreadable_blobs() -> None:
    check.section("stored JSON that no longer parses: the reader falls back, and says so")
    root = _tmp("blobs")
    try:
        mem = await _memory(root)
        await mem.add_fact(entity="plan:g1", attribute="tree", value="{not json", confidence=1.0)
        await mem.add_fact(entity="exptrials:e1", attribute="data", value="[broken", confidence=1.0)
        await mem.add_fact(entity="experiment", attribute="e2", value="{bad", confidence=1.0)
        await mem.add_fact(entity="experiment", attribute="e3",
                           value=json.dumps({"id": "e3", "name": "fast replies"}), confidence=1.0)
        await mem.add_fact(entity="exptrials:e3", attribute="data", value="[cut off", confidence=1.0)
        await mem.add_fact(entity="activity", attribute="log", value="[\"a\", \"b", confidence=1.0)
        await mem.add_fact(entity="skill", attribute="s1", value="{\"id\": \"s1\"", confidence=1.0)
        await mem.add_fact(entity="skill", attribute="s2",
                           value=json.dumps({"id": "s2", "name": "no steps"}), confidence=1.0)
        await mem._sqlite.upsert_person(uuid4(), "Sarah", "{bad json")

        cases = [
            ("load_plan", lambda: mem.load_plan("g1"), None, ("plan", "g1")),
            ("get_skill", lambda: mem.get_skill("s1"), None, ("skill", "s1")),
            ("recent_activity", lambda: mem.recent_activity(), [], ("activity log", "log")),
        ]
        for label, call, want, blob in cases:
            with listening() as rec:
                got = await call()
            check(got == want and blob in rec.blobs(), f"{label}: {want!r}, and {blob} is said")

        with listening() as rec:
            verdict = await mem.analyze_experiment("e1")
        check(isinstance(verdict, dict) and ("experiment trials", "e1") in rec.blobs(),
              "analyze_experiment: judged on no trials, and the unreadable ones are said")
        with listening() as rec:
            exps = await mem.list_experiments()
        ids = sorted(e.get("id") for e in exps)
        check(ids == ["e3"] and ("experiment", "e2") in rec.blobs()
              and ("experiment trials", "e3") in rec.blobs()
              and exps[0].get("trials") == 0,
              f"list_experiments: skips the unreadable definition, counts unreadable trials as 0, says both ({ids})")
        with listening() as rec:
            skills = await mem.list_skills()
        check(skills == [] and ("skill", "s1") in rec.blobs() and ("skill", "s2") in rec.blobs(),
              "list_skills: an unreadable skill and one missing its steps are both said")
        with listening() as rec:
            person = await mem.recall_person("Sarah")
        check(person is not None and person["attributes"] == {}
              and ("person attributes", "Sarah") in rec.blobs(),
              "recall_person: the person, with no readable attributes, and said")
        with listening() as rec:
            upcoming = await mem.list_people_with_upcoming_dates(within_days=365)
        check(upcoming == [] and ("person attributes", "Sarah") in rec.blobs(),
              "list_people_with_upcoming_dates: skipped, and said (her birthday heads-up depends on it)")

        conv = uuid4()
        await mem._sqlite.add_turn(uuid4(), conv, "user", "this row has a broken time stamp",
                                   created_at_iso="garbage", speaker_entity="user",
                                   speaker_label="Marcus")
        with listening() as rec:
            await mem.episodes_for_consolidation()
        check(("episode turn time", "garbage") in rec.blobs(),
              "episodes_for_consolidation: a turn with an unreadable time is left out, and said")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_overwrites_keep_the_old_value() -> None:
    check.section("about to overwrite an unreadable value: kept in the audit log first")
    root = _tmp("overwrite")
    try:
        mem = await _memory(root)
        await mem.add_fact(entity="exptrials:e4", attribute="data", value="[cut off", confidence=1.0)
        await mem.add_fact(entity="activity", attribute="log", value="[\"x\", \"y", confidence=1.0)
        await mem._sqlite.upsert_person(uuid4(), "Sarah", "{\"relation\": \"fri")

        with env(NOVA_EXPERIMENTS="1"), listening() as rec:
            ok = await mem.add_experiment_trial("e4", "A", {"latency": 1.5})
        trials = json.loads((await mem.get_latest_fact(entity="exptrials:e4", attribute="data")).value)
        check(ok and trials == [{"variant": "A", "metrics": {"latency": 1.5}}],
              "a trial is still recorded over unreadable trials")
        check(("experiment trials", "e4") in rec.blobs(), "the unreadable trials are said")

        with listening() as rec:
            await mem.log_activity("tool:weather")
        check(await mem.recent_activity() == ["tool:weather"] and ("activity log", "log") in rec.blobs(),
              "an activity is still logged over an unreadable log, and it is said")

        with listening() as rec:
            await mem.upsert_person("Sarah", {"birthday": "09-16"})
        person = await mem.recall_person("Sarah")
        check(person is not None and person["attributes"] == {"birthday": "09-16"}
              and ("person attributes", "Sarah") in rec.blobs(),
              "a person is still updated over unreadable attributes, and it is said")

        kept = {(r.get("entity"), r.get("value")) for r in _audit(root)
                if r.get("kind") == "unreadable_overwritten"}
        check(("exptrials:e4", "[cut off") in kept and ("activity", "[\"x\", \"y") in kept
              and ("person:Sarah", "{\"relation\": \"fri") in kept,
              f"all three old values are in the audit log, byte for byte ({len(kept)} kept)")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_stored_json_is_never_cut() -> None:
    check.section("stored JSON is bounded by dropping the oldest, never by cutting the text")
    root = _tmp("bounded")
    try:
        mem = await _memory(root)
        with listening() as rec:
            for i in range(300):
                await mem.log_activity(f"event {i:03d} " + "x" * 90)
        log = await mem.recent_activity(limit=1000)
        stored = (await mem.get_latest_fact(entity="activity", attribute="log")).value
        check(len(stored) <= 20000 and json.loads(stored) == log,
              f"the activity log stays valid JSON within the limit ({len(stored)} chars)")
        check(len(log) >= 180 and log[-1].startswith("event 299"),
              f"keeping the newest events, not resetting to a handful ({len(log)} kept)")
        check(not rec.named("memory_blob_unreadable"), "and it never reads back as unreadable")

        with env(NOVA_EXPERIMENTS="1"):
            for i in range(400):
                await mem.add_experiment_trial("e5", "AB"[i % 2],
                                               {"latency": float(i), "tokens": 100.0 + i, "score": 0.5})
        trials = json.loads((await mem.get_latest_fact(entity="exptrials:e5", attribute="data")).value)
        check(len(trials) >= 150 and trials[-1]["metrics"]["latency"] == 399.0,
              f"experiment trials keep the newest within the limit ({len(trials)} kept)")

        sid = await mem.learn_skill("deploy the site", ["build it", "test it", "ship it"])
        for i in range(200):
            await mem.update_skill(sid, [f"step {j} of revision {i} " + "y" * 20 for j in range(10)])
        skill = await mem.get_skill(sid)
        check(skill is not None and skill["version"] == 201,
              f"a skill edited 200 times is still there, at version 201 ({skill and skill['version']})")
        check(skill is not None and 0 < len(skill.get("versions") or []) < 200,
              "its oldest versions were dropped to make room, not its current steps")

        big = {"goal": "move house", "steps": [{"title": f"step {i}", "notes": "z" * 200} for i in range(120)]}
        with listening() as rec:
            await mem.save_plan("g2", big)
        check(await mem.load_plan("g2") == big, "a plan over the limit is stored whole and reads back")
        check(bool(rec.named("memory_blob_oversize")), "and its size is said")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 4. best-effort sections ─────────────────────────────────────────────────

async def test_sections() -> None:
    check.section("best-effort sections: left out, and said, naming which")
    root = _tmp("sections")
    try:
        mem = await _memory(root)
        await mem.add_fact(entity="user", attribute="child", value="Liam", confidence=0.9)
        await mem.upsert_person("Sarah", {"relation": "friend"})

        async def expect(where: str, call, *, result_ok=lambda r: True) -> None:
            with listening() as rec:
                try:
                    result = await call()
                    raised = ""
                except Exception as e:  # noqa: BLE001
                    result, raised = None, f"{type(e).__name__}: {e}"
            check(not raised and result_ok(result) and where in rec.wheres(),
                  f"{where}: the rest still answers, and the failure is said "
                  f"({raised or rec.wheres()})")

        mem._known_names_cache = None
        with failing(mem._sqlite, "all_people"):
            await expect("known_person_names.people", mem.known_person_names,
                         result_ok=lambda r: r == ["Liam"])
        mem._known_names_cache = None
        with failing(mem, "get_facts"):
            await expect("known_person_names.facts", mem.known_person_names,
                         result_ok=lambda r: r == ["Sarah"])
        mem._known_names_cache = None

        with failing(mem._sqlite, "search_turns"):
            await expect("recall_person.last_mentioned", lambda: mem.recall_person("Sarah"),
                         result_ok=lambda r: r is not None and r["last_mentioned"] is None)

        conv = uuid4()
        with failing(mem, "get_latest_fact", when=lambda *a, **kw: kw.get("entity") == "projects"):
            await expect("ingest_turn.active_project",
                         lambda: mem.ingest_turn(conv, "user", "I talked to Sarah about the cabin today."))

        rebuilds: list[int] = []

        async def _no_rebuild() -> dict[str, Any]:
            rebuilds.append(1)
            return {}

        fresh = await _memory(root / "empty")
        fresh._chroma = _Chroma(count=0)
        fresh.rebuild_semantic_index = _no_rebuild
        with failing(fresh._sqlite, "all_turns"):
            await expect("semantic_index.count_turns", fresh._ensure_semantic_index)
        check(rebuilds == [], "and an index that cannot be counted is not rebuilt on a guess")

        with env(NOVA_DIGITAL_TWIN="1"):
            for where, obj, name, when in (
                ("digital_twin.turn_hours", mem._sqlite, "search_turns", None),
                ("digital_twin.trends", mem, "recent_mood_trend", None),
                ("digital_twin.interests", mem, "get_facts",
                 lambda *a, **kw: kw.get("entity") == "interest_focus"),
                ("digital_twin.lessons", mem, "get_facts",
                 lambda *a, **kw: kw.get("entity") == unifier_mod.LESSON_ENTITY),
                ("digital_twin.reminder_punctuality", mem, "list_reminders", None),
                ("digital_twin.goal_progress", mem, "list_goals", None),
            ):
                with failing(obj, name, when=when):
                    await expect(where, mem.digital_twin_profile,
                                 result_ok=lambda r: isinstance(r, dict) and r.get("enabled"))

            real_derive = unifier_mod.derive_profile

            def _enough(inputs):
                prof = real_derive(inputs)
                prof["enough_data"] = True
                prof.setdefault("stress_level", {"value": "low", "basis": "test"})
                return prof

            unifier_mod.derive_profile = _enough
            mem._expression = _Expression()

            async def _mood(days: int = 5) -> str:
                return "a bit stressed this week"

            mem.recent_mood_trend = _mood
            try:
                await expect("digital_twin.stress_read", mem.digital_twin_profile,
                             result_ok=lambda r: r["stress_level"]["value"] == "low")
            finally:
                unifier_mod.derive_profile = real_derive
                mem._expression = None
                del mem.recent_mood_trend

        for where, name in (("executive.reminders", "list_reminders"),
                            ("executive.important_dates", "list_people_with_upcoming_dates"),
                            ("executive.goals", "list_goals"),
                            ("executive.twin_signals", "digital_twin_profile")):
            mem._exec_cache = None
            with failing(mem, name):
                await expect(where, mem._gather_executive, result_ok=lambda r: isinstance(r, list))

        mem._expression = _Expression()
        recs = [{"message": "You have three goals with no progress this week.", "kind": "goal"}]
        await expect("executive.rephrase", lambda: mem._phrase_recommendations(recs),
                     result_ok=lambda r: r[0]["message"] == recs[0]["message"])
        real_detect = unifier_mod.detect_repeated_workflow
        unifier_mod.detect_repeated_workflow = lambda *_a, **_kw: {"steps": ["open the report", "email it"]}
        try:
            await expect("skills.name_for", mem.detect_learnable_workflow,
                         result_ok=lambda r: r is not None and r["steps"] == ["open the report", "email it"])
        finally:
            unifier_mod.detect_repeated_workflow = real_detect
            mem._expression = None

        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for where, name in (("catchup.documents", "list_indexed_documents"),
                            ("catchup.reminders", "list_reminders"),
                            ("catchup.goals", "list_goals"),
                            ("catchup.trend", "recent_mood_trend")):
            with failing(mem, name):
                await expect(where, lambda: mem.build_catchup_summary(since),
                             result_ok=lambda r: isinstance(r, str))

        with failing(mem, "list_goals"):
            await expect("work_state", mem.describe_work_state, result_ok=lambda r: r == "")

        mem._chroma = _Chroma(query_fails=True)
        try:
            await expect("documents.semantic", lambda: mem.search_document_chunks_broad("cabin"),
                         result_ok=lambda r: isinstance(r, list))
        finally:
            mem._chroma = None
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 5. consolidation reads the current speaker ──────────────────────────────

async def test_consolidation_reads_the_current_speaker() -> None:
    check.section("episodes_for_consolidation: insights about Marcus come from Marcus")
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity, active_turn

    class _P:
        role = "guest"
    alice = TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice", attempted=True),
        profile=_P())

    root = _tmp("episodes")
    try:
        mem = await _memory(root)
        conv = uuid4()
        await mem.ingest_turn(conv, "user", "I finally finished the deck for Thursday's pitch.")
        with active_turn(alice):
            await mem.ingest_turn(conv, "user", "I always work night shifts at the hospital.")
        mine = await mem.episodes_for_consolidation()
        text = " ".join(m for day in mine for m in day["messages"])
        check("deck for Thursday" in text and "night shifts" not in text,
              "Marcus's episodes hold his messages and not a guest's")
        with active_turn(alice):
            hers = await mem.episodes_for_consolidation()
        text = " ".join(m for day in hers for m in day["messages"])
        check("night shifts" in text and "deck" not in text, "and Alice's hold hers")
        with active_turn(TurnIdentity.voice_unverified()):
            nobody = await mem.episodes_for_consolidation()
        check(nobody == [], "an unidentified voice gets none")

        # The digital twin's active hours are HIS. Backdate the guest's turn to
        # a distinctive hour and make sure it does not show up in his profile.
        import aiosqlite
        # Twelve hours from NOW, so it can never be the hour his own turn
        # landed in, whatever time the suite runs.
        local_now = datetime.now().astimezone()
        odd_hour = (local_now.hour + 12) % 24
        guest_at = local_now.replace(hour=odd_hour, minute=33) - timedelta(days=1)
        async with aiosqlite.connect(mem._sqlite._db_path) as db:
            await db.execute("UPDATE turns SET created_at=? WHERE speaker_entity=?",
                             (guest_at.astimezone(timezone.utc).isoformat(),
                              str(alice.memory_entity)))
            await db.commit()
        captured: list[list[int]] = []
        real_derive = unifier_mod.derive_profile

        def _capture(inputs):
            captured.append(list(inputs.turn_hours))
            return real_derive(inputs)

        unifier_mod.derive_profile = _capture
        try:
            with env(NOVA_DIGITAL_TWIN="1"):
                await mem.digital_twin_profile()
        finally:
            unifier_mod.derive_profile = real_derive
        his_hours = {datetime.fromisoformat(r["created_at"]).astimezone().hour
                     for r in await mem._sqlite.search_turns(limit=50, speaker_entity="user")}
        check(bool(captured) and set(captured[0]) == his_hours and odd_hour not in captured[0],
              f"the digital twin's active hours come from Marcus's turns only "
              f"({captured[:1]} vs his {sorted(his_hours)}, guest at {odd_hour}:33)")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    test_nothing_quiet_is_left()
    test_identity_guards()
    await test_unreadable_blobs()
    await test_overwrites_keep_the_old_value()
    await test_stored_json_is_never_cut()
    await test_sections()
    await test_consolidation_reads_the_current_speaker()
    check.finish()


if __name__ == "__main__":
    run(main)
