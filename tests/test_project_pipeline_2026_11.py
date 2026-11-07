"""Projects: plan, build, run, report. Every branch of the build pipeline.

Test program layer 2, item 4 (docs/TEST_PROGRAM.md). Layer 1 found that the
suites reach every stage of `ProjectBuilder._build` and `_improve`, but almost
only along the path where everything works: the model returns a good plan,
good files, a test that passes. The branches that decide what Nova SAYS when
something goes wrong were the ones nothing ran:

  * a run check that crashes, a fix that does not help, a fix that does not
    compile, a package that is not installed;
  * a generated test that fails, that cannot be imported, that tests nothing;
  * an HTML page that throws, and a repair that does not repair it;
  * a plan in the wrong shape, a path outside the project, a file that does
    not compile, a repair that comes back empty;
  * an improvement that fails, for any of those reasons.

Each one is tested here against what the code is MEANT to do (its docstring,
the audit that put it there, or what Marcus has said), with real programs run
in real subprocesses in throwaway directories. Only the model's text is
scripted. A scripted reply is checked against the prompt it answers: a prompt
no case expected fails the test instead of falling through to a default.

Run:  venv\\Scripts\\python.exe tests\\test_project_pipeline_2026_11.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

import core.project_builder as builder_mod  # noqa: E402
import core.project_html_check as html_mod  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from core.project_builder import ProjectBuilder  # noqa: E402

PB = ProjectBuilder
check = Checks()


# ── helpers ─────────────────────────────────────────────────────────────────

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

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for _lvl, name, kw in self.events if name == event]


@contextlib.contextmanager
def listening(module):
    rec = _Recorder()
    original = module.logger
    module.logger = rec
    try:
        yield rec
    finally:
        module.logger = original


@contextlib.contextmanager
def bus_events():
    """Every event published while the block runs, still delivered."""
    seen: list[tuple[str, dict[str, Any]]] = []
    real = BUS.publish

    def publish(event_type: str, data: dict[str, Any] | None = None) -> None:
        seen.append((event_type, dict(data or {})))
        real(event_type, data)

    BUS.publish = publish
    try:
        yield seen
    finally:
        del BUS.publish


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


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"nova-pp-{prefix}-"))


def write(folder: Path, name: str, text: str) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else "<missing>"


class Script:
    """A scripted `_llm_file` or `_llm_json`.

    Each rule is a prompt substring and the replies to give, in order; the last
    reply repeats. A prompt no rule matches RAISES, so a test cannot pass by a
    model call it did not expect.
    """

    def __init__(self) -> None:
        self.rules: list[tuple[str, list[Any]]] = []
        self.prompts: list[str] = []

    def when(self, needle: str, *replies: Any) -> "Script":
        self.rules.append((needle.lower(), list(replies)))
        return self

    async def __call__(self, prompt: str, *_a: Any, **_kw: Any) -> Any:
        self.prompts.append(prompt)
        low = prompt.lower()
        for needle, replies in self.rules:
            if needle in low:
                return replies.pop(0) if len(replies) > 1 else replies[0]
        raise AssertionError(f"unscripted model call: {prompt[:90]!r}")

    def asked(self, needle: str) -> list[str]:
        n = needle.lower()
        return [p for p in self.prompts if n in p.lower()]


def bare_builder(root: Path, *, files: Script | None = None,
                 plans: Script | None = None) -> ProjectBuilder:
    """A ProjectBuilder on a throwaway root, with only the model scripted."""
    import asyncio as _asyncio

    pb = ProjectBuilder(projects_dir=root, llm=None,
                        llm_semaphore=_asyncio.Semaphore(1), memory=None)
    pb._llm_file = files or Script()
    pb._llm_json = plans or Script()
    return pb


# ── 1. the run check: what a real run says ──────────────────────────────────

def test_the_run_check_reads_a_real_run() -> None:
    check.section("1. _run_check: healthy, crashed, waiting, alive, swallowed")
    d = tmp("run")
    write(d, "ok.py", "print('hello')\n")
    check(PB._run_check(d, "ok.py") is None, "a program that exits cleanly is healthy")

    write(d, "crash.py", src("""
        def divide(a, b):
            return a / b

        divide(1, 0)
    """))
    out = PB._run_check(d, "crash.py")
    check(out is not None and "ZeroDivisionError" in out and "Traceback" in out,
          f"a crash returns the traceback ({(out or '').splitlines()[-1:]!r})")

    write(d, "ask.py", "name = input('your name? ')\nprint('hi', name)\n")
    check(PB._run_check(d, "ask.py") is None,
          "a console program waiting for typed input is not a crash (stdin is closed)")

    write(d, "eof.py", "raise EOFError('the archive ended early')\n")
    out = PB._run_check(d, "eof.py")
    check(out is not None and "EOFError" in out,
          "but an EOFError that is not input() waiting still is one")

    write(d, "alive.py", "import time\nprint('started', flush=True)\ntime.sleep(30)\n")
    check(PB._run_check(d, "alive.py", timeout_s=1.5) is None,
          "a program still running at the timeout is healthy: games and UIs never exit")

    write(d, "alive_tb.py", src("""
        import sys, time, traceback
        try:
            {}["missing"]
        except Exception:
            traceback.print_exc()
        sys.stderr.flush()
        time.sleep(30)
    """))
    out = PB._run_check(d, "alive_tb.py", timeout_s=2.0)
    check(out is not None and "KeyError" in out,
          "one that printed a traceback and stayed alive (a GUI callback error) is broken")

    write(d, "swallowed.py", src("""
        import traceback
        try:
            [][0]
        except Exception:
            traceback.print_exc()
    """))
    out = PB._run_check(d, "swallowed.py")
    check(out is not None and "IndexError" in out,
          "so is one that exited 0 after printing a traceback")

    out = PB._run_check(d / "no-such-folder", "ok.py")
    check(out is not None and out.strip() != "",
          f"a run that cannot even start is reported, never called healthy ({(out or '')[:50]!r})")


# ── 2. running a generated test file ────────────────────────────────────────

def test_a_test_file_passes_only_when_a_test_ran() -> None:
    check.section("2. _run_test_file: what counts as the tests passing (audit H17)")
    d = tmp("tests")
    write(d, "main.py", "def add(a, b):\n    return a + b\n")

    write(d, "test_pass.py", "from main import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    check(PB._run_test_file(d, "test_pass.py") is None, "pytest ran a test and it passed")

    write(d, "test_fail.py", "from main import add\n\n\ndef test_add():\n    assert add(2, 3) == 6\n")
    out = PB._run_test_file(d, "test_fail.py")
    check(out is not None and "1 failed" in out and "assert" in out,
          "a failing test returns pytest's report of it")

    write(d, "test_nothing.py", "from main import add\n\nprint(add(1, 1))\n")
    out = PB._run_test_file(d, "test_nothing.py")
    check(out is not None and "No test actually passed" in out,
          "a file that tests nothing is never a pass")

    write(d, "test_top.py", "from main import add\n\nassert add(2, 3) == 5\n")
    check(PB._run_test_file(d, "test_top.py") is None,
          "module-level asserts that really ran and held count as a pass")

    write(d, "test_top_bad.py", "from main import add\n\nassert add(2, 3) == 6, 'wrong sum'\n")
    out = PB._run_test_file(d, "test_top_bad.py")
    check(out is not None and "AssertionError" in out and "wrong sum" in out,
          "a module-level assert that fails is reported, with the assertion")

    write(d, "test_broken.py", "from main import nope\n\n\ndef test_x():\n    assert nope()\n")
    out = PB._run_test_file(d, "test_broken.py")
    check(out is not None and "could not run the generated tests (exit 2)" in out and "nope" in out,
          "a test pytest cannot even collect says so, naming what broke")

    write(d, "test_mainblock.py", src("""
        from main import add

        if __name__ == "__main__":
            assert add(2, 3) == 6, "main-block check"
    """))
    out = PB._run_test_file(d, "test_mainblock.py")
    check(out is not None and "main-block check" in out,
          "nothing for pytest, but a direct run that fails: the failure is reported")

    write(d, "test_skip.py", src("""
        import pytest
        from main import add


        @pytest.mark.skip
        def test_add():
            assert add(2, 3) == 5
    """))
    out = PB._run_test_file(d, "test_skip.py")
    check(out is not None and "No test actually passed" in out,
          "a file whose only test is skipped proved nothing")

    write(d, "test_xfail.py", src("""
        import pytest
        from main import add


        @pytest.mark.xfail
        def test_add():
            assert add(2, 3) == 6
    """))
    out = PB._run_test_file(d, "test_xfail.py")
    check(out is not None and "No test actually passed" in out,
          "a file whose only test FAILED (as expected, xfail) proved nothing either")

    write(d, "test_hang.py", "import time\n\n\ndef test_hang():\n    time.sleep(30)\n")
    out = PB._run_test_file(d, "test_hang.py", timeout_s=3.0)
    check(out is not None and "timed out" in out, "a test that hangs is reported as a hang")

    out = PB._run_test_file(d / "no-such-folder", "test_pass.py")
    check(out is not None and out.strip() != "", "a test run that cannot start is reported")

    # Whatever a machine's plugins print on stderr, pytest's report is the END
    # of the text, because every reader looks at the end. With pytest-asyncio
    # installed, a deprecation warning used to come last.
    dn = tmp("noise")
    write(dn, "main.py", "def add(a, b):\n    return a + b\n")
    write(dn, "conftest.py", src("""
        import sys


        def pytest_configure(config):
            sys.__stderr__.write("NOISE: a plugin printed this on stderr\\n")
            sys.__stderr__.flush()
    """))
    write(dn, "test_main.py", "from main import add\n\n\ndef test_add():\n    assert add(2, 3) == 6\n")
    out = PB._run_test_file(dn, "test_main.py") or ""
    last = out.strip().splitlines()[-1] if out.strip() else ""
    check(re.match(r"1 failed in ", last) is not None and "NOISE" in out,
          f"pytest's report ends the text, after anything on stderr ({last[:50]!r})")
    write(dn, "test_broken.py", "from main import nope\n\n\ndef test_x():\n    assert nope()\n")
    out = PB._run_test_file(dn, "test_broken.py") or ""
    first = out.splitlines()[0] if out else ""
    check(first.startswith("pytest could not run the generated tests (exit 2): ImportError: "
                           "cannot import name 'nope'"),
          f"and a collection error leads with what broke ({first[:100]!r})")


def test_the_failure_line() -> None:
    check.section("3a. failure_line: the line that says what went wrong")
    from core.project_builder import failure_line
    report = src("""
        F                                                                        [100%]
        ================================== FAILURES ===================================
        __________________________________ test_add ___________________________________

            def test_add():
        >       assert add(2, 3) == 6
        E       assert 5 == 6
        E        +  where 5 = add(2, 3)

        test_main.py:5: AssertionError
        =========================== short test summary info ===========================
        FAILED test_main.py::test_add - assert 5 == 6
        1 failed in 0.06s
    """)
    check(failure_line(report) == "FAILED test_main.py::test_add - assert 5 == 6",
          "a pytest report: its short-summary line, not '1 failed in 0.06s'")
    collect = src("""
        ==================================== ERRORS ===================================
        _______________________ ERROR collecting test_broken.py _______________________
        ImportError while importing test module 'test_broken.py'.
        test_broken.py:1: in <module>
            from main import nope
        E   ImportError: cannot import name 'nope' from 'main'
        =========================== short test summary info ===========================
        ERROR test_broken.py
        1 error in 0.12s
    """)
    check(failure_line(collect) == "ImportError: cannot import name 'nope' from 'main'",
          "a collection error: the exception pytest printed")
    chained = src("""
        _______________________ ERROR collecting test_main.py _________________________
        main.py:5: in <module>
            CONFIG = json.loads(open("config.json").read())
        E   FileNotFoundError: [Errno 2] No such file or directory: 'config.json'

        During handling of the above exception, another exception occurred:
        main.py:7: in <module>
            raise RuntimeError("the configuration could not be loaded")
        E   RuntimeError: the configuration could not be loaded
        =========================== short test summary info ===========================
        ERROR test_main.py
    """)
    check(failure_line(chained) == "RuntimeError: the configuration could not be loaded",
          "a chained error: the exception that was finally raised, as a traceback reads")
    traceback = src("""
        Traceback (most recent call last):
          File "C:\\\\p\\\\main.py", line 6, in <module>
            print(mean([]))
          File "C:\\\\p\\\\main.py", line 2, in mean
            return sum(values) / len(values)
        ZeroDivisionError: division by zero
        C:\\\\venv\\\\plugin.py:207: PytestDeprecationWarning: a plugin's warning
          warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
    """)
    check(failure_line(traceback) == "ZeroDivisionError: division by zero",
          "a traceback: its exception line, even with a warning printed after it")
    dotted = src("""
        Traceback (most recent call last):
          File "main.py", line 4, in load
            return json.loads(text)
        json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
        printed after the traceback
    """)
    check(failure_line(dotted).startswith("json.decoder.JSONDecodeError"),
          "a dotted exception name counts, even with a line printed after it")
    check(failure_line("boom\n") == "boom" and failure_line("") == "",
          "no exception at all: the last line, and nothing for nothing")
    program = src("""
        A . . .
        E . . X
        ERROR - lost connection to the scoreboard - retrying
        Traceback (most recent call last):
          File "main.py", line 9, in <module>
            move(board, "E9")
        IndexError: list index out of range
    """)
    check(failure_line(program) == "IndexError: list index out of range",
          "a crashing program's OWN output is not read as pytest's: its error is the exception")
    logged = src("""
        ================================== FAILURES ===================================
        __________________________________ test_add ___________________________________
        ------------------------------ Captured log call ------------------------------
        ERROR    root:main.py:5 lost connection - retrying
        E       assert 5 == 6
        =========================== short test summary info ===========================
        FAILED test_main.py::test_add - assert 5 == 6
    """)
    check(failure_line(logged) == "FAILED test_main.py::test_add - assert 5 == 6",
          "inside a pytest report, a captured ERROR log line is not the failure summary")


def test_compile_and_empty_generation_guards() -> None:
    check.section("3. _py_compile_error and _looks_like_failed_generation")
    d = tmp("compile")
    check(PB._py_compile_error(write(d, "ok.py", "x = 1\n")) is None, "valid Python compiles")
    err = PB._py_compile_error(write(d, "bad.py", "def f(:\n    pass\n"))
    check(err is not None and err.startswith("SyntaxError") and "line 1" in err,
          f"a syntax error is named with its line ({err!r})")
    (d / "nul.py").write_bytes(b"x = 1\x00\n")
    err = PB._py_compile_error(d / "nul.py")
    check(err is not None, f"a NUL byte is not 'compiles' ({err!r})")
    (d / "folder.py").mkdir()
    err = PB._py_compile_error(d / "folder.py")
    check(err is not None, f"a path that cannot be read is not 'compiles' ({(err or '')[:50]!r})")

    bad = PB._looks_like_failed_generation
    check(bad("") and bad("   \n") and bad("x = 1"), "under 10 characters is a failed generation")
    real = "def add(a, b):\n    return a + b\n" * 10
    check(not bad(real), "a real file is not")
    check(bad("def add(a, b): pass\n", real),
          "a reply under a fifth of the file it replaces is a failed generation")
    check(bad("print('hi')   # forty-ish characters", "x = 1\n"),
          "and under 40 characters always is, against an existing file")
    check(not bad(real[: len(real) // 2], real), "half the file is a real edit, not a failure")


# ── 4. the run check's fix loop ─────────────────────────────────────────────

CRASHER = src("""
    def mean(values):
        return sum(values) / len(values)


    if __name__ == "__main__":
        print(mean([]))
