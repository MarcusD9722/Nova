"""A database written by an older Nova, opened by this one (Stage 13C §16).

A restart is also how a machine picks up new CODE. The durable store on disk
was written by a Nova that had never heard of execution outcomes, of goal
generations, or of which run a progress line belonged to - and it is full of
rows describing work that a person actually asked for.

The temptation with old data is to make it look modern: fill in the missing
columns with the best current guess. That is the one thing that must not
happen. A legacy progress line stamped with the goal's CURRENT generation
would be a fabricated claim about which run it came from, and it would be
indistinguishable from a real one.

So the contract is narrow and awkward on purpose:

  * what the old row already IMPLIES may be filled in (a `done` row succeeded);
  * what it does not imply stays NULL, is still shown, and is labelled as
    unknown rather than guessed;
  * old in-flight rows are subject to the same restart truth as new ones -
    nothing that was `running` in a process that is gone may claim to have
    finished;
  * and opening the same database twice must not change it the second time.

The old schema here is built column by column, not by deleting columns from
the current one, so this test keeps failing if the migration silently starts
depending on something that old databases never had.

Run:  venv\\Scripts\\python.exe tests\\test_migration_s13c.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "1800")

from harness import Checks, run  # noqa: E402

from restart_harness import one, prepare_root, run_step  # noqa: E402

check = Checks()

OLD = "1970-01-01T00:00:00+00:00"
P = "flappy-bird"

#: The schema as it stood BEFORE Stage 13B: no `outcome`, no `generation`, and
#: progress with no idea which run or task produced it.
_OLD_SCHEMA = """
CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY, project_name TEXT NOT NULL, title TEXT NOT NULL,
    objective TEXT NOT NULL, success_criteria TEXT NOT NULL, status TEXT NOT NULL,
    priority INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, goal_id TEXT, project_name TEXT NOT NULL,
    tool_name TEXT NOT NULL, args_json TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL, run_after TEXT NOT NULL, last_error TEXT NOT NULL,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE autonomy_tasks (
    task_id TEXT PRIMARY KEY, conversation_id TEXT, project_name TEXT NOT NULL,
    title TEXT NOT NULL, details TEXT NOT NULL, priority INTEGER NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL, run_after TEXT NOT NULL,
    last_error TEXT NOT NULL, result_json TEXT NOT NULL,
    initiated_by_user INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE progress_events (
    event_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, project_name TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
    acknowledged INTEGER NOT NULL);
"""


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _uid() -> str:
    return str(uuid.uuid4())


def write_old_db(root: Path) -> dict[str, str]:
    """Lay down a plausible database from before any of this existed."""
    prepare_root(root)
    db_path = root / "memory_data" / "sqlite" / "nova.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ids = {"goal": _uid(), "paused_goal": _uid(),
           "done": _uid(), "failed": _uid(), "cancelled": _uid(),
           "queued": _uid(), "running": _uid(),
           "auto_done": _uid(), "auto_running": _uid(),
           "prog_a": _uid(), "prog_b": _uid()}
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_OLD_SCHEMA)
        con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)",
                    (ids["goal"], P, "add a pause menu", "pause menu",
                     "it pauses", "active", 50, OLD, OLD))
        con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)",
                    (ids["paused_goal"], P, "add sound", "sound", "beeps",
                     "paused", 50, OLD, OLD))
        for key, status, err in (("done", "done", ""),
                                 ("failed", "failed", "the sprite sheet is missing"),
                                 ("cancelled", "cancelled", ""),
                                 ("queued", "queued", ""),
                                 ("running", "running", "")):
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ids[key], ids["goal"], P, f"code.{key}", "{}", status,
                         1, OLD, err, "{}", OLD, OLD))
        for key, status in (("auto_done", "done"), ("auto_running", "running")):
            con.execute("INSERT INTO autonomy_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ids[key], "conv-old", P, f"old {key}", "details", 50,
                         status, 1, OLD, "", "{}", 1, OLD, OLD))
        for key, msg in (("prog_a", "started the pause menu"),
                         ("prog_b", "wrote menu.py")):
            con.execute("INSERT INTO progress_events VALUES (?,?,?,?,?,?,?)",
                        (ids[key], ids["goal"], P, "note", msg, OLD, 0))
        con.commit()
    finally:
        con.close()
    return ids


def columns(root: Path, table: str) -> list[str]:
    con = sqlite3.connect(root / "memory_data" / "sqlite" / "nova.sqlite3")
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def raw(root: Path, sql: str, args: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(root / "memory_data" / "sqlite" / "nova.sqlite3")
    try:
        return list(con.execute(sql, args))
    finally:
        con.close()


READ_BACK = """
    tasks = await mem.list_goal_tasks(limit=200)
    goals = await mem.list_goals(limit=50)
    emit({"tasks": {str(t["task_id"]): [t["status"], t["outcome"],
                                        int(t["generation"])] for t in tasks},
          "goals": {str(g["goal_id"]): [g["status"], int(g["generation"])]
                    for g in goals}})
"""


async def test_a_an_old_database_opens_and_says_what_it_knows():
    check.section("§16 opening a pre-Stage-13B database")
    with _tmp() as td:
        root = Path(td) / "n"
        ids = write_old_db(root)
        check("outcome" not in columns(root, "tasks"),
              "the fixture really is the old schema (no outcome column)")
        check("generation" not in columns(root, "goals"),
              "and no goal generation")

        seen = run_step(root, READ_BACK)
        tasks = one(seen, "tasks") or {}
        goals = one(seen, "goals") or {}
        check("outcome" in columns(root, "tasks")
              and "generation" in columns(root, "tasks"),
              "the migration ran")

        # What the old row already implied may be filled in.
        check(tasks.get(ids["done"], [None, None])[1] == "succeeded",
              f"a finished step reads as succeeded ({tasks.get(ids['done'])})")
        check(tasks.get(ids["failed"], [None, None])[1] == "failed",
              f"a failed step reads as failed ({tasks.get(ids['failed'])})")
        check(tasks.get(ids["cancelled"], [None, None])[1] == "never_started",
              f"a cancelled step never started ({tasks.get(ids['cancelled'])})")
        check(raw(root, "SELECT last_error FROM tasks WHERE task_id=?",
                  (ids["failed"],))[0][0] == "the sprite sheet is missing",
              "and the old error text is still there to tell the user")

        # A goal from before generations existed is on generation 0 - the
        # oldest run there can be - not on some invented later one.
        check(goals.get(ids["goal"], [None, None])[1] == 0,
              f"an old goal starts at the first generation ({goals.get(ids['goal'])})")
        check(all(v[2] == 0 for v in tasks.values()),
              f"as does all of its old work ({sorted({v[2] for v in tasks.values()})})")


async def test_b_old_in_flight_work_gets_the_same_restart_truth():
    check.section("§16 rows left `running` by a Nova that is long gone")
    with _tmp() as td:
        root = Path(td) / "n"
        ids = write_old_db(root)
        boot = run_step(root, """
            rec = await mem.cancel_pending_background_work()
            tasks = await mem.list_goal_tasks(limit=200)
            auto = await mem.list_tasks(limit=50)
            emit({"recovery": rec,
                  "tasks": {str(t["task_id"]): [t["status"], t["outcome"]]
                            for t in tasks},
                  "auto": {str(t["task_id"]): [t["status"], t["outcome"]]
                           for t in auto}})
        """)
        tasks = one(boot, "tasks") or {}
        auto = one(boot, "auto") or {}

        running = tasks.get(ids["running"], [None, None])
        check(running[0] not in ("running", "queued"),
              f"the old in-flight step is not still running ({running})")
        check(running[1] == "unknown",
              f"and nobody claims to know how it went ({running})")
        queued = tasks.get(ids["queued"], [None, None])
        check(queued[0] == "cancelled" and queued[1] == "never_started",
              f"the old queued step never started ({queued})")

        old_auto = auto.get(ids["auto_running"], [None, None])
        check(old_auto[1] == "unknown",
              f"the old background task is unknown too ({old_auto})")
        check(auto.get(ids["auto_done"], [None, None])[1] == "succeeded",
              f"while the one that finished still succeeded "
              f"({auto.get(ids['auto_done'])})")

        # The decisive negative: nothing old was resurrected as runnable.
        claim = run_step(root, """
            c = await mem.claim_next_goal_task()
            emit({"claim": None if c is None else str(c["task_id"])})
        """)
        check(one(claim, "claim") is None,
              f"and no old row became work to do ({one(claim, 'claim')})")


async def test_c_legacy_progress_is_shown_but_never_relabelled():
    check.section("§16 progress lines from before provenance existed")
    with _tmp() as td:
        root = Path(td) / "n"
        ids = write_old_db(root)
        gid = ids["goal"]

        seen = run_step(root, f"""
            await mem.cancel_pending_background_work()
            await mem.resume_goal(goal_id=__import__("uuid").UUID("{gid}"))
            g0 = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            await mem.add_progress_event(goal_id="{gid}", project_name="{P}",
                                         kind="note", message="a new line",
                                         generation=int(g0["generation"]))
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            evs = await mem.list_progress_events(goal_id="{gid}", limit=50)
            emit({{"generation": int(g["generation"]),
                   "events": [[str(e.get("event_id")), e.get("message"),
                               e.get("generation")] for e in evs]}})
        """)
        gen = one(seen, "generation")
        events = one(seen, "events") or []
        by_msg = {e[1]: e for e in events}
        check(gen >= 1, f"the goal has moved on to a later run ({gen})")
        check(len(events) == 4,
              f"every line is shown - two legacy, Nova's own restart note, and "
              f"the new one ({len(events)})")
        check(by_msg.get("started the pause menu", [None, None, "x"])[2] is None,
              f"a legacy line has no generation "
              f"({by_msg.get('started the pause menu')})")
        check(by_msg.get("a new line", [None, None, None])[2] == gen,
              f"and a new one is stamped with the run that wrote it "
              f"({by_msg.get('a new line')})")
        # The line Nova wrote HERSELF over migrated data, with no help from the
        # test: the restart notice, stamped with the run it interrupted (0),
        # not with the run the goal has since moved to.
        restart_note = [e for e in events if "restarted" in str(e[1])]
        check(len(restart_note) == 1 and restart_note[0][2] == 0,
              f"Nova's own restart note names the run it interrupted "
              f"({restart_note})")
        check(restart_note and restart_note[0][2] != gen,
              f"which is NOT the run the goal is on now ({gen})")
        check(raw(root, "SELECT COUNT(*) FROM progress_events "
                        "WHERE generation IS NULL")[0][0] == 2,
              f"and only the two legacy rows are NULL in the database "
              f"({raw(root, 'SELECT COUNT(*) FROM progress_events WHERE generation IS NULL')[0][0]})")


async def test_d_opening_it_again_changes_nothing():
    check.section("§16 the same old database, opened three times")
    with _tmp() as td:
        root = Path(td) / "n"
        ids = write_old_db(root)
        run_step(root, READ_BACK)
        first = raw(root, "SELECT task_id, status, outcome, generation "
                          "FROM tasks ORDER BY task_id")
        vers_first = raw(root, "SELECT version FROM schema_version ORDER BY version")

        for _ in range(2):
            run_step(root, READ_BACK)
        again = raw(root, "SELECT task_id, status, outcome, generation "
                          "FROM tasks ORDER BY task_id")
        vers_again = raw(root, "SELECT version FROM schema_version ORDER BY version")

        check(first == again,
              f"nothing drifted on re-open ({len(first)} rows)")
        check(vers_first == vers_again and len(vers_first) == len(set(vers_first)),
              f"and each migration is recorded once ({vers_again})")
        check(len(raw(root, "SELECT 1 FROM tasks")) == 5,
              "no row was duplicated by the migration")


async def test_e_fencing_works_on_migrated_rows():
    check.section("§16 an old task cannot complete into a newer run")
    with _tmp() as td:
        root = Path(td) / "n"
        ids = write_old_db(root)
        gid, old_task = ids["goal"], ids["queued"]

        out = run_step(root, f"""
            await mem.cancel_pending_background_work()
            # The user asks for the goal again: a new run over old rows.
            await mem.resume_goal(goal_id=__import__("uuid").UUID("{gid}"))
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            # A worker from the old life reports on generation 0.
            v = await mem.complete_goal_task(task_id="{old_task}", status="done",
                                             result={{"ok": True}}, error="",
                                             expected_generation=0)
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            emit({{"gen": int(g["generation"]), "verdict": v,
                   "row": [[t["status"], t["outcome"], int(t["generation"])]
                           for t in rows if str(t["task_id"]) == "{old_task}"]}})
        """)
        check(int(one(out, "gen")) >= 1,
              f"the goal is on a later run ({one(out, 'gen')})")
        check(one(out, "verdict") in ("ignored", "superseded"),
              f"the old worker's completion does not apply "
              f"({one(out, 'verdict')})")
        row = (one(out, "row") or [[None, None, None]])[0]
        check(row[0] != "done" and row[1] != "succeeded",
              f"so a migrated row cannot be marked finished by it ({row})")


async def test_f_a_brand_new_database_is_not_replayed_over():
    """COUNTER-TEST. Replaying an old database must not mean replaying every
    database: a file this process created already has the latest schema, and
    stamping it is the whole point of the shortcut."""
    check.section("§16 a database Nova creates herself")
    with _tmp() as td:
        root = Path(td) / "n"
        prepare_root(root)
        run_step(root, READ_BACK)
        vers = [v for (v,) in raw(root, "SELECT version FROM schema_version "
                                        "ORDER BY version")]
        check(vers == [8],
              f"a fresh database is stamped current, not walked through "
              f"history ({vers})")
        check("generation" in columns(root, "tasks")
              and "outcome" in columns(root, "tasks"),
              "and it has the latest schema anyway")

        # And it stays that way: a second boot neither replays nor re-stamps.
        run_step(root, READ_BACK)
        check([v for (v,) in raw(root, "SELECT version FROM schema_version "
                                       "ORDER BY version")] == [8],
              "a second boot changes nothing")


async def main() -> None:
    await test_a_an_old_database_opens_and_says_what_it_knows()
    await test_b_old_in_flight_work_gets_the_same_restart_truth()
    await test_c_legacy_progress_is_shown_but_never_relabelled()
    await test_d_opening_it_again_changes_nothing()
    await test_e_fencing_works_on_migrated_rows()
    await test_f_a_brand_new_database_is_not_replayed_over()
    check.finish()


if __name__ == "__main__":
    run(main)
