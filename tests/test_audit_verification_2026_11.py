"""Audit 2026-11-02, H17-H20: whether Nova can tell that her work works.

H17 — "Logic tests passed" meant nothing ran. Fixed earlier in this round:
      `_run_test_file` runs pytest and requires collected > 0. Pinned here so
      the shape classifier cannot quietly go back.
H18 — acceptance evidence was wrong in BOTH directions: a check's own crash
      was recorded against the app, a script that printed CANNOT_CHECK and
      exited 0 was recorded as passed, and a check writing a data file in the
      project directory staled every verdict including its own.
H19 — HTML projects were never executed at all. No run check, no logic tests,
      no check scripts, and nothing said so.
H20 — the first improve of any contracted project failed ("not a span of the
      request at revision 2") and the second "succeeded" with zero criteria,
      silently dropping the original contract.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from core.completion import FAILED, INCONCLUSIVE, PASSED  # noqa: E402
from core.project_acceptance import (  # noqa: E402
    _crashed_in_the_check, _declared_verdict, check_criterion,
)
from core.project_builder import ProjectBuilder  # noqa: E402
from core.project_html_check import (  # noqa: E402
    inline_scripts, node_executable, smoke_test_html,
)
from harness import Checks, boot, run  # noqa: E402

check = Checks()


# ── H17: a test file that runs no tests is not a pass ────────────────────────

def test_a_test_file_that_runs_nothing_is_not_a_pass() -> None:
    check.section("H17: 'Ran 0 tests. OK' is not evidence")
    from core.project_builder import _collected_count, _module_level_asserts

    # pytest's own summary lines.
    check(_collected_count("3 passed in 0.12s") == 3, "3 passed -> 3")
    check(_collected_count("1 failed, 10 passed in 1.2s") == 11,
          "1 failed, 10 passed -> 11")
    check(_collected_count("no tests ran in 0.01s") == 0,
          "'no tests ran' -> 0, which is not a pass")
    check(_collected_count("Ran 0 tests\n\nOK") == 0,
          "unittest's 'Ran 0 tests ... OK' -> 0")
    check(_collected_count("") == 0, "no output -> 0")

    # The two real shapes from the finding.
    # `_module_level_asserts` reads a FILE: it parses the source rather than
    # grepping it, so a string containing the word is not evidence.
    tmp = Path(tempfile.mkdtemp(prefix="nova-h17-"))

    def _asserts(src: str, name: str) -> int:
        f = tmp / name
        f.write_text(src, encoding="utf-8")
        return _module_level_asserts(f)

    check(_asserts("def test_add():\n    assert 1 + 1 == 2\n"
                   "def test_sub():\n    assert 2 - 1 == 1\n",
                   "pytest_style.py") == 0,
          "bare def test_* has no module-level asserts to mistake for a run")
    check(_asserts("assert 1 == 1\nassert 2 == 2\n", "top_level.py") == 2,
          "module-level asserts are counted")
    check(_asserts("class TestThing:\n    def test_a(self):\n"
                   "        assert True\n\n"
                   "if __name__ == '__main__':\n"
                   "    import unittest\n    unittest.main()\n",
                   "no_testcase.py") == 0,
          "a class without TestCase under unittest.main() proves nothing")
    check(_asserts('x = "this mentions assert"\n', "stringy.py") == 0,
          "and counted structurally, not by grepping for the word")


# ── H18: the evidence itself ────────────────────────────────────────────────

def test_the_verdict_is_read_from_the_output() -> None:
    check.section("H18: a check's verdict is what it SAID, not its exit code")
    check(_declared_verdict("PASS: tasks persist") == (PASSED, "tasks persist"),
          "PASS is a pass")
    check(_declared_verdict("FAIL: done sorts first")[0] == FAILED,
          "FAIL is a failure")
    v = _declared_verdict("CANNOT_CHECK: needs a live window")
    check(v is not None and v[0] == INCONCLUSIVE,
          f"CANNOT_CHECK is inconclusive, not a pass ({v})")
    check(_declared_verdict("checked some things") is None,
          "a script that states no verdict states no verdict")
    check(_declared_verdict("PASS: early\nFAIL: later")[0] == FAILED,
          "the LAST verdict line wins")


def test_a_checks_own_crash_is_not_evidence() -> None:
    check.section("H18: a check that breaks itself decided nothing")
    rel = "nova_check_123.py"
    own_crash = (
        "Traceback (most recent call last):\n"
        f'  File "C:\\tmp\\project\\{rel}", line 12, in <module>\n'
        "    print(totl)\n"
        "NameError: name 'totl' is not defined\n")
    app_crash = (
        "Traceback (most recent call last):\n"
        f'  File "C:\\tmp\\project\\{rel}", line 8, in <module>\n'
        "    main.add_task('x')\n"
        '  File "C:\\tmp\\project\\main.py", line 40, in add_task\n'
        "    return self.tasks[99]\n"
        "IndexError: list index out of range\n")
    check(_crashed_in_the_check(own_crash, rel),
          "a NameError in the check itself is the check's fault")
    check(not _crashed_in_the_check(app_crash, rel),
          "a crash inside the program is a finding about the program")
    check(not _crashed_in_the_check("FAIL: wrong order", rel),
          "no traceback, no crash")
    check(_crashed_in_the_check(
        f'  File "{rel}", line 3\n    This is an English sentence.\n'
        "       ^\nSyntaxError: invalid syntax\nTraceback (most recent call last):\n", rel),
        "an uncommented English sentence is the check's fault too")


async def test_a_crashing_check_reaches_the_verdict_as_inconclusive() -> None:
    """The unit above is only worth what the call site does with it."""
    check.section("H18: a crashing check reaches the verdict as inconclusive")
    project = Path(tempfile.mkdtemp(prefix="nova-h18d-")) / "proj"
    project.mkdir(parents=True)
    (project / "main.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")

    for label, script, want in (
        # The four real shapes from the finding.
        ("a NameError in the check",
         "```python\nimport main\nprint(totl)\n```", INCONCLUSIVE),
        ("an uncommented English sentence",
         "```python\nimport main\nNow check that adding works.\n```", INCONCLUSIVE),
        ("looking for a file after chdir-ing away",
         "```python\nimport os, pathlib\nos.chdir(pathlib.Path(os.sep))\n"
         "open('main.py').read()\n```", INCONCLUSIVE),
        # …against a check that finds something REAL about the program, which
        # must still be a failure.
        ("a genuine finding about the program",
         "```python\nimport main\nassert main.add(2, 2) == 5\n"
         "print('PASS: adds')\n```", FAILED),
    ):
        verdict, detail = await check_criterion(
            path=project, entry="main.py", module="main", listing="main.py",
            code="def add(a, b): return a + b",
            criterion={"text": "it adds two numbers", "origin_quote": "adds"},
            request="a calculator that adds", ask_file=_reply(script),
            declare_scaffold=lambda paths: None)
        check(verdict == want,
              f"{label} -> {verdict} (want {want}): {detail[:70]!r}")


async def test_cannot_check_anywhere_is_not_a_pass() -> None:
    """The 60-character window let a CANNOT_CHECK script run and pass."""
    check.section("H18: CANNOT_CHECK past the first 60 characters")
    project = Path(tempfile.mkdtemp(prefix="nova-h18-")) / "proj"
    project.mkdir(parents=True)
    (project / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                     encoding="utf-8")

    # A reply whose CANNOT_CHECK sits well past character 60 — exactly what a
    # model produces when it explains itself first.
    padded = ("I had a look at this and the criterion is about what a person "
              "sees on screen, which running code cannot observe, so: "
              "CANNOT_CHECK")
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="def add(a, b): return a + b",
        criterion={"text": "the window looks right", "origin_quote": "looks right"},
        request="make a thing that looks right",
        ask_file=_reply(padded), declare_scaffold=lambda paths: None)
    check(verdict == INCONCLUSIVE,
          f"a late CANNOT_CHECK is inconclusive ({verdict}: {detail[:60]!r})")
    # …and RECOGNISED as a refusal to check, not written out as a script and
    # run. With the 60-character window the prose was saved as Python, died of
    # SyntaxError and landed as inconclusive by accident — same verdict, for
    # the wrong reason, and one syntactically-valid sentence away from a pass.
    check("no runnable check could be written" in detail,
          f"because it was READ as CANNOT_CHECK, not run and crashed ({detail[:80]!r})")

    # A script that PRINTS CANNOT_CHECK and exits 0 — the false pass.
    script = ("```python\n"
              "print('CANNOT_CHECK: this needs a real window')\n"
              "```")
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="def add(a, b): return a + b",
        criterion={"text": "the window looks right", "origin_quote": "looks right"},
        request="make a thing that looks right",
        ask_file=_reply(script), declare_scaffold=lambda paths: None)
    check(verdict == INCONCLUSIVE,
          f"a script that prints CANNOT_CHECK and exits 0 is NOT a pass "
          f"({verdict}: {detail[:70]!r})")

    # …and the OTHER side of that line, which nothing here used to assert:
    # reading the word anywhere in the reply is only safe because a code fence
    # overrides it. A model that writes a real check AND gives it a
    # can't-decide branch of its own mentions the word inside working Python,
    # and a refusal is read from prose, never from a script. Found by mutation
    # testing: deleting the fence exemption broke no check in this file, so
    # every generated check with a CANNOT_CHECK branch could have been thrown
    # away unrun and nothing would have said so.
    with_branch = (
        "Here is the check.\n\n"
        "```python\n"
        "import main\n"
        "try:\n"
        "    r = main.add(2, 3)\n"
        "except Exception as e:\n"
        "    print('CANNOT_CHECK: could not call it:', e)\n"
        "    raise SystemExit(0)\n"
        "assert r == 5, f'got {r}'\n"
        "print('PASS: it adds')\n"
        "```\n")
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="def add(a, b): return a + b",
        criterion={"text": "it adds two numbers", "origin_quote": "adds"},
        request="a calculator that adds",
        ask_file=_reply(with_branch), declare_scaffold=lambda paths: None)
    check(verdict == PASSED,
          f"a real check that MENTIONS CANNOT_CHECK is run, not discarded "
          f"({verdict}: {detail[:70]!r})")
    check("no runnable check could be written" not in detail,
          f"the fence is what tells a check from a refusal ({detail[:80]!r})")


async def test_a_silent_exit_zero_is_weaker_evidence() -> None:
    """A printed CANNOT_CHECK is the false pass; a bare assert is not.

    Requiring a declared verdict for a PASS was tried and reverted: it
    discards every assert-based check, which is most of them, and H18's actual
    false pass was a script that PRINTED CANNOT_CHECK and exited 0. So the
    exit code is still believed when nothing was declared, and the detail says
    so.
    """
    check.section("H18: exit 0 with no verdict is believed, but recorded as such")
    project = Path(tempfile.mkdtemp(prefix="nova-h18b-")) / "proj"
    project.mkdir(parents=True)
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")

    # The real false-pass shape: it looks at the first line and says nothing.
    script = ("```python\n"
              "out = 'all good'\n"
              "if out.startswith('Error:'):\n"
              "    raise SystemExit(1)\n"
              "```")
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="x = 1", criterion={"text": "it works", "origin_quote": "works"},
        request="make it work", ask_file=_reply(script),
        declare_scaffold=lambda paths: None)
    check(verdict == PASSED,
          f"a clean exit is still evidence ({verdict}: {detail[:60]!r})")
    check("stated no verdict" in detail,
          f"but the trail says nothing was declared ({detail[:80]!r})")

    # …and a check that states PASS is believed.
    ok = "```python\nimport main\nprint('PASS: main imports and x is', main.x)\n```"
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="x = 1", criterion={"text": "it works", "origin_quote": "works"},
        request="make it work", ask_file=_reply(ok),
        declare_scaffold=lambda paths: None)
    check(verdict == PASSED, f"a stated PASS is a pass ({verdict}: {detail[:60]!r})")


async def test_a_check_cannot_disturb_the_project() -> None:
    """A check that writes a data file used to stale every verdict."""
    check.section("H18: checks run on a copy, not on the project")
    project = Path(tempfile.mkdtemp(prefix="nova-h18c-")) / "proj"
    project.mkdir(parents=True)
    (project / "main.py").write_text(
        "import json, pathlib\n"
        "def save(tasks):\n"
        "    pathlib.Path('tasks.json').write_text(json.dumps(tasks))\n"
        "def load():\n"
        "    p = pathlib.Path('tasks.json')\n"
        "    return json.loads(p.read_text()) if p.exists() else []\n",
        encoding="utf-8")
    before = {p.name for p in project.iterdir()}

    # The natural check for "tasks persist between runs" writes tasks.json.
    script = ("```python\n"
              "import main\n"
              "main.save(['a', 'b'])\n"
              "assert main.load() == ['a', 'b']\n"
              "print('PASS: tasks round-trip through the file')\n"
              "```")
    verdict, detail = await check_criterion(
        path=project, entry="main.py", module="main", listing="main.py",
        code="(see above)",
        criterion={"text": "tasks persist between runs",
                   "origin_quote": "persist between runs"},
        request="a todo app where tasks persist between runs",
        ask_file=_reply(script), declare_scaffold=lambda paths: None)

    check(verdict == PASSED, f"the check decides the criterion ({verdict})")
    after = {p.name for p in project.iterdir()}
    check(after == before,
          f"and the project is untouched: {sorted(after - before) or 'nothing added'}")
    check("tasks.json" not in after,
          "the data file the check wrote did not land in the project")
    check(not (project / ".nova").exists(),
          "and no check script was written into the project either")


# ── H19: HTML projects ──────────────────────────────────────────────────────

def test_inline_scripts_are_found() -> None:
    check.section("H19: the page's own JavaScript is located")
    html = ('<html><head><script src="game.js"></script>'
            '<script src="https://cdn.example.com/x.js"></script>'
            '<script type="application/json">{"a":1}</script></head>'
            '<body><script>const x = 1;</script></body></html>')
    inline, external = inline_scripts(html)
    check(inline == ["const x = 1;"], f"inline code is extracted ({inline})")
    check(len(external) == 2, f"both src scripts are seen ({external})")
    check(not any("json" in s for s in inline),
          "a JSON block is not treated as code")


async def test_a_broken_page_is_reported_not_announced() -> None:
    check.section("H19: a page is actually executed")
    if node_executable() is None:
        check(False, "node is not installed, so this cannot be tested here")
        return
    tmp = Path(tempfile.mkdtemp(prefix="nova-h19-"))

    # 1. A syntax error in generated JS.
    bad = tmp / "syntax.html"
    bad.write_text("<html><body><script>function (( {</script></body></html>",
                   encoding="utf-8")
    r = await smoke_test_html(bad)
    check(r.ran and not r.syntax_ok,
          f"a JS syntax error is found ({r.errors[:1]})")
    check(not r.passed and "FAILED to parse" in r.note(),
          f"and the note says so ({r.note()[:70]!r})")

    # 2. A page that throws on load.
    throws = tmp / "throws.html"
    throws.write_text(
        "<html><body><script>document.getElementById('nope').nothing.x = 1;"
        "</script></body></html>", encoding="utf-8")
    r = await smoke_test_html(throws)
    check(r.ran and r.syntax_ok and not r.loaded,
          f"a crash on load is found (loaded={r.loaded})")
    check("threw while loading" in r.note(), f"and named ({r.note()[:70]!r})")

    # 3. A page that loads, draws and handles keys — the passing case.
    good = tmp / "game.html"
    good.write_text(
        "<html><body><canvas id='c'></canvas><script>\n"
        "const ctx = document.getElementById('c').getContext('2d');\n"
        "let dir = 'right';\n"
        "document.addEventListener('keydown', e => { dir = e.key; });\n"
        "function loop() { ctx.fillRect(0, 0, 10, 10); "
        "requestAnimationFrame(loop); }\n"
        "requestAnimationFrame(loop);\n"
        "</script></body></html>", encoding="utf-8")
    r = await smoke_test_html(good)
    check(r.passed, f"a working page passes ({r.note()[:80]!r})")
    check(r.ticks > 0 and r.drew > 0 and r.key_handlers == 1,
          f"it was driven: {r.ticks} ticks, {r.drew} draws, "
          f"{r.key_handlers} key handler(s)")
    check("not that it plays correctly" in r.note(),
          "and the note does not overclaim what was proven")

    # 4. The serpent shape: it runs, but nothing listens for a key.
    deaf = tmp / "deaf.html"
    deaf.write_text(
        "<html><body><canvas id='c'></canvas><script>\n"
        "const ctx = document.getElementById('c').getContext('2d');\n"
        "function loop() { ctx.fillRect(0, 0, 5, 5); requestAnimationFrame(loop); }\n"
        "requestAnimationFrame(loop);\n</script></body></html>", encoding="utf-8")
    r = await smoke_test_html(deaf)
    check(r.passed and r.key_handlers == 0, "a page with no key handler still runs")
    check("NO keyboard handler" in r.note(),
          f"but the note says nothing responds to a key ({r.note()[:90]!r})")

    # 5. A page that loads and does nothing at all.
    inert = tmp / "inert.html"
    inert.write_text("<html><body><script>const unused = 1;</script></body></html>",
                     encoding="utf-8")
    r = await smoke_test_html(inert)
    check("NOTHING DRAWN" in r.note(),
          f"a page that draws nothing is called out ({r.note()[:80]!r})")

    # 6. No JavaScript in the project: honest about having run nothing.
    plain = tmp / "plain.html"
    plain.write_text("<html><body><h1>Hello</h1></body></html>", encoding="utf-8")
    r = await smoke_test_html(plain)
    check(not r.ran and "NOT run" in r.note(),
          f"nothing to run is said plainly ({r.note()[:80]!r})")


async def test_an_html_project_gets_a_verification_note() -> None:
    """The builder must not pass over an HTML project in silence."""
    check.section("H19: the builder checks HTML projects")
    if node_executable() is None:
        check(False, "node is not installed, so this cannot be tested here")
        return
    async with boot() as nova:
        pb = nova.runtime._project_builder
        path = Path(tempfile.mkdtemp(prefix="nova-h19b-")) / "proj"
        path.mkdir(parents=True)
        (path / "index.html").write_text(
            "<html><body><canvas id='c'></canvas><script>\n"
            "const ctx = document.getElementById('c').getContext('2d');\n"
            "document.addEventListener('keydown', () => {});\n"
            "function loop() { ctx.fillRect(0,0,4,4); requestAnimationFrame(loop); }\n"
            "requestAnimationFrame(loop);\n</script></body></html>",
            encoding="utf-8")

        note = await pb._smoke_test_html("probe", path, ["index.html"])
        check(note is not None, f"an HTML project gets a note ({str(note)[:60]!r})")
        check(note is not None and "smoke test passed" in note,
              f"saying it ran ({str(note)[:90]!r})")
        check(note is not None and "no human has played it" in note,
              "and saying what it does not prove")

        # A Python project is not sent through it.
        py = Path(tempfile.mkdtemp(prefix="nova-h19c-")) / "proj"
        py.mkdir(parents=True)
        (py / "main.py").write_text("print('hi')\n", encoding="utf-8")
        check(await pb._smoke_test_html("probe", py, ["main.py"]) is None,
              "a Python project is left to the run check")


# ── H20: the contract across revisions ──────────────────────────────────────

def test_the_combined_request_is_one_definition() -> None:
    check.section("H20: the planner and the service use the same text")
    c = ProjectBuilder.combined_request
    check(c("a snake game", "add a pause button") == "a snake game add a pause button",
          "a follow-up revision is the previous request plus the new one")
    check(c("", "add a pause button") == "add a pause button",
          "a first revision is just the request")
    check(c("a snake game", "") == "a snake game", "an empty instruction changes nothing")


async def test_improving_a_contracted_project_twice() -> None:
    """The whole H20 sequence: build, improve, improve."""
    check.section("H20: two improvements, with the contract intact")
    async with boot() as nova:
        slug = "h20-probe"
        svc = nova.runtime._project_builder._completion
        pb = nova.runtime._project_builder
        mem = nova.memory

        request = "build a calculator that adds and subtracts"
        rev1 = await svc.record_request(slug=slug, request_text=request)
        await svc.set_criteria(slug=slug, revision=rev1, criteria=[
            {"text": "it adds two numbers", "origin_quote": "adds",
             "source": "plan", "required": True, "verify_kind": "machine"},
            {"text": "it subtracts two numbers", "origin_quote": "subtracts",
             "source": "plan", "required": True, "verify_kind": "machine"},
        ])
        rows = await mem.list_acceptance_criteria(project_name=slug, revision=rev1)
        check(len(rows) == 2, f"revision 1 holds two criteria ({len(rows)})")

        # FIRST IMPROVE. This raised "not a span of the request at revision 2".
        instruction = "also multiply"
        prev_rev, prev_text = await pb._contract_basis(slug)
        check(prev_rev == rev1, f"the basis is revision 1 ({prev_rev})")
        reanchor, drop = await pb._plan_contract_change(slug, instruction, prev_rev)
        check(not drop, f"nothing is dropped by a request that only ADDS ({drop})")
        try:
            rev2 = await pb._establish_contract(
                slug, instruction, previous_revision=prev_rev,
                reanchor=reanchor, drop=drop)
            failed = ""
        except ValueError as e:
            rev2, failed = None, str(e)
        check(not failed, f"the first improve does not raise ({failed[:110]})")

        rows2 = await mem.list_acceptance_criteria(project_name=slug, revision=rev2)
        texts = {r["text"] for r in rows2}
        check("it adds two numbers" in texts and "it subtracts two numbers" in texts,
              f"both original criteria carried forward ({sorted(texts)})")
        recorded = await mem.requirement_at(project_name=slug, revision=rev2)
        check(recorded is not None
              and "adds and subtracts" in (recorded["request_text"] or "")
              and "multiply" in (recorded["request_text"] or ""),
              f"and the revision records BOTH requests "
              f"({str((recorded or {}).get('request_text'))[:70]!r})")

        # SECOND IMPROVE. This "succeeded" with zero criteria.
        prev_rev2, _ = await pb._contract_basis(slug)
        check(prev_rev2 == rev2, f"the basis is revision 2 ({prev_rev2})")
        reanchor, drop = await pb._plan_contract_change(slug, "and divide", prev_rev2)
        rev3 = await pb._establish_contract(slug, "and divide",
                                           previous_revision=prev_rev2,
                                           reanchor=reanchor, drop=drop)
        rows3 = await mem.list_acceptance_criteria(project_name=slug, revision=rev3)
        texts3 = {r["text"] for r in rows3}
        check("it adds two numbers" in texts3 and "it subtracts two numbers" in texts3,
              f"the ORIGINAL contract is still in force at revision 3 "
              f"({sorted(texts3)})")
        check(len(rows3) >= 2,
              f"revision 3 does not have zero criteria ({len(rows3)})")


async def test_an_empty_revision_cannot_erase_the_contract() -> None:
    """A revision with no criteria is stepped over, not carried from."""
    check.section("H20: a contract-less revision does not drop the contract")
    async with boot() as nova:
        slug = "h20-empty"
        svc = nova.runtime._project_builder._completion
        pb = nova.runtime._project_builder
        mem = nova.memory

        rev1 = await svc.record_request(slug=slug, request_text="build a timer")
        await svc.set_criteria(slug=slug, revision=rev1, criteria=[
            {"text": "it counts down", "origin_quote": "timer",
             "source": "plan", "required": True, "verify_kind": "machine"}])
        # The dangling revision the old failure left behind.
        rev2 = await svc.record_request(slug=slug, request_text="add a bell")
        rows = await mem.list_acceptance_criteria(project_name=slug, revision=rev2)
        check(not rows, f"revision 2 holds no criteria ({len(rows)})")

        basis, text = await pb._contract_basis(slug)
        check(basis == rev1,
              f"the basis steps back to the last revision WITH a contract ({basis})")
        check("timer" in text, f"and brings its words ({text!r})")

        # …and the REAL improve path uses it. Asserting on the helper alone
        # left the call site free to go back to `current_requirement`, which
        # is the bug: carrying forward from an empty revision carries nothing.
        proj = nova.root / "projects" / slug
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "main.py").write_text("def countdown(n):\n    return n - 1\n",
                                      encoding="utf-8")
        (proj / "PROJECT.md").write_text(f"# {slug}\n\n## Files\n- main.py\n",
                                         encoding="utf-8")
        res = await pb.improve(slug=slug, instructions="and make it loud")
        check(res.get("started") is True, f"improve starts ({res})")
        task = pb._active.get(slug)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=90)
            except (TimeoutError, asyncio.TimeoutError):
                check(False, "the improve did not finish within 90s")

        req = await mem.current_requirement(project_name=slug)
        latest = int((req or {}).get("revision") or 0)
        rows = await mem.list_acceptance_criteria(project_name=slug,
                                                  revision=latest)
        texts = {r["text"] for r in rows}
        check("it counts down" in texts,
              f"the original criterion survived a real improve at revision "
              f"{latest} ({sorted(texts)})")
        check("timer" in str((req or {}).get("request_text") or ""),
              f"and the recorded request still contains it "
              f"({str((req or {}).get('request_text'))[:70]!r})")


def _reply(text: str):
    """An `ask_file` that returns a fixed reply."""
    async def _ask(_prompt: str) -> str:
        return text
    return _ask


async def main() -> None:
    test_a_test_file_that_runs_nothing_is_not_a_pass()
    test_the_verdict_is_read_from_the_output()
    test_a_checks_own_crash_is_not_evidence()
    await test_a_crashing_check_reaches_the_verdict_as_inconclusive()
    await test_cannot_check_anywhere_is_not_a_pass()
    await test_a_silent_exit_zero_is_weaker_evidence()
    await test_a_check_cannot_disturb_the_project()
    test_inline_scripts_are_found()
    await test_a_broken_page_is_reported_not_announced()
    await test_an_html_project_gets_a_verification_note()
    test_the_combined_request_is_one_definition()
    await test_improving_a_contracted_project_twice()
    await test_an_empty_revision_cannot_erase_the_contract()
    check.finish()


if __name__ == "__main__":
    run(main)