""")
FIXED_CRASHER = src("""
    def mean(values):
        return sum(values) / len(values) if values else 0.0


    if __name__ == "__main__":
        print(mean([]))
""")


async def test_the_run_check_fix_loop() -> None:
    check.section("4. _verify_and_fix_runtime: pass, blocked, patched, regenerated, refused")
    root = tmp("verify")

    proj = root / "off"
    write(proj, "main.py", CRASHER)
    pb = bare_builder(root)
    with env(NOVA_PROJECT_RUN_CHECK="off"):
        out = await pb._verify_and_fix_runtime("off", proj, ["main.py"])
    check(out is None and not pb._llm_file.prompts, "switched off: no note, nothing run")

    proj = root / "entry"
    for name in ("test_main.py", "tests.py", "helper.py", "app_main.py"):
        write(proj, name, "print('ok')\n")
    pb = bare_builder(root)
    out = await pb._verify_and_fix_runtime("entry", proj,
                                           ["test_main.py", "tests.py", "helper.py", "app_main.py"])
    check(out == "Run check passed (app_main.py).",
          f"the entry is the file named main, never a test file ({out!r})")
    out = await pb._verify_and_fix_runtime("entry", proj, ["helper.py", "tests.py"])
    check(out == "Run check passed (helper.py).", f"else the first runnable file ({out!r})")
    out = await pb._verify_and_fix_runtime("entry", proj, ["test_main.py", "tests.py", "index.html"])
    check(out is None, "only tests and pages: nothing to run, no note")

    proj = root / "blocked"
    write(proj, "main.py", "import nova_surely_not_installed_pkg\n")
    pb = bare_builder(root)
    out = await pb._verify_and_fix_runtime("blocked", proj, ["main.py"])
    check(out == "Run check blocked: package 'nova_surely_not_installed_pkg' is not "
                 "installed on this machine.",
          f"a missing package is reported as the environment's problem ({out!r})")
    check(not pb._llm_file.prompts and not pb._llm_json.prompts,
          "and the fix loop is not let loose on the code to work around it")

    proj = root / "patched"
    write(proj, "main.py", CRASHER)
    plans = Script().when("SMALLEST fix", {"find": "return sum(values) / len(values)",
                                           "replace": "return sum(values) / len(values) if values else 0.0"})
    pb = bare_builder(root, plans=plans)
    out = await pb._verify_and_fix_runtime("patched", proj, ["main.py"])
    check(out == "Run check passed (main.py).", f"a crash fixed by a one-line patch passes ({out!r})")
    check(read(proj / "main.py") == FIXED_CRASHER, "and the patch is what is on disk")
    check(not pb._llm_file.asked("crashes when run"), "the whole file was never regenerated")

    proj = root / "regen"
    write(proj, "main.py", CRASHER)
    plans = Script().when("SMALLEST fix", None)
    files = Script().when("crashes when run", FIXED_CRASHER)
    pb = bare_builder(root, files=files, plans=plans)
    out = await pb._verify_and_fix_runtime("regen", proj, ["main.py"])
    check(out == "Run check passed (main.py)." and read(proj / "main.py") == FIXED_CRASHER,
          f"no usable patch: the whole-file fix is used and passes ({out!r})")

    proj = root / "empty-fix"
    write(proj, "main.py", CRASHER)
    files = Script().when("crashes when run", "\n")
    pb = bare_builder(root, files=files, plans=Script().when("SMALLEST fix", None))
    out = await pb._verify_and_fix_runtime("empty-fix", proj, ["main.py"])
    check(out is not None and out.startswith("Run check failed; fix generation came back empty")
          and "ZeroDivisionError" in out,
          f"a fix that comes back empty is reported, with the crash ({(out or '')[:70]!r})")
    check(read(proj / "main.py") == CRASHER, "and the program is left as it was")

    proj = root / "bad-fix"
    write(proj, "main.py", CRASHER)
    files = Script().when("crashes when run", "def mean(values)\n    return 0.0  # no colon above\n")
    pb = bare_builder(root, files=files, plans=Script().when("SMALLEST fix", None))
    out = await pb._verify_and_fix_runtime("bad-fix", proj, ["main.py"])
    check(out is not None and out.startswith("Run check failed; proposed fix did not compile"),
          f"a fix that does not compile is refused ({(out or '')[:60]!r})")
    check(read(proj / "main.py") == CRASHER, "and reverted: the crashing original is restored")

    proj = root / "stuck"
    write(proj, "main.py", CRASHER)
    files = Script().when("crashes when run", CRASHER)
    plans = Script().when("SMALLEST fix", None)
    pb = bare_builder(root, files=files, plans=plans)
    with bus_events() as seen:
        out = await pb._verify_and_fix_runtime("stuck", proj, ["main.py"])
    check(out is not None and out.startswith("Run check still failing after fixes")
          and "ZeroDivisionError" in out,
          f"three failed runs end in 'still failing', with the error ({(out or '')[:60]!r})")
    runs = [d for t, d in seen if t == "project.progress" and d.get("stage") == "run_check"]
    unchanged = [d for t, d in seen if t == "project.progress" and d.get("stage") == "fix_unchanged"]
    check(len(runs) == 3 and len(files.asked("crashes when run")) == 2 and len(unchanged) == 2,
          f"exactly three runs and two fixes, each unchanged one said ({len(runs)}, "
          f"{len(files.asked('crashes when run'))}, {len(unchanged)})")

    proj = root / "deep"
    chain = "".join(f"def f{i}(x):\n    return f{i + 1}(x)\n\n\n" for i in range(40))
    write(proj, "main.py", chain + "def f40(x):\n    raise ValueError('deep failure')\n\n\nf0(1)\n")
    files = Script().when("crashes when run", "\n")
    pb = bare_builder(root, files=files, plans=Script().when("SMALLEST fix", None))
    await pb._verify_and_fix_runtime("deep", proj, ["main.py"])
    asked = files.asked("crashes when run")
    check(bool(asked) and "ValueError: deep failure" in asked[0],
          "a long traceback is shown to the fix with its END, where the error is")
    # In the TRACEBACK block: the patch prompt also states the error on a line
    # of its own, so a check of the whole prompt passes with the traceback cut.
    block = re.search(r"Traceback:\n(.*?)\n\n", (pb._llm_json.prompts or [""])[0], re.DOTALL)
    check(block is not None and "ValueError: deep failure" in block.group(1),
          "and so is it to the patch, in the traceback it is shown")

    proj = root / "long"
    padding = "".join(f"# {'-' * 70}\n" for _ in range(110))
    write(proj, "main.py", padding + CRASHER)
    files = Script().when("crashes when run", FIXED_CRASHER)     # must not be asked
    pb = bare_builder(root, files=files, plans=Script().when("SMALLEST fix", None))
    out = await pb._verify_and_fix_runtime("long", proj, ["main.py"])
    check(out is not None and "too long" in out and "ZeroDivisionError" in out
          and not files.prompts and read(proj / "main.py") == padding + CRASHER,
          "a file longer than the fix can be shown is not rewritten from part of it")


# ── 5. the minimal patch ────────────────────────────────────────────────────

async def test_the_minimal_patch() -> None:
    check.section("5. _fix_runtime_error_patch: where it looks, and when it refuses")
    root = tmp("patch")
    proj = root / "p"

    async def patch_with(code: str, reply: Any, *, run: bool = True,
                         error: str | None = None) -> tuple[bool, str, Script]:
        write(proj, "main.py", code)
        err = error if error is not None else (PB._run_check(proj, "main.py") or "")
        plans = Script().when("SMALLEST fix", reply)
        pb = bare_builder(root, plans=plans)
        ok = await pb._fix_runtime_error_patch(proj, "main.py", err)
        return ok, read(proj / "main.py"), plans

    ok, after, _ = await patch_with(CRASHER, None)
    check(ok is False and after == CRASHER, "no patch from the model: False, file untouched")
    ok, after, _ = await patch_with(CRASHER, {"find": "return total", "replace": "return 0"})
    check(ok is False and after == CRASHER, "a find text that is not in the file: refused")
    same = "return sum(values) / len(values)"
    ok, after, _ = await patch_with(CRASHER, {"find": same, "replace": same})
    check(ok is False and after == CRASHER, "a patch that changes nothing: refused")
    ok, after, _ = await patch_with(CRASHER, {"find": "len(values)", "replace": "len(values"})
    check(ok is False and after == CRASHER, "a patch that would break the syntax: refused")

    twice = src("""
        def first(values):
            head = values[3] if len(values) > 3 else None
            return values[3]


        print(first([1, 2]))
    """)
    ok, after, _ = await patch_with(twice, {"find": "values[3]", "replace": "values[-1]"})
    check(ok is True and "head = values[3] if" in after and "return values[-1]" in after,
          "applied on the crash line only: the same text elsewhere is left alone")

    off_line = src("""
        LIMIT = 0


        def ratio(n):
            return n / LIMIT


        print(ratio(4))
    """)
    ok, after, _ = await patch_with(off_line, {"find": "LIMIT = 0", "replace": "LIMIT = 2"})
    check(ok is True and after.startswith("LIMIT = 2\n"),
          "a unique find text away from the crash line is applied there")

    two_away = src("""
        LIMIT = 0
        OTHER = 0


        def ratio(n):
            return n / LIMIT


        print(ratio(4))
    """)
    ok, after, _ = await patch_with(two_away, {"find": " = 0", "replace": " = 2"})
    check(ok is False and after == two_away, "an ambiguous find text away from the crash line: refused")

    no_newline = "def f():\n    return 1 / 0\n\nf()"
    ok, after, _ = await patch_with(no_newline, {"find": "1 / 0", "replace": "1 / 1"})
    check(ok is True and after == "def f():\n    return 1 / 1\n\nf()",
          "a file without a final newline keeps it that way")

    ok, _after, plans = await patch_with(CRASHER, None, error="it just stopped")
    check(bool(plans.prompts) and CRASHER[:60] in plans.prompts[0]
          and "The crash is at line" not in plans.prompts[0],
          "no line number in the error: the model is shown the top of the file")

    library = src("""
        import random


        def pick(options):
            return random.choice(options)


        print(pick([]))
    """)
    ok, _after, plans = await patch_with(library, None)
    prompt = plans.prompts[0] if plans.prompts else ""
    window = re.search(r"Code around the crash:\n```python\n(.*?)\n```", prompt, re.DOTALL)
    check("The crash is at line 5: `return random.choice(options)`" in prompt,
          "a crash raised INSIDE a library names the program's own line that called it")
    check(window is not None and "return random.choice(options)" in window.group(1),
          f"and shows the code around it, not an empty window "
          f"({(window.group(1)[:40] if window else None)!r})")

    decoder = src("""
        import json


        def load(text):
            return json.loads(text)


        print(load(""))
    """)
    ok, _after, plans = await patch_with(decoder, None)
    prompt = plans.prompts[0] if plans.prompts else ""
    check("The crash is at line 5: `return json.loads(text)`" in prompt,
          "'line 1 column 1' in an error MESSAGE is not mistaken for the crash line")


# ── 6. the HTML smoke test's repair round ───────────────────────────────────

GOOD_PAGE = src("""
    <html><body><canvas id="c"></canvas><script>
    const ctx = document.getElementById("c").getContext("2d");
    document.addEventListener("keydown", () => {});
    function loop() { ctx.fillRect(0, 0, 4, 4); requestAnimationFrame(loop); }
    requestAnimationFrame(loop);
    </script></body></html>
