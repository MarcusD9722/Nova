"""The coverage map's own rules: what counts as "run", and what counts as heard.

`tests/coverage_map.py` turns coverage data into claims like "nothing ever
calls this function" and "this except block swallows errors quietly". Layer 2
of the test program is planned from those claims, so a wrong rule would send
it after the wrong functions - or, worse, mark a never-tested function as
covered. These are the rules that decide it:

  * A function's `def` line runs at IMPORT. Counting it would mark every
    function in an imported module as run, nothing called or not.
  * A nested function's body is the nested function's. Crediting it to the
    parent would let a parent look fully run while its closure never ran.
  * A timeout caught around a network call and turned into `return None` is
    a swallowed failure, not control flow. Only a poll loop's `continue` /
    `break` is. Dropping an item on QueueFull is data loss, and swallowing
    CancelledError is how shutdown once stopped halfway (U10), so neither is
    ever "control flow".
  * A debug-level log is invisible at the default level, so it is reported
    as its own kind rather than as "logs".
  * A call to a function whose WHOLE job is logging (log_worker_error) is
    logging; a call to one that merely logs on its way to real work, or that
    only bumps a counter, is not. The first draft of the map missed the
    helper and called worker failures that log at error level "quiet".

  * The `swallowed` recorder must see what a watched block caught - the
    exception type, where it was RAISED, the test function on the stack -
    in worker threads too, without touching blocks it does not watch, and
    never by raising into the code it observes. A block coverage saw run but
    the recorder never saw is reported as unexplained, not silently skipped.

No coverage data and no coverage install are needed: the rules are pure AST,
and the recorder is checked on a throwaway module in a child process.

Run:  venv\\Scripts\\python.exe tests\\test_coverage_map_tool.py
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "coverage_map", REPO / "tests" / "coverage_map.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

check = Checks()

#: Line numbers matter below, so this is kept exactly as laid out.
SRC = '''\
import asyncio
from fastapi import APIRouter
router = APIRouter()

def outer(x):
    y = x + 1
    def inner(z):
        return z * 2
    class Local:
        attr = 1
        def method(self):
            return self.attr
    return inner(y)

class Service:
    def run(self):
        """Doc."""
        return 1
    async def poll(self):
        return 2

@router.get("/status")
async def status():
    return {"ok": True}

@functools.cache
def plain():
    return None
'''

TREE = ast.parse(SRC)
FUNCS = {qual: (node, is_method) for node, qual, is_method in cm.iter_functions(TREE)}

#: Statement lines as coverage.py reports them for SRC (docstrings excluded).
STMTS = {1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 18, 19, 20,
         22, 23, 24, 26, 27, 28}
#: What executes when the module is merely IMPORTED: imports, the
#: assignment, and every top-level def/class line. Nothing is called.
IMPORT_ONLY = {1, 2, 3, 5, 15, 16, 19, 22, 23, 26, 27}


def record(qual: str, executed: set[int], contexts=None, missing=(), taken=()):
    node, is_method = FUNCS[qual]
    return cm.function_record(node, qual, is_method, "x.py", STMTS, executed,
                              contexts or {}, list(missing), list(taken))


def handler(body: str, catches: str | None = "Exception") -> ast.ExceptHandler:
    clause = f"except {catches}:" if catches else "except:"
    src = "try:\n    work()\n" + clause + "\n" + "\n".join(
        "    " + ln for ln in body.splitlines()) + "\n"
    return ast.parse(src).body[0].handlers[0]


async def test_every_def_is_found_with_its_qualified_name() -> None:
    check.section("Every def is found, named the way a person would look it up")
    check(set(FUNCS) == {"outer", "outer.<locals>.inner", "outer.<locals>.Local.method",
                         "Service.run", "Service.poll", "status", "plain"},
          f"all seven functions, nested and methods included: {sorted(FUNCS)}")
    check(FUNCS["Service.run"][1] and FUNCS["outer.<locals>.Local.method"][1],
          "methods are marked as methods, a class nested in a function included")
    check(not FUNCS["outer"][1] and not FUNCS["outer.<locals>.inner"][1],
          "a function or a closure is not a method")


async def test_a_function_only_imported_is_never_run() -> None:
    check.section("Importing a module runs every def line - that is not a call")
    for qual in ("outer", "Service.run", "Service.poll", "status", "plain"):
        rec = record(qual, IMPORT_ONLY)
        check(rec["status"] == "never" and rec["executed"] == 0,
              f"{qual}: imported but never called reads as never run "
              f"(got {rec['status']}, {rec['executed']}/{rec['statements']})")


async def test_a_nested_body_is_the_nested_functions_own() -> None:
    check.section("A closure's body belongs to the closure, not to its parent")
    own = cm.own_lines(FUNCS["outer"][0])
    check(own == {6, 7, 9, 10, 11, 13},
          f"outer owns its own lines, the nested def LINE and the nested class "
          f"body, but not the closure's body or the method's: {sorted(own)}")
    called_parent_only = IMPORT_ONLY | {6, 7, 9, 10, 11, 13}
    check(record("outer", called_parent_only)["status"] == "full",
          "outer is fully run without any of the closure's body running")
    check(record("outer.<locals>.inner", called_parent_only)["status"] == "never",
          "...and the closure it defined but never called is still never run")
    check(record("outer.<locals>.Local.method", called_parent_only)["status"] == "never",
          "...as is the method of the class it defined")
    check(record("outer.<locals>.inner", called_parent_only | {8})["status"] == "full",
          "once the closure's own line runs, the closure is run")


async def test_partial_suites_and_branches_are_attributed() -> None:
    check.section("Partial runs, the suites that reached them, and branches")
    # (18, -16) is a missed branch in ANOTHER function: it must not count here.
    rec = record("outer", IMPORT_ONLY | {6, 7},
                 contexts={6: ["test_a"], 7: ["test_a", "test_b"], 13: ["test_c"]},
                 missing=[(6, 7), (18, -16)], taken=[(13, -5)])
    check(rec["status"] == "partial" and rec["executed"] == 2 and rec["statements"] == 6,
          f"two of six own statements is partial ({rec['executed']}/{rec['statements']})")
    check(rec["suites"] == ["test_a", "test_b"],
          f"only suites that ran a line THAT EXECUTED are credited: {rec['suites']}")
    check(rec["branches"] == 2 and rec["branches_missed"] == 1,
          "branches are counted by their source line, and only inside the function "
          f"({rec['branches_missed']}/{rec['branches']})")
    child = record("Service.run", IMPORT_ONLY | {18}, contexts={18: [""]})
    check(child["suites"] == ["(subprocess)"],
          f"a line run only by an unlabelled child process says so: {child['suites']}")
    check(record("Service.run", IMPORT_ONLY | {18})["status"] == "full",
          "a docstring is not a statement, so a one-line body that ran is fully run")


async def test_endpoints_are_recognised_and_nothing_else_is() -> None:
    check.section("Endpoints are the functions a route decorator registers")
    check(cm.route_of(FUNCS["status"][0]) == "GET /status", "@router.get('/status')")
    check(cm.route_of(FUNCS["plain"][0]) is None, "a decorator that is not a route")
    check(cm.route_of(FUNCS["outer"][0]) is None, "an undecorated function")
    ws = ast.parse('@app.websocket("/ws/events")\nasync def ws(): pass\n').body[0]
    check(cm.route_of(ws) == "WEBSOCKET /ws/events", "a websocket route")


async def test_handlers_are_classified_by_whether_anyone_hears() -> None:
    check.section("An except block is quiet unless it raises, logs or prints")
    cases = [
        ("pass", "Exception", "quiet", "except Exception: pass"),
        ("return None", "Exception", "quiet", "a fallback value, said to no one"),
        ("pass", None, "quiet", "a bare except"),
        ("logger.debug('x')", "Exception", "logs-debug", "debug only: invisible at the default level"),
        ("logger.info('x')", "Exception", "logs-info", "info"),
        ("logger.warning('x', err=str(e))", "Exception as e", "logs-warning", "warning"),
        ("logger.exception('x')", "Exception", "logs-error", "exception"),
        ("print('failed')", "Exception", "logs-info", "a print is at least visible in a console"),
        ("raise", "Exception", "raises", "a bare re-raise"),
        ("raise ValueError('x') from None", "KeyError", "raises", "a translated raise"),
        ("logger.debug('x')\nraise", "Exception", "raises", "raising wins over logging"),
    ]
    for body, catches, want, label in cases:
        got = cm.classify_handler(handler(body, catches))
        check(got == want, f"{label} -> {want} (got {got})")


#: A pure logging helper (shaped like core/workers/lifecycle.py's), a method
#: that only RECORDS the failure, a pure warning method, and a method that
#: logs on its way to doing real work.
HELPERS_SRC = '''\
import sys

def log_worker_error(log, event, exc, **fields):
    """Report a worker error in a way that cannot kill the worker."""
    try:
        log.exception(event, error=str(exc)[:500], **fields)
    except Exception:
        try:
            print(event, file=sys.stderr)
        except Exception:
            pass

class Backend:
    def _note_skip(self, kind, exc):
        self._last_skip_reason = str(exc)[:300]

    def _warn(self, msg):
        logger.warning(msg)

    def chatty(self):
        logger.info("starting")
        self.count += 1
'''
#: Elsewhere in the codebase, a function with the same name as the warning
#: helper that does not log at all.
OTHER_SRC = '''\
def _warn(msg):
    return msg
'''


async def test_a_logging_helper_counts_as_logging() -> None:
    check.section("A call to a function whose whole job is logging is logging")
    helpers = cm.collect_log_helpers([ast.parse(HELPERS_SRC)])
    check(helpers == {"log_worker_error": 4, "_warn": 3},
          "only the pure loggers are helpers, at the level they log at - not the "
          f"method that records, not the one that logs on the way: {helpers}")
    both = cm.collect_log_helpers([ast.parse(HELPERS_SRC), ast.parse(OTHER_SRC)])
    check(both == {"log_worker_error": 4},
          "a name shared with a function that does not log is not trusted, "
          f"since a call cannot be told apart by name: {both}")
    cases = [
        ("log_worker_error(logger, 'x', e)", "Exception as e", helpers, "logs-error",
         "the helper called by name"),
        ("log_worker_error(logger, 'x', e)", "Exception as e", None, "quiet",
         "...which, without the helper map, has no log call in it"),
        ("self._warn('x')", "Exception", helpers, "logs-warning",
         "a helper called as a method"),
        ("self._note_skip('query', e)", "Exception as e", helpers, "quiet",
         "recording a failure in a counter is not telling anyone"),
        ("self.chatty()", "Exception", helpers, "quiet",
         "a function that happens to log something is not a helper"),
    ]
    for body, catches, hmap, want, label in cases:
        got = cm.classify_handler(handler(body, catches), hmap)
        check(got == want, f"{label} -> {want} (got {got})")


#: Watched blocks: one catching an exception raised a call deeper, one in a
#: worker thread, one hit 40 times. One block is deliberately NOT watched.
RECORDED_MOD = '''\
import threading


def _boom():
    raise KeyError("missing-key")


def quiet_main():
    try:
        _boom()
    except Exception:
        return None


def quiet_thread(out):
    try:
        int("not a number")
    except ValueError:
        out.append(1)


def unwatched():
    try:
        raise RuntimeError("not watched")
    except RuntimeError:
        return 0


def many():
    for i in range(40):
        try:
            raise OSError(i)
        except OSError:
            pass
'''
#: A helper in tests/ that is not a test file, between the test and the code.
RECORDED_HELPER = "def call(fn):\n    return fn()\n"
RECORDED_DRIVER = '''\
import sys
import threading

sys.path.insert(0, sys.argv[1])
import helper_mod
import mod


def test_case():
    helper_mod.call(mod.quiet_main)
    out = []
    t = threading.Thread(target=mod.quiet_thread, args=(out,), name="worker-1")
    t.start()
    t.join()
    mod.unwatched()
    mod.many()


test_case()
'''


def _handler_body_lines(src: str) -> dict[str, int]:
    tree = ast.parse(src)
    out = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.ExceptHandler):
                    out[fn.name] = node.body[0].lineno
    return out


async def test_the_recorder_sees_what_a_handler_caught() -> None:
    check.section("The recorder writes down what each watched block caught")
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        mod = root / "src" / "mod.py"
        mod.write_text(RECORDED_MOD, encoding="utf-8")
        (root / "tests" / "helper_mod.py").write_text(RECORDED_HELPER, encoding="utf-8")
        driver = root / "tests" / "test_driver.py"
        driver.write_text(RECORDED_DRIVER, encoding="utf-8")
        body = _handler_body_lines(RECORDED_MOD)
        watched = [body["quiet_main"], body["quiet_thread"], body["many"]]
        out_dir = root / "out"
        out_dir.mkdir()
        targets = cm.write_swallow_hook(root / "hook", {str(mod): watched})
        env = cm.recorder_env(os.environ, root / "hook", targets, out_dir, "drv",
                              root / "tests")
        proc = subprocess.run([sys.executable, str(driver), str(root / "src")], env=env,
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=120)
        check(proc.returncode == 0 and "[coverage_map recorder]" not in proc.stderr,
              f"the observed process runs normally with the recorder in it "
              f"(exit {proc.returncode}, stderr {proc.stderr.strip()[-200:]!r})")
        recs = cm.read_swallow_records(out_dir)

    loaded = [r for r in recs if r.get("event") == "loaded"]
    hits = [r for r in recs if r.get("event") != "loaded"]
    check(len(loaded) == 1 and loaded[0]["suite"] == "drv",
          f"the process says the recorder loaded into it: {loaded}")
    at = {ln: [r for r in hits if r["line"] == ln] for ln in body.values()}

    main_hits = at[body["quiet_main"]]
    check(len(main_hits) == 1 and main_hits[0]["exc"] == "builtins.KeyError"
          and main_hits[0]["msg"] == "'missing-key'",
          f"the exception being handled, by full type name: {main_hits}")
    origin = main_hits[0]["origin"] if main_hits else ""
    check(origin.endswith(":5 in _boom") and "mod.py" in origin,
          f"...raised where it was RAISED, a call deeper, not where it was caught: {origin}")
    test = main_hits[0]["test"] if main_hits else ""
    check(bool(test) and "test_driver.py" in test and test.endswith(" in test_case"),
          f"...with the test FUNCTION on the stack, past a helper in tests/: {test}")

    thread_hits = at[body["quiet_thread"]]
    check(len(thread_hits) == 1 and thread_hits[0]["exc"] == "builtins.ValueError"
          and thread_hits[0]["thread"] == "worker-1",
          f"a block run in a worker thread is seen too: {thread_hits}")
    check(not at[body["unwatched"]], "a block that is not watched records nothing")
    check(len(at[body["many"]]) == 25,
          f"one place, one exception type: at most 25 records per process "
          f"(got {len(at[body['many']])})")


async def test_records_group_by_handler_and_exception() -> None:
    check.section("Hits are grouped by handler, then by what was caught")
    handlers = [
        {"file": "core/a.py", "line": 10, "body_line": 11, "in": "f",
         "catches": "Exception", "does": "pass", "kind": "quiet", "reached": True,
         "suites": ["test_one"]},
        {"file": "core/b.py", "line": 20, "body_line": 21, "in": "g",
         "catches": "ValueError", "does": "return None", "kind": "quiet",
         "reached": True, "suites": ["test_two"]},
    ]
    a_abs = str(REPO / "core" / "a.py")
    records = [
        {"event": "loaded", "suite": "test_one", "pid": 1},
        {"file": a_abs, "line": 11, "exc": "builtins.KeyError", "msg": "'k'",
         "origin": "x.py:1 in f", "suite": "test_one", "test": "t.py:5 in test_x"},
        # The same place spelled differently: forward slashes, and on Windows
        # (where paths are case-insensitive) the other case.
        {"file": (REPO / "core" / "a.py").as_posix().upper() if os.name == "nt"
         else (REPO / "core" / "a.py").as_posix(), "line": 11,
         "exc": "builtins.KeyError", "msg": "'k'", "origin": "x.py:1 in f",
         "suite": "test_one"},
        {"file": a_abs, "line": 11, "exc": "builtins.ValueError", "msg": "bad",
         "origin": "y.py:2 in h", "suite": "test_one"},
        # Not the body line of any handler: must be counted, not dropped.
        {"file": str(REPO / "core" / "b.py"), "line": 22, "exc": "builtins.KeyError",
         "suite": "test_two"},
    ]
    groups, unmatched = cm.aggregate_swallowed(records, handlers)
    check(len(groups) == 1 and groups[0]["file"] == "core/a.py",
          f"one handler caught something: {[g['file'] for g in groups]}")
    caught = groups[0]["caught"] if groups else []
    check([(c["exc"], c["hits"]) for c in caught]
          == [("builtins.KeyError", 2), ("builtins.ValueError", 1)],
          "each exception type separately, most frequent first, the same file "
          f"matched however it was spelled: {[(c['exc'], c['hits']) for c in caught]}")
    check(unmatched == 1, f"a hit that matches no handler is counted (got {unmatched})")
    md =cm.render_swallowed(groups, unmatched, handlers,
                             [{"suite": "test_one.py", "status": "pass"}], 1)
    never = md.split("## Reached under coverage, never seen by the recorder")
    check(len(never) == 2 and "core/b.py:20" in never[1] and "core/a.py:10" not in never[1],
          "a block coverage saw run but the recorder never saw is listed as unexplained")


async def test_control_flow_is_narrow_on_purpose() -> None:
    check.section("Only 'nothing more' and a poll tick are control flow")
    good = [
        ("break", "asyncio.QueueEmpty", "an empty queue ends a drain loop"),
        ("return", "queue.Empty", "an empty queue, plain return"),
        ("continue", "TimeoutError", "a poll timeout just loops again"),
        ("continue", "(TimeoutError, asyncio.TimeoutError)", "both timeout spellings, in a loop"),
        ("break", "StopIteration", "the end of an iterator"),
    ]
    for body, catches, label in good:
        got = cm.classify_handler(handler(body, catches))
        check(got == "control-flow", f"{label} -> control-flow (got {got})")
    bad = [
        ("return None", "asyncio.TimeoutError",
         "a timeout around a network call turned into None is a SWALLOWED FAILURE"),
        ("pass", "TimeoutError", "a timeout simply ignored"),
        ("pass", "asyncio.QueueFull", "dropping an item on a full queue is data loss"),
        ("pass", "asyncio.CancelledError", "swallowing cancellation (the U10 shutdown bug)"),
        ("break", "(asyncio.QueueEmpty, ValueError)", "a real error caught alongside a signal"),
        ("x = 1\nbreak", "asyncio.QueueEmpty", "more than a plain exit happens"),
        ("pass\nself.dropped += 1", "asyncio.QueueEmpty",
         "a pass followed by real work is not a plain exit"),
    ]
    for body, catches, label in bad:
        got = cm.classify_handler(handler(body, catches))
        check(got == "quiet", f"{label} -> quiet (got {got})")


async def main() -> None:
    await test_every_def_is_found_with_its_qualified_name()
    await test_a_function_only_imported_is_never_run()
    await test_a_nested_body_is_the_nested_functions_own()
    await test_partial_suites_and_branches_are_attributed()
    await test_endpoints_are_recognised_and_nothing_else_is()
    await test_handlers_are_classified_by_whether_anyone_hears()
    await test_a_logging_helper_counts_as_logging()
    await test_the_recorder_sees_what_a_handler_caught()
    await test_records_group_by_handler_and_exception()
    await test_control_flow_is_narrow_on_purpose()
    check.finish()


if __name__ == "__main__":
    run(main)