""")
BROKEN_PAGE = src("""
    <html><body><canvas id="c"></canvas><script>
    const ctx = document.getElementById("c").getContext("2d");
    function loop() { ctx.fillRect(0, 0, 4, 4); undefinedHelper(); requestAnimationFrame(loop); }
    requestAnimationFrame(loop);
    </script></body></html>
""")


async def test_the_html_repair_round() -> None:
    check.section("6. _smoke_test_html: one honest repair round (audit H19)")
    if html_mod.node_executable() is None:
        check(False, "node is not installed, so the HTML smoke test cannot be tested here")
        return
    root = tmp("html")

    proj = root / "off"
    write(proj, "index.html", BROKEN_PAGE)
    pb = bare_builder(root)
    with env(NOVA_PROJECT_HTML_CHECK="0"):
        check(await pb._smoke_test_html("off", proj, ["index.html"]) is None,
              "switched off: no note")

    proj = root / "pick"
    write(proj, "about.html", BROKEN_PAGE)
    write(proj, "index.html", GOOD_PAGE)
    write(proj, "game.html", GOOD_PAGE)
    pb = bare_builder(root)
    note = await pb._smoke_test_html("pick", proj, ["about.html", "index.html"])
    check(note is not None and note.endswith("(index.html)") and "smoke test passed" in note,
          f"index.html is the entry page when there is one ({(note or '')[-40:]!r})")
    note = await pb._smoke_test_html("pick", proj, ["game.html", "about.html"])
    check(note is not None and note.endswith("(game.html)"), "else the first page")

    proj = root / "empty-repair"
    write(proj, "index.html", BROKEN_PAGE)
    pb = bare_builder(root, files=Script().when("fails when run headlessly", "\n"))
    with listening(builder_mod) as rec:
        note = await pb._smoke_test_html("empty-repair", proj, ["index.html"])
    check(note is not None and "undefinedHelper" in note
          and "repair attempt came back empty, so the file is unchanged" in note,
          f"a repair that comes back empty is said, with the error ({(note or '')[:80]!r})")
    check(read(proj / "index.html") == BROKEN_PAGE, "and the page is untouched")
    check(len(rec.named("html_smoke_failed")) == 1, "the failure is logged")
    prompt = pb._llm_file.prompts[0] if pb._llm_file.prompts else ""
    check("undefinedHelper" in prompt, "the repair was shown the real error")

    proj = root / "repaired"
    write(proj, "index.html", BROKEN_PAGE)
    pb = bare_builder(root, files=Script().when("fails when run headlessly", GOOD_PAGE))
    note = await pb._smoke_test_html("repaired", proj, ["index.html"])
    check(note is not None and "smoke test passed" in note
          and "failed the first time and was repaired" in note,
          f"a repair that works says the page failed first ({(note or '')[-70:]!r})")
    check(read(proj / "index.html") == GOOD_PAGE, "and the repaired page is kept")

    proj = root / "no-help"
    write(proj, "index.html", BROKEN_PAGE)
    still_broken = BROKEN_PAGE.replace("undefinedHelper", "otherMissingHelper")
    pb = bare_builder(root, files=Script().when("fails when run headlessly", still_broken))
    note = await pb._smoke_test_html("no-help", proj, ["index.html"])
    check(note is not None and "did not help, so the file was restored unchanged" in note,
          f"a repair that does not help is said ({(note or '')[-60:]!r})")
    check(read(proj / "index.html") == BROKEN_PAGE,
          "and the page is restored as it STARTED, not left as a second broken version")

    proj = root / "stub"
    big_broken = BROKEN_PAGE.replace("<script>", "<script>\n" + "// the rest of the game\n" * 90)
    write(proj, "index.html", big_broken)
    pb = bare_builder(root, files=Script().when("fails when run headlessly", GOOD_PAGE))
    note = await pb._smoke_test_html("stub", proj, ["index.html"])
    check(read(proj / "index.html") == big_broken and note is not None and "came back empty" in note,
          "a 'repair' a fraction of the page's size is refused, even one that would pass")

    proj = root / "long-page"
    long_page = BROKEN_PAGE.replace("<script>", "<script>\n" + "// the rest of the game\n" * 400)
    write(proj, "index.html", long_page)
    pb = bare_builder(root, files=Script().when("fails when run headlessly", GOOD_PAGE))  # must not be asked
    note = await pb._smoke_test_html("long-page", proj, ["index.html"])
    check(note is not None and "too long" in note and not pb._llm_file.prompts
          and read(proj / "index.html") == long_page,
          "a page longer than the repair can be shown is left as it was, and said")

    write(root / "calculator", "app.js", "throw new Error('a sibling project was loaded');\n")
    proj = root / "calc"
    write(proj, "index.html", GOOD_PAGE.replace(
        "<script>", '<script src="../calculator/app.js"></script><script>', 1))
    pb = bare_builder(root, files=Script().when("fails when run headlessly", GOOD_PAGE))
    note = await pb._smoke_test_html("calc", proj, ["index.html"])
    check(note is not None and "smoke test passed" in note and "sibling" not in note
          and not pb._llm_file.prompts,
          "a script in ANOTHER project whose name starts the same is not run as part of this one")

    proj = root / "no-node"
    write(proj, "index.html", BROKEN_PAGE)
    pb = bare_builder(root)
    real = html_mod.node_executable
    html_mod.node_executable = lambda: None
    try:
        note = await pb._smoke_test_html("no-node", proj, ["index.html"])
    finally:
        html_mod.node_executable = real
    check(note is not None and "NOT run" in note and not pb._llm_file.prompts,
          "no node: the note says nothing ran, and nothing is 'repaired' blind")


# ── 7. generated logic tests and the fix loop ───────────────────────────────

BUGGY_MAIN = src("""
    def apply_discount(price, percent):
        return price * percent / 100


    if __name__ == "__main__":
        print(apply_discount(80, 25))
""")
FIXED_MAIN = src("""
    def apply_discount(price, percent):
        return price - price * percent / 100


    if __name__ == "__main__":
        print(apply_discount(80, 25))
""")
DISCOUNT_TEST = src("""
    from main import apply_discount


    def test_quarter_off():
        assert apply_discount(80, 25) == 60
""")


async def run_logic(root: Path, name: str, main: str, *, tests: Any,
                    fixes: Any = None, written: list[str] | None = None,
                    review: Any = None) -> tuple[str | None, Path, Script]:
    proj = root / name
    write(proj, "main.py", main)
    files = Script().when("Write a test file", tests)
    files.when("TRACE each assertion", review if review is not None else tests)
    if fixes is not None:
        files.when("fails a logic test", fixes)
    pb = bare_builder(root, files=files)
    out = await pb._generate_and_run_tests(name, proj, written or ["main.py"], "discounts")
    return out, proj, files


async def test_the_logic_test_loop() -> None:
    check.section("7. _generate_and_run_tests: the code ends better, or exactly as it started")
    root = tmp("logic")

    proj = root / "off"
    write(proj, "main.py", BUGGY_MAIN)
    pb = bare_builder(root)
    with env(NOVA_PROJECT_LOGIC_TESTS="no"):
        out = await pb._generate_and_run_tests("off", proj, ["main.py"], "x")
    check(out is None and not pb._llm_file.prompts, "switched off: no note, no model call")
    out = await pb._generate_and_run_tests("off", proj, ["index.html", "test_main.py"], "x")
    check(out is None and not pb._llm_file.prompts, "nothing importable: no note, no model call")

    out, proj, _ = await run_logic(root, "none", BUGGY_MAIN, tests="NO_TESTS\n")
    check(out == "No automated logic tests were applicable." and not (proj / "test_main.py").exists(),
          f"NO_TESTS: said, and no test file written ({out!r})")

    out, proj, _ = await run_logic(root, "uncompilable", BUGGY_MAIN,
                                   tests="def test_x(:\n    assert True\n")
    check(out == "No reliable logic tests could be generated."
          and not (proj / "test_main.py").exists(),
          f"a test that does not compile is dropped and said ({out!r})")

    out, proj, files = await run_logic(root, "green", FIXED_MAIN, tests=DISCOUNT_TEST)
    check(out == "Logic tests passed (test_main.py)." and read(proj / "test_main.py") == DISCOUNT_TEST,
          f"a passing test is reported and kept ({out!r})")
    check(not files.asked("fails a logic test"), "and nothing was 'fixed'")

    out, proj, _ = await run_logic(root, "fixed", BUGGY_MAIN, tests=DISCOUNT_TEST, fixes=FIXED_MAIN)
    check(out == "Logic tests passed (test_main.py)." and read(proj / "main.py") == FIXED_MAIN,
          f"a failing assertion gets the PROGRAM fixed, and the fix is kept ({out!r})")

    out, proj, _ = await run_logic(root, "empty-fix", BUGGY_MAIN, tests=DISCOUNT_TEST, fixes="\n")
    check(out is not None and out.startswith("Logic tests inconclusive")
          and read(proj / "main.py") == BUGGY_MAIN,
          f"a fix that comes back empty: inconclusive, code byte-identical ({(out or '')[:50]!r})")

    with bus_events() as seen:
        out, proj, _ = await run_logic(root, "bad-fix", BUGGY_MAIN, tests=DISCOUNT_TEST,
                                       fixes="def apply_discount(price, percent)\n    return 0\n" * 3)
    runs = [d for t, d in seen if t == "project.progress" and d.get("stage") == "logic_test"]
    check(out is not None and out.startswith("Logic tests inconclusive")
          and read(proj / "main.py") == BUGGY_MAIN and len(runs) == 1,
          f"a fix that does not compile is never written, so never run ({len(runs)} test runs)")

    out, proj, files = await run_logic(root, "bad-import", BUGGY_MAIN,
                                       tests=DISCOUNT_TEST.replace("apply_discount", "discounted"))
    check(out is not None and out.startswith("Logic tests inconclusive")
          and not files.asked("fails a logic test") and read(proj / "main.py") == BUGGY_MAIN,
          "a test that cannot import what it tests is the TEST's fault: no code fix is tried")

    out, proj, files = await run_logic(root, "no-help", BUGGY_MAIN, tests=DISCOUNT_TEST,
                                       fixes=BUGGY_MAIN.replace("price * percent", "percent * price"))
    check(len(files.asked("fails a logic test")) == 2 and read(proj / "main.py") == BUGGY_MAIN,
          f"two fixes that do not help, then the original is put back "
          f"({len(files.asked('fails a logic test'))} fixes)")
    check(out is not None and "assert" in out.lower(), f"and the note names the failure ({out!r})")

    breaks_launch = FIXED_MAIN.replace("print(apply_discount(80, 25))", "print(apply_discount(80))")
    out, proj, _ = await run_logic(root, "fix-breaks-launch", BUGGY_MAIN, tests=DISCOUNT_TEST,
                                   fixes=breaks_launch)
    check(read(proj / "main.py") == BUGGY_MAIN,
          "a fix that passes the tests but stops the program starting is undone")
    check(out is not None and "a test may be wrong" not in out,
          f"and the note does not blame the test for it ({out!r})")

    no_launch = FIXED_MAIN.replace("print(apply_discount(80, 25))", "print(apply_discount(80))")
    out, proj, files = await run_logic(root, "tests-pass-no-launch", no_launch, tests=DISCOUNT_TEST)
    check(read(proj / "main.py") == no_launch and not files.asked("fails a logic test"),
          "tests that pass on a program that does not start: nothing is changed")
    check(out is not None and "a test may be wrong" not in out and "unknown" not in out
          and "TypeError" in out,
          f"and the note does not blame a test that PASSED, it names the crash ({out!r})")

    long_buggy = "".join(f"# {'-' * 70}\n" for _ in range(110)) + BUGGY_MAIN
    out, proj, files = await run_logic(root, "long", long_buggy, tests=DISCOUNT_TEST, fixes=FIXED_MAIN)
    check(not files.asked("fails a logic test") and read(proj / "main.py") == long_buggy
          and out is not None and "too long" in out,
          "a file longer than the fix can be shown is not rewritten from part of it")


# ── 8. the contract helpers, at their own layer ─────────────────────────────

class _Ns:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _ContractMemory:
    """Just the requirement and criteria reads the contract helpers make."""

    def __init__(self, *, revisions: dict[int, str], criteria: dict[int, list[dict]],
                 current: int | None) -> None:
        self.revisions, self.criteria, self.current = revisions, criteria, current

    async def current_requirement(self, *, project_name: str):
        if self.current is None:
            return None
        return {"revision": self.current, "request_text": self.revisions.get(self.current, "")}

    async def requirement_at(self, *, project_name: str, revision: int):
        text = self.revisions.get(revision)
        return None if text is None else {"request_text": text}

    async def list_acceptance_criteria(self, *, project_name: str, revision: int):
        return list(self.criteria.get(revision, []))


async def test_the_contract_helpers() -> None:
    check.section("8. the contract helpers: evidence lines, carried criteria, refusals")
    root = tmp("contract")
    pb = bare_builder(root)

    crit = lambda text: _Ns(text=text)  # noqa: E731
    verdict = _Ns(state="failing", revision=2,
                  failing=[_Ns(criterion=crit("adds two numbers"))],
                  outstanding=[_Ns(criterion=crit("subtracts"), stale_reason=""),
                               _Ns(criterion=crit("divides"),
                                   stale_reason="the code changed after it was checked")])
    lines = pb._evidence_log(verdict)
    check(lines == ["Completion state: failing (revision 2)",
                    "FAILING — adds two numbers",
                    "outstanding — subtracts (not demonstrated)",
                    "outstanding — divides (the code changed after it was checked)"],
          f"each failing and outstanding criterion gets its own line, with why ({lines[2:]!r})")

    mem = _ContractMemory(revisions={1: "a snake game"},
                          criteria={1: [{"criterion_id": "c1", "origin_quote": "a snake game"},
                                        {"criterion_id": "c2", "origin_quote": "with a pause button"}]},
                          current=1)
    pb._memory = mem
    reanchor, drop = await pb._plan_contract_change("snake", "add a high score", 1)
    check(drop == ["c2"] and reanchor == {},
          "a criterion whose words are no longer in the request is retired EXPLICITLY")
    check(await pb._plan_contract_change("snake", "add a high score", None) == ({}, []),
          "with no earlier revision there is nothing to carry")

    mem.current, mem.revisions = 3, {3: "the current words"}
    check(await pb._request_text_at("snake", 2) == "the current words",
          "a revision with no row of its own falls back to the current request text")

    class _Completion:
        def __init__(self, *, carry_fails: bool = False, criteria_fail: bool = False) -> None:
            self.carry_fails, self.criteria_fail = carry_fails, criteria_fail
            self.sealed = False

        async def record_request(self, *, slug: str, request_text: str) -> int:
            self.recorded = request_text
            return 4

        async def carry_forward(self, **_kw: Any) -> None:
            if self.carry_fails:
                raise ValueError("criterion c9 is not a span of the request at revision 4")

        async def set_criteria(self, **_kw: Any) -> None:
            if self.criteria_fail:
                raise ValueError("quote did not survive validation")

        async def seal_contract(self, **_kw: Any) -> None:
            self.sealed = True

    mem.revisions = {3: "a calculator", 4: "a calculator add a square root"}
    mem.criteria = {}
    pb._completion = _Completion(carry_fails=True)
    pb._llm_json = Script().when("ACCEPTANCE CRITERIA", {"criteria": []})
    raised = None
    with listening(builder_mod) as rec, bus_events() as seen:
        try:
            await pb._establish_contract("calc", "add a square root", previous_revision=3)
        except ValueError as e:
            raised = e
    errors = [d for t, d in seen if t == "project.error"]
    check(raised is not None, "a criterion that cannot be carried forward stops the change")
    check(len(rec.named("contract_carry_forward_failed")) == 1
          and errors and errors[0].get("mode") == "contract"
          and "nothing was changed" in str(errors[0].get("error")),
          "and is said twice: in the log, and to the user as a project error")
    check(not pb._llm_json.prompts, "no criteria are derived for a revision that was refused")

    pb._completion = _Completion(criteria_fail=True)
    pb._llm_json = Script().when("ACCEPTANCE CRITERIA", {"criteria": [
        {"text": "takes a square root", "origin_quote": "add a square root"}]})
    with listening(builder_mod) as rec:
        rev = await pb._establish_contract("calc", "add a square root")
    check(rev == 4 and len(rec.named("acceptance_criteria_rejected")) == 1,
          "criteria the service rejects are logged, and the revision still stands")
    check(not pb._completion.sealed and len(rec.named("acceptance_contract_unsealed")) == 1,
          "a revision left with no criteria is not sealed")

    mem.current = None
    pb._completion = _Completion()
    began: list[Any] = []

    async def _begin(**kw: Any) -> None:
        began.append(kw)
    pb._completion.begin_check = _begin
    await pb._validate_criteria("calc", "x", ["main.py"])
    check(not began, "no requirement recorded: no criterion is checked")

    import core.project_acceptance as acceptance_mod
    mem.current, mem.revisions = 1, {1: "a calculator page"}
    mem.criteria = {1: [{"criterion_id": "c1", "text": "has a page", "verify_kind": "machine"},
                        {"criterion_id": "c2", "text": "looks nice", "verify_kind": "human"}]}
    recorded: list[dict[str, Any]] = []

    class _Checker:
        async def begin_check(self, *, slug: str, criterion_id: str) -> str:
            return criterion_id

        async def record_verdict(self, *, context: str, verdict: str, detail: str) -> None:
            recorded.append({"id": context, "verdict": verdict, "detail": detail})

    pb._completion = _Checker()
    await pb._validate_criteria("calc", "a calculator page", ["index.html"])
    check([r["id"] for r in recorded] == ["c1"] and recorded[0]["verdict"] == "inconclusive"
          and "no runnable entry point" in recorded[0]["detail"],
          "no Python entry point: each MACHINE criterion is recorded inconclusive, saying why "
          "(the human one is left for a person)")

    recorded.clear()
    proj = root / "calc"
    (proj / "main.py").mkdir(parents=True)          # exists, cannot be read as a file
    seen_code: list[str] = []

    async def _fake_check(**kw: Any) -> tuple[str, str]:
        seen_code.append(kw["code"])
        return "passed", "fake"
    real_check = acceptance_mod.check_criterion
    acceptance_mod.check_criterion = _fake_check
    try:
        with listening(builder_mod) as rec:
            await pb._validate_criteria("calc", "a calculator page", ["main.py"])
    finally:
        acceptance_mod.check_criterion = real_check
    check(seen_code == [""] and recorded and recorded[0]["verdict"] == "passed",
          "an entry file that cannot be read is checked with no code, not skipped")
    unread = rec.named("criteria_entry_unreadable")
    check(len(unread) == 1 and unread[0].get("file") == "main.py",
          "and that it could not be read is logged")

    out = await pb.run_repro_check(root / "no-such-folder", "nova_check.py")
    check(out[0] is False and out[1].startswith("could not run the check"),
          f"a repro check that cannot start is a failure, not a pass ({out[1][:40]!r})")


# ── 9. _build, end to end on a throwaway root ───────────────────────────────

ADDER = src("""
    def add(a, b):
        return a + b


    if __name__ == "__main__":
        print(add(2, 3))
""")
SIBLING = "print('the real calculator: hands off')\n"


def plan(*files: Any, summary: str = "adds numbers", run_line: str = "python main.py") -> str:
    return json.dumps({"summary": summary, "language": "python",
                       "files": list(files), "run": run_line})


def seq(*replies: str):
    """Replies in order; the last one repeats."""
    left = list(replies)
    return lambda _prompt: left.pop(0) if len(left) > 1 else left[0]


def fenced(code: str) -> str:
    return f"```python\n{code}```"


def md_section(doc: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\n(.*?)(?=\n## |\Z)", doc, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


async def finish(pb: ProjectBuilder, slug: str, timeout_s: float = 30.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        if not pb.is_building(slug):
            return True
        await asyncio.sleep(0.05)
    return False


class Builds:
    """One booted Nova; each case scripts its own plan and files by its brief."""

    def __init__(self, nova: Any) -> None:
        self.nova = nova
        self.pb = nova.runtime._project_builder

    def on_plan(self, brief: str, reply: Any) -> None:
        self.nova.llm.when(lambda p, b=brief: "Plan a small, complete, WORKING project" in p
                           and f"Request: {b}\n" in p, reply)

    def on_file(self, brief: str, rel: str, reply: Any) -> None:
        """`rel` is a PREFIX of the file name: "" is every file of this case."""
        self.nova.llm.when(lambda p, b=brief, r=rel: f"contents of `{r}" in p
                           and f"Project request: {b}\n" in p, reply)

    def on_repair(self, rel: str, reply: Any) -> None:
        self.nova.llm.when(lambda p, r=rel: "has a syntax error" in p and f"File `{r}`" in p, reply)

    def on_suggest(self, summary: str, reply: Any) -> None:
        self.nova.llm.when(lambda p, s=summary: f"A project was just built: {s}\n" in p, reply)

    async def build(self, name: str, brief: str) -> tuple[Path, str]:
        res = await self.pb.start(name=name, brief=brief)
        assert res.get("started"), res
        ok = await finish(self.pb, res["project"])
        proj = self.nova.projects_dir / res["project"]
        doc = read(proj / "PROJECT.md")
        return proj, (doc if ok else "<the build never finished>\n" + doc)

    def calls(self, needle: str) -> int:
        return len(self.nova.llm.prompts_matching(needle))


BUILD_RULES = [("You are writing the ACCEPTANCE CRITERIA", '{"criteria": []}')]


async def test_the_build_pipeline() -> None:
    check.section("9. _build: plans, paths, generation, repair, suggestions, failure")
    async with boot(rules=BUILD_RULES, default_reply="NO_TESTS") as nova:
        b = Builds(nova)

        for brief, reply in (("case noplan: something", "I could not decide."),
                             ("case notalist: something", '{"summary": "x", "files": "main.py"}'),
                             ("case emptylist: something", '{"summary": "x", "files": []}')):
            b.on_plan(brief, reply)
            proj, doc = await b.build(brief.split(":")[0].split()[1], brief)
            check("Build failed: planning failed: model did not return a valid file plan"
                  in md_section(doc, "Progress log") and md_section(doc, "Status") == "idea",
                  f"{brief.split(':')[0]}: no valid file plan fails the build, and says so")

        b.on_plan("case junk: something", plan(42, {"purpose": "no path"}, {"path": "   "}))
        _proj, doc = await b.build("junk", "case junk: something")
        check("Build failed: planning failed: no usable file paths in plan"
              in md_section(doc, "Progress log"), "a plan with no usable path fails, and says so")

        b.on_plan("case loose: something", plan("loose_main.py", 7, {"path": ""}))
        b.on_file("case loose: something", "loose_main.py", fenced(ADDER))
        proj, doc = await b.build("loose", "case loose: something")
        check(read(proj / "loose_main.py") == ADDER
              and "Run check passed (loose_main.py)." in md_section(doc, "Progress log"),
              "a bare file name in the plan is accepted; junk entries beside it are skipped")

        many = [{"path": f"m{i}.py", "purpose": "x"} for i in range(1, 8)]
        b.on_plan("case many: something", plan(*many))
        b.on_file("case many: something", "m", fenced("print('m')\n"))
        before = b.calls("Project request: case many: something")
        proj, doc = await b.build("many", "case many: something")
        made = sorted(p.name for p in proj.glob("m*.py"))
        check(made == ["m1.py", "m2.py", "m3.py", "m4.py", "m5.py"]
              and b.calls("Project request: case many: something") - before == 5,
              f"at most five files are generated, in plan order ({made})")

        sibling = nova.projects_dir / "calculator"
        write(sibling, "main.py", SIBLING)
        outside = nova.root / "outside.py"
        b.on_plan("case calc: something", plan(
            {"path": str((sibling / "main.py").resolve()), "purpose": "hijack"},
            {"path": "../escape.py", "purpose": "escape"},
            {"path": str(outside.resolve()), "purpose": "outside"},
            {"path": ".", "purpose": "the folder itself"},
            {"path": "calc_main.py", "purpose": "the real one"}))
        b.on_file("case calc: something", "", fenced(ADDER))
        proj, doc = await b.build("calc", "case calc: something")
        check(read(sibling / "main.py") == SIBLING,
              "a planned path into ANOTHER project whose name starts the same is refused")
        check(not (nova.projects_dir / "escape.py").exists() and not outside.exists(),
              "and so are '..' and an absolute path elsewhere")
        check(read(proj / "calc_main.py") == ADDER, "the project's own file is still written")

        b.on_plan("case allunsafe: something", plan("../only_escape.py"))
        _proj, doc = await b.build("allunsafe", "case allunsafe: something")
        check("Build failed: no files were written" in md_section(doc, "Progress log")
              and not (nova.projects_dir / "only_escape.py").exists(),
              "a plan whose every path is refused fails, and says no files were written")

        b.on_plan("case retry: something", plan("retry_main.py"))
        b.on_file("case retry: something", "retry_main.py", seq("", fenced(ADDER)))
        proj, doc = await b.build("retry", "case retry: something")
        check(read(proj / "retry_main.py") == ADDER and b.calls("contents of `retry_main.py`") == 2,
              "an empty generation is retried once, and the retry is used")

        b.on_plan("case giveup: something", plan("giveup_main.py"))
        b.on_file("case giveup: something", "giveup_main.py", "")
        proj, doc = await b.build("giveup", "case giveup: something")
        check("Build failed: generation for giveup_main.py came back empty/incomplete"
              in md_section(doc, "Progress log") and not (proj / "giveup_main.py").exists(),
              "two empty generations fail the build, and nothing empty is written")

        broken = ADDER.replace("def add(a, b):", "def add(a, b)")
        b.on_plan("case repair: something", plan("repair_main.py"))
        b.on_file("case repair: something", "repair_main.py", fenced(broken))
        b.on_repair("repair_main.py", fenced(ADDER))
        proj, doc = await b.build("repair", "case repair: something")
        check(read(proj / "repair_main.py") == ADDER
              and "Run check passed (repair_main.py)." in md_section(doc, "Progress log"),
              "a file that does not compile is repaired once, and the build goes on")

        b.on_plan("case stillbroken: something", plan("stillbroken_main.py"))
        b.on_file("case stillbroken: something", "stillbroken_main.py", fenced(broken))
        b.on_repair("stillbroken_main.py", fenced(broken))
        _proj, doc = await b.build("stillbroken", "case stillbroken: something")
        check("Build failed: stillbroken_main.py failed to compile after repair: SyntaxError"
              in md_section(doc, "Progress log"), "a repair that still does not compile fails the build")

        b.on_plan("case emptyrepair: something", plan("emptyrepair_main.py"))
        b.on_file("case emptyrepair: something", "emptyrepair_main.py", fenced(broken))
        b.on_repair("emptyrepair_main.py", "")
        proj, doc = await b.build("emptyrepair", "case emptyrepair: something")
        log = md_section(doc, "Progress log")
        check(read(proj / "emptyrepair_main.py").strip() != "",
              "a repair that comes back EMPTY never replaces the file with nothing")
        check("Run check passed" not in log and "Build failed" in log,
              f"and the build fails instead of 'passing' a run of an empty file "
              f"({log.splitlines()[-1][-70:] if log else ''!r})")

        long_broken = "".join(f"# {'-' * 70}\n" for _ in range(90)) + broken
        b.on_plan("case longbroken: something", plan("longbroken_main.py"))
        b.on_file("case longbroken: something", "longbroken_main.py", fenced(long_broken))
        b.on_repair("longbroken_main.py", fenced(ADDER))
        proj, doc = await b.build("longbroken", "case longbroken: something")
        log = md_section(doc, "Progress log")
        check("too long" in log and "Build failed" in log and b.calls("`longbroken_main.py`:\n") == 0,
              "a generated file too long for its repair to be shown whole fails the build, unrepaired")

        b.on_plan("case shapes: something", plan("shapes_main.py", summary="shapes summary"))
        b.on_file("case shapes: something", "shapes_main.py", fenced(ADDER))
        b.on_suggest("shapes summary", json.dumps({"suggestions": [
            {"improvement": "Add A"}, {"suggestion": "Add B"}, {"text": "Add C"}, "Add D", ""]}))
        _proj, doc = await b.build("shapes", "case shapes: something")
        check(md_section(doc, "Next steps / suggestions") == "- [ ] Add A\n- [ ] Add B\n- [ ] Add C",
              "suggestions given as objects are read by their text, and at most three are kept")

        b.on_plan("case onestring: something", plan("onestring_main.py", summary="onestring summary"))
        b.on_file("case onestring: something", "onestring_main.py", fenced(ADDER))
        b.on_suggest("onestring summary", '{"suggestions": "Add a pause command"}')
        _proj, doc = await b.build("onestring", "case onestring: something")
        got = md_section(doc, "Next steps / suggestions")
        check(got == "- [ ] Add a pause command",
              f"ONE suggestion given as a string is one suggestion, not its letters ({got[:40]!r})")

        b.on_plan("case nullsugg: something", plan("nullsugg_main.py", summary="nullsugg summary"))
        b.on_file("case nullsugg: something", "nullsugg_main.py", fenced(ADDER))
        b.on_suggest("nullsugg summary", '{"suggestions": null}')
        _proj, doc = await b.build("nullsugg", "case nullsugg: something")
        log = md_section(doc, "Progress log")
        check("Build failed" not in log and "Run check passed (nullsugg_main.py)." in log,
              f"'no suggestions' is never a failed build ({log.splitlines()[-1][-60:] if log else ''!r})")

        pb = b.pb
        b.on_plan("case reportfails: something", "nothing usable")

        async def _broken_evaluate(**_kw: Any) -> Any:
            raise RuntimeError("the completion store is unavailable")
        pb._completion.evaluate = _broken_evaluate
        try:
            with listening(builder_mod) as rec, bus_events() as seen:
                res = await pb.start(name="reportfails", brief="case reportfails: something")
                done = await finish(pb, res["project"])
        finally:
            del pb._completion.evaluate
        failed = rec.named("build_failure_report_failed")
        check(done and len(failed) == 1 and "unavailable" in str(failed[0].get("error")),
              "when even the failure report fails, that is logged too")
        check(any(t == "project.error" and "planning failed" in str(d.get("error"))
                  for t, d in seen),
              "and the build's own failure still reaches the user as a project error")

        fut = asyncio.get_running_loop().create_future()
        pb._active["busy"] = fut
        write(nova.projects_dir / "busy", "PROJECT.md", "# busy\n")
        try:
            started = await pb.start(name="busy", brief="case busy: something")
            improving = await pb.improve(slug="busy", instructions="anything")
        finally:
            fut.cancel()
            pb._active.pop("busy", None)
        check(started == {"project": "busy", "started": False, "reason": "already building"}
              and improving.get("reason") == "already building",
              "a project already being built is not started or improved a second time")
        check(await pb.improve(slug="never-made", instructions="x")
              == {"project": "never-made", "started": False, "reason": "unknown project"},
              "an improvement to a project that does not exist is refused")


async def test_a_failed_build_stops_its_other_files() -> None:
    check.section("10. _build: one file fails, the rest stop (no work for a dead build)")
    import time
    async with boot(rules=BUILD_RULES, default_reply="NO_TESTS") as nova:
        stamps: list[float] = []

        def gen(_prompt: str) -> str:
            stamps.append(time.monotonic())
            return ""
        nova.llm.when("Plan a small, complete, WORKING project", plan(
            *({"path": p, "purpose": "x"} for p in ("a.py", "b.py", "c.py"))))
        nova.llm.when("Write the COMPLETE contents of", gen)
        pb = nova.runtime._project_builder
        await pb.start(name="trio", brief="three files")
        nova.llm.call_delay = 0.25
        try:
            while pb.is_building("trio"):
                await asyncio.sleep(0.01)
            over = time.monotonic()
            await asyncio.sleep(1.2)
        finally:
            nova.llm.call_delay = 0.0
        late = [round(s - over, 2) for s in stamps if s > over]
        check(not late, f"no generation call completes after the build has failed (late: {late})")
        # STOPPED, not waited for. The calls share one GPU permit, so they run
        # a1 b1 c1, then a's retry fails the build. Cancelled, b and c never
        # get their retries: four calls. Left running (or merely awaited) they
        # do: six. "Nothing completes after the end" alone cannot tell a
        # cancel from a wait, which made the end come later instead.
        check(len(stamps) == 4,
              f"the other files stop at the first failure: three first attempts and the one "
              f"retry that failed ({len(stamps)} generation calls)")


# ── 11. _improve, end to end ────────────────────────────────────────────────

async def seed(nova: Any, slug: str, files: dict[str, str]) -> Path:
    proj = nova.projects_dir / slug
    for name, text in files.items():
        write(proj, name, text)
    nova.runtime._project_builder._write_project_md(
        slug, brief="adds numbers", status="building",
        files=[{"path": n, "purpose": "seeded"} for n in files],
        run="python main.py", log_lines=["seeded"], suggestions=["Add a subtract button"])
    return proj


class Improves:
    def __init__(self, nova: Any) -> None:
        self.nova = nova
        self.pb = nova.runtime._project_builder

    def on_plan(self, slug: str, reply: Any) -> None:
        self.nova.llm.when(f"improving an existing project `{slug}`", reply)

    def on_file(self, slug: str, rel: str, reply: Any) -> None:
        """`rel` is a PREFIX of the file name: "" is every file of this case."""
        self.nova.llm.when(lambda p, s=slug, r=rel: f"Improve `{r}" in p
                           and f"` in project `{s}`." in p, reply)

    async def improve(self, slug: str, instructions: str = "make it better") -> tuple[str, _Recorder]:
        with listening(builder_mod) as rec:
            res = await self.pb.improve(slug=slug, instructions=instructions)
            assert res.get("started"), res
            await finish(self.pb, slug)
        return read(self.nova.projects_dir / slug / "PROJECT.md"), rec

    def calls(self, needle: str) -> int:
        return len(self.nova.llm.prompts_matching(needle))


SUBTRACTOR = src("""
    def add(a, b):
        return a + b


    def sub(a, b):
        return a - b


    if __name__ == "__main__":
        print(add(2, 3), sub(5, 1))
""")


async def test_the_improve_pipeline() -> None:
    check.section("11. _improve: plans, paths, retries, reverts, and failure reports")
    async with boot(rules=BUILD_RULES, default_reply="NO_TESTS") as nova:
        im = Improves(nova)

        proj = await seed(nova, "imp-bare", {"main.py": ADDER})
        im.on_plan("imp-bare", '{"changes": ["main.py"], "summary": "add subtract"}')
        im.on_file("imp-bare", "main.py", fenced(SUBTRACTOR))
        doc, rec = await im.improve("imp-bare")
        check(read(proj / "main.py") == SUBTRACTOR and not rec.named("project_improve_failed"),
              "a change given as a bare file name is accepted, as the build accepts one")

        sibling = nova.projects_dir / "imp-unsafe-twin"
        write(sibling, "main.py", SIBLING)
        proj = await seed(nova, "imp-unsafe", {"main.py": ADDER})
        im.on_plan("imp-unsafe", json.dumps({"summary": "x", "changes": [
            {"path": "../outside.py", "what": "escape"},
            {"path": str((sibling / "main.py").resolve()), "what": "hijack"}]}))
        im.on_file("imp-unsafe", "", fenced(SUBTRACTOR))
        doc, rec = await im.improve("imp-unsafe")
        failed = rec.named("project_improve_failed")
        check(read(sibling / "main.py") == SIBLING and not (nova.projects_dir / "outside.py").exists(),
              "an improvement never writes outside its own project, sibling or '..'")
        check(failed and "no files matched the plan" in str(failed[0].get("error")),
              "and with nothing left to change it fails, saying why")

        proj = await seed(nova, "imp-retry", {"main.py": ADDER})
        im.on_plan("imp-retry", '{"changes": [{"path": "main.py", "what": "sub"}], "summary": "s"}')
        im.on_file("imp-retry", "main.py", seq("", fenced(SUBTRACTOR)))
        doc, _ = await im.improve("imp-retry")
        check(read(proj / "main.py") == SUBTRACTOR and im.calls("in project `imp-retry`") == 2,
              "an empty generation is retried once, and the retry is used")

        proj = await seed(nova, "imp-skip", {"main.py": ADDER})
        im.on_plan("imp-skip", json.dumps({"summary": "s", "changes": [
            {"path": "main.py", "what": "sub"}, {"path": "extra.py", "what": "a helper"}]}))
        im.on_file("imp-skip", "main.py", "")
        im.on_file("imp-skip", "extra.py", fenced("def double(x):\n    return x * 2\n"))
        doc, _ = await im.improve("imp-skip")
        log = md_section(doc, "Progress log")
        check(read(proj / "main.py") == ADDER and (proj / "extra.py").exists(),
              "a file whose generation came back empty is left as it was; the other is written")
        check("Changed: extra.py" in log
              and "NOT changed — main.py: generation came back empty/incomplete" in log,
              "and the log says which changed and which did not, and why")

        proj = await seed(nova, "imp-revert", {"main.py": ADDER})
        im.on_plan("imp-revert", '{"changes": [{"path": "main.py", "what": "sub"}, '
                                 '{"path": "notes.txt", "what": "notes"}], "summary": "s"}')
        im.on_file("imp-revert", "main.py", fenced(SUBTRACTOR.replace("def sub(a, b):", "def sub(a, b)")))
        im.on_file("imp-revert", "notes.txt", "Notes about the adder, kept for later reading.\n")
        doc, _ = await im.improve("imp-revert")
        log = md_section(doc, "Progress log")
        check(read(proj / "main.py") == ADDER,
              "an edit that breaks the syntax of an existing file is reverted byte-for-byte")
        check("NOT changed — main.py: compile error after edit" in log,
              "and the log says so")

        proj = await seed(nova, "imp-newbroken", {"main.py": ADDER})
        im.on_plan("imp-newbroken", '{"changes": [{"path": "helper.py", "what": "helper"}, '
                                    '{"path": "notes.txt", "what": "notes"}], "summary": "s"}')
        im.on_file("imp-newbroken", "helper.py", fenced("def helper(x)\n    return x * 2  # no colon\n"))
        im.on_file("imp-newbroken", "notes.txt", "Notes about the adder, kept for later reading.\n")
        doc, _ = await im.improve("imp-newbroken")
        log = md_section(doc, "Progress log")
        check(not (proj / "helper.py").exists(),
              "a NEW file that does not compile is not left behind")
        check("NOT changed — helper.py" in log and "Changed: helper.py" not in log,
              f"and is reported as not changed, not as changed ({log.splitlines()[-4:-3]!r})")

        proj = await seed(nova, "imp-allfail", {"main.py": ADDER})
        im.on_plan("imp-allfail", '{"changes": [{"path": "main.py", "what": "x"}], "summary": "s"}')
        im.on_file("imp-allfail", "main.py", "")
        doc, rec = await im.improve("imp-allfail")
        failed = rec.named("project_improve_failed")
        check(failed and "no files were successfully improved (main.py: generation came back "
                         "empty/incomplete)" in str(failed[0].get("error")),
              "when no file could be changed the improvement fails, with each reason")

        proj = await seed(nova, "imp-noplan", {"main.py": ADDER})
        im.on_plan("imp-noplan", '{"changes": [], "summary": "nothing"}')
        doc, rec = await im.improve("imp-noplan", "add a subtract function")
        log = md_section(doc, "Progress log")
        check(rec.named("project_improve_failed"), "no plan: the improvement fails, logged")
        check("Improvement failed: no improvement plan produced" in log,
              "and PROJECT.md records that it failed, and why")
        facts = await nova.memory.get_facts(entity="project:imp-noplan", attribute="last_error")
        check(facts and "no improvement plan produced" in str(
                  facts[0].get("value") if isinstance(facts[0], dict) else facts[0].value),
              "and the reason is kept as the project's last error")
        status = await im.pb.status_text("imp-noplan")
        check("Improvement failed" in status,
              f"so 'what's the status?' mentions it ({status[-80:]!r})")
        check(md_section(doc, "Files") == "- `main.py` — seeded"
              and md_section(doc, "How to run") == "python main.py"
              and md_section(doc, "Next steps / suggestions") == "- [ ] Add a subtract button",
              "and nothing PROJECT.md recorded is lost: the project is as it was")

        proj = await seed(nova, "imp-reportfails", {"main.py": ADDER})
        im.on_plan("imp-reportfails", '{"changes": [], "summary": "nothing"}')

        async def _broken_evaluate(**_kw: Any) -> Any:
            raise RuntimeError("the completion store is unavailable")
        im.pb._completion.evaluate = _broken_evaluate
        try:
            with bus_events() as seen:
                doc, rec = await im.improve("imp-reportfails")
        finally:
            del im.pb._completion.evaluate
        failed = rec.named("improve_failure_report_failed")
        check(len(failed) == 1 and "unavailable" in str(failed[0].get("error")),
              "when the failed improvement's own report fails, that is logged too")
        check(any(t == "project.error" and "no improvement plan" in str(d.get("error"))
                  for t, d in seen) and not im.pb.is_building("imp-reportfails"),
              "and the failure still reaches the user, and the project is free again")

        long_main = "".join(f"def helper_{i:03d}(x):\n    return x + {i}  # {'-' * 60}\n\n\n"
                            for i in range(80)) + "print(helper_000(1))\n"
        proj = await seed(nova, "imp-long", {"main.py": long_main})
        im.on_plan("imp-long", '{"changes": [{"path": "main.py", "what": "tidy"}], "summary": "s"}')
        im.on_file("imp-long", "main.py", fenced(ADDER * 40))
        doc, rec = await im.improve("imp-long")
        failed = rec.named("project_improve_failed")
        check(read(proj / "main.py") == long_main and im.calls("in project `imp-long`") == 0,
              "a file longer than the model can be shown is not rewritten from its first part")
        check(failed and "too long" in str(failed[0].get("error"))
              and "too long" in md_section(doc, "Progress log"),
              "and the improvement says why it changed nothing")

        proj = await seed(nova, "imp-sections", {"main.py": ADDER})
        im.on_plan("imp-sections", '{"changes": [{"path": "main.py", "what": "sub"}, '
                                   '{"path": "extra.py", "what": "a helper"}], "summary": "s"}')
        im.on_file("imp-sections", "main.py", fenced(SUBTRACTOR))
        im.on_file("imp-sections", "extra.py", fenced("def double(x):\n    return x * 2\n"))
        doc, _ = await im.improve("imp-sections")
        files_md, run_md = md_section(doc, "Files"), md_section(doc, "How to run")
        check("`main.py`" in files_md and "`extra.py`" in files_md,
              f"after an improvement PROJECT.md still lists the files, new ones too ({files_md[:60]!r})")
        check(run_md == "python main.py", f"and still says how to run it ({run_md!r})")

        proj = await seed(nova, "imp-cap", {"main.py": ADDER})
        im.on_plan("imp-cap", json.dumps({"summary": "s", "changes": [
            {"path": f"f{i}.py", "what": "x"} for i in range(5)]}))
        im.on_file("imp-cap", "f", fenced("X = 1\n" * 10))
        before = im.calls("in project `imp-cap`")
        doc, _ = await im.improve("imp-cap")
        check(im.calls("in project `imp-cap`") - before == 3
              and sorted(p.name for p in proj.glob("f*.py")) == ["f0.py", "f1.py", "f2.py"],
              "at most three files are changed per improvement")


# ── 12. what is said about a project, and which one is current ──────────────

async def test_status_and_current_project() -> None:
    check.section("12. status_text and last_active: what is said when something cannot be read")
    root = tmp("status")
    proj = root / "calc"
    pb = bare_builder(root)
    pb._write_project_md("calc", brief="a calculator", status="building",
                         summary="adds numbers", log_lines=["Wrote 1 file(s)."],
                         suggestions=["Add subtract"])

    def verdict(**kw: Any) -> _Ns:
        base: dict[str, Any] = dict(state="scaffolded", failing=[], outstanding=[], legacy_note="")
        base.update(kw)
        return _Ns(**base)

    class _Eval:
        def __init__(self, v: _Ns) -> None:
            self.v = v

        async def evaluate(self, *, slug: str) -> _Ns:
            return self.v

    class _Goals:
        def __init__(self, *, fails: bool = False) -> None:
            self.fails = fails

        async def list_goals(self, *, project_name: str, limit: int = 5) -> list[dict]:
            if self.fails:
                raise RuntimeError("the goal store is locked")
            return []

    pb._completion = _Eval(verdict())
    pb._memory = _Goals(fails=True)
    with listening(builder_mod) as rec:
        text = await pb.status_text("calc")
    check("could not check whether the planned work" in text
          and len(rec.named("project_goals_unreadable")) == 1,
          f"planned work that cannot be read is SAID and logged, not left out ({text[:70]!r})")
    pb._memory = _Goals()
    text = await pb.status_text("calc")
    check("could not check" not in text, "and is not said when the work was read")

    pb._completion = _Eval(verdict(legacy_note="Built before acceptance criteria existed."))
    text = await pb.status_text("calc")
    check("Built before acceptance criteria existed." in text, "a historical note is passed on")
    pb._completion = _Eval(verdict())

    fut = asyncio.get_running_loop().create_future()
    pb._active["calc"] = fut
    try:
        text = await pb.status_text("calc")
    finally:
        fut.cancel()
        pb._active.pop("calc", None)
    check("working on it right now" in text and "Suggested next steps" not in text,
          "while it is being built it says so, instead of offering next steps")
    text = await pb.status_text("calc")
    check("Suggested next steps: Add subtract." in text, "otherwise the open suggestions are offered")
    write(proj, "PROJECT.md", read(proj / "PROJECT.md").replace("- [ ] Add subtract",
                                                                 "- [x] Add subtract"))
    text = await pb.status_text("calc")
    check("Suggested next steps" not in text, "a suggestion already ticked off is not offered again")

    class _Pointer:
        def __init__(self, value: str | None, *, read_fails: bool = False,
                     write_fails: bool = False) -> None:
            self.value, self.read_fails, self.write_fails = value, read_fails, write_fails

        async def get_latest_fact(self, *, entity: str, attribute: str) -> Any:
            if self.read_fails:
                raise RuntimeError("the fact store is locked")
            return None if self.value is None else _Ns(value=self.value)

        async def add_fact(self, *, entity: str, attribute: str, value: str,
                           confidence: float) -> None:
            if self.write_fails:
                raise RuntimeError("read-only")
            self.value = value

    pb._memory = _Pointer("calc")
    check(await pb.last_active() == "calc", "a pointer to a real project is the current project")
    pb._memory = _Pointer(None, read_fails=True)
    with listening(builder_mod) as rec:
        current = await pb.last_active()
    check(current is None and len(rec.named("last_active_unreadable")) == 1,
          "a store that cannot be read: no current project, and that is logged")
    pb._memory = _Pointer("gone")
    check(await pb.last_active() is None and pb._memory.value == "",
          "a pointer to a project that is gone is not returned, and is cleared")
    pb._memory = _Pointer("..")
    check(await pb.last_active() is None and pb._memory.value == "",
          "nor is one that is not a project name at all, and it is cleared the same way")
    pb._memory = _Pointer("gone", write_fails=True)
    with listening(builder_mod) as rec:
        current = await pb.last_active()
    check(current is None and len(rec.named("stale_last_active_not_cleared")) == 1,
          "a stale pointer that cannot be cleared is logged")


SECTIONS = {
    "1": test_the_run_check_reads_a_real_run,
    "2": test_a_test_file_passes_only_when_a_test_ran,
    "3a": test_the_failure_line,
    "3": test_compile_and_empty_generation_guards,
    "4": test_the_run_check_fix_loop,
    "5": test_the_minimal_patch,
    "6": test_the_html_repair_round,
    "7": test_the_logic_test_loop,
    "8": test_the_contract_helpers,
    "9": test_the_build_pipeline,
    "10": test_a_failed_build_stops_its_other_files,
    "11": test_the_improve_pipeline,
    "12": test_status_and_current_project,
}


def run_all() -> None:
    """Every section, or only those named in PP_ONLY (e.g. "4,5"), which the
    mutation runs use to judge a break by the sections that cover it."""
    only = {s.strip() for s in os.environ.get("PP_ONLY", "").split(",") if s.strip()}
    unknown = only - SECTIONS.keys()
    if unknown:
        raise SystemExit(f"PP_ONLY names no such section: {sorted(unknown)}")

    async def main() -> None:
        for key, fn in SECTIONS.items():
            if only and key not in only:
                continue
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        check.finish()

    run(main)


if __name__ == "__main__":
    run_all()
