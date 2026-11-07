"""Find-and-replace edits for files too long to rewrite whole: core/patch_edit.py.

Test program layer 2, item 4, follow-up (docs/TEST_PROGRAM.md). Every repair,
fix and improvement used to ask the model for the COMPLETE file, and a long
file lost its end. Marcus chose (2026-11-07) to have long files edited by
find/replace patches instead. This suite holds the module to what its
docstring promises:

  * an edit applies only where its SEARCH matches exactly ONE place;
  * all the edits apply, or none do;
  * the match is exact, then exact inside a line, then exact but for trailing
    whitespace and one consistent indentation, and nothing looser;
  * a refusal names the edit and says what was wrong, because the model is
    shown it;
  * a long file's view shows the parts that matter, copied exactly, inside its
    budget.

The builder's use of it (`_patch_file` and the five callers) is tested in
test_project_pipeline_2026_11.py, sections 4, 6, 7, 9, 11 and 13.

Run:  venv\\Scripts\\python.exe tests\\test_patch_edit_2026_11.py
"""
from __future__ import annotations

import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks  # noqa: E402

import core.project_builder as builder_mod  # noqa: E402
from core import patch_edit as pe  # noqa: E402
from core.project_html_check import page_lines  # noqa: E402

check = Checks()


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def refused(fn: Any, *args: Any) -> str:
    """The PatchError message, or "" when it did NOT refuse."""
    try:
        fn(*args)
    except pe.PatchError as e:
        return str(e) or "<empty message>"
    return ""


def block(search: str, replace: str, marks: tuple[str, str, str] = (
        "<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE")) -> str:
    return f"{marks[0]}\n{search}\n{marks[1]}\n{replace}\n{marks[2]}\n"


PROGRAM = src("""
    import random


    class Bird:
        def __init__(self):
            self.y = 0
            self.speed = 0

        def flap(self):
            self.speed = -8

        def fall(self):
            self.speed += 1
            self.y += self.speed


    def score(passed):
        return passed * 10


    if __name__ == "__main__":
        print(score(3))
""")


# ── 1. parse_edits ──────────────────────────────────────────────────────────

def test_parsing() -> None:
    check.section("1. parse_edits: what counts as an edit block")
    one = block("        self.speed = -8", "        self.speed = -9")
    check(pe.parse_edits(one) == [("        self.speed = -8", "        self.speed = -9")],
          "one block is one (search, replace) pair, indentation kept")

    wrapped = ("Here is the fix:\n```python\n" + one + "```\nThat should do it.\n")
    check(pe.parse_edits(wrapped) == pe.parse_edits(one),
          "prose and a code fence around the block are ignored")

    two = one + "\nand then\n" + block("return passed * 10", "return passed * 5")
    got = pe.parse_edits(two)
    check(len(got) == 2 and got[1] == ("return passed * 10", "return passed * 5"),
          "two blocks are two edits, in order")

    for marks in (("<<<<<< SEARCH", "======", ">>>>>> REPLACE"),
                  ("<<<<<<<< SEARCH  ", "========  ", ">>>>>>>> REPLACE  "),
                  ("  <<<<<<< SEARCH", "=======", "  >>>>>>> REPLACE")):
        check(pe.parse_edits(block("a", "b", marks)) == [("a", "b")],
              f"markers of 5 to 9 characters, trailing spaces, an indented marker: {marks[0]!r}")

    underline = block("    Title\n    =======\n    text", "    Title\n    =======\n    more")
    check(pe.parse_edits(underline) == [("    Title\n    =======\n    text",
                                         "    Title\n    =======\n    more")],
          "an INDENTED row of '=' is a line of code, not the divider")

    check(pe.parse_edits(block("x = 1", "")) == [("x = 1", "")],
          "an empty REPLACE is a deletion")
    crlf = one.replace("\n", "\r\n")
    check(pe.parse_edits(crlf) == pe.parse_edits(one), "a reply with CRLF line ends reads the same")

    check("no edit blocks" in refused(pe.parse_edits, "I changed the speed to -9."),
          "a reply with no blocks is refused, saying so")
    check("cut off" in refused(pe.parse_edits, "<<<<<<< SEARCH\nx = 1\n"),
          "a reply that stops inside SEARCH is refused as cut off")
    cut = one + "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n"
    check("cut off" in refused(pe.parse_edits, cut) and "edit 2" in refused(pe.parse_edits, cut),
          "one that stops inside a later REPLACE is refused WHOLE, naming the edit")
    check("=======" in refused(pe.parse_edits, "<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n"),
          "a block with no divider is refused")
    check(">>>>>>> REPLACE" in refused(pe.parse_edits,
                                       "<<<<<<< SEARCH\nx\n=======\ny\n<<<<<<< SEARCH\n"),
          "a block that is never ended is refused")


# ── 2. apply_edits ──────────────────────────────────────────────────────────

def test_applying() -> None:
    check.section("2. apply_edits: one place exactly, all or nothing")
    out = pe.apply_edits(PROGRAM, [("        self.speed = -8", "        self.speed = -9")])
    check(out == PROGRAM.replace("self.speed = -8", "self.speed = -9"),
          "an exact whole-line match is replaced, and every other byte is kept")
    check(out.endswith("print(score(3))\n"), "the final newline is kept")
    bare = PROGRAM.rstrip("\n")
    check(pe.apply_edits(bare, [("        print(score(3))", "        print(score(4))")])
          == bare.replace("score(3)", "score(4)"),
          "and a file without one stays without one")

    check(pe.apply_edits(PROGRAM, [("passed * 10", "passed * 5")])
          == PROGRAM.replace("passed * 10", "passed * 5"),
          "text inside a line is replaced there and nowhere else")
    msg = refused(pe.apply_edits, PROGRAM, [("self.speed", "self.velocity")])
    check("matches" in msg and "places" in msg and "edit 1" in msg,
          f"text that appears in several places is refused, not guessed ({msg[:60]!r})")
    twice = PROGRAM + "\n\ndef other():\n    return 1\n\n\ndef again():\n    return 1\n"
    msg = refused(pe.apply_edits, twice, [("    return 1", "    return 2")])
    check("matches 2 places (lines 26, 30)" in msg,
          f"whole lines in two places: refused, with both line numbers ({msg[:70]!r})")

    spaced = [("        self.speed = -8   ", "        self.speed = -9")]
    check(pe.apply_edits(PROGRAM, spaced) == PROGRAM.replace("= -8", "= -9"),
          "trailing spaces the model added to SEARCH do not stop the match")
    trailing = PROGRAM.replace("self.speed = -8\n", "self.speed = -8  \n")
    check(pe.apply_edits(trailing, [("        self.speed = -8", "        self.speed = -9")])
          == trailing.replace("= -8", "= -9"),
          "nor do trailing spaces in the FILE (it is exact text inside that line)")

    dedented = [("def fall(self):\n    self.speed += 1\n    self.y += self.speed",
                 "def fall(self):\n    if self.y < 400:\n        self.speed += 1\n\n    self.y += self.speed")]
    want = PROGRAM.replace(
        "        self.speed += 1\n",
        "        if self.y < 400:\n            self.speed += 1\n\n")
    check(pe.apply_edits(PROGRAM, dedented) == want,
          "a method copied without its class's indentation matches, and REPLACE is moved the "
          "same way (a blank line in it stays empty)")
    flat = [("def fall(self):\nself.speed += 1", "x")]
    check("is not in the file" in refused(pe.apply_edits, PROGRAM, flat),
          "lines that would each need a DIFFERENT shift are not a match")
    suffix = [("y = 0\nspeed = 0", "y = 1\nspeed = 1")]
    check("is not in the file" in refused(pe.apply_edits, PROGRAM, suffix),
          "nor are lines that only END the same way: `self.` is not indentation")
    msg = refused(pe.apply_edits, PROGRAM, [("    def fall(self):\n        self.speed += 1",
                                             "    def fall(self):\nself.speed += 1")])
    check(msg == "", "(a REPLACE less indented than an EXACT SEARCH is the model's business)")
    less = [("  def fall(self):\n      self.speed += 1", "  def fall(self):\nself.speed += 1")]
    msg = refused(pe.apply_edits, PROGRAM, less)
    check("cannot be moved" in msg,
          f"but one less indented than a SEARCH that matched only once moved is refused ({msg[:50]!r})")
    uneven = [("def fall(self):\n            self.speed += 1", "x")]
    check("is not in the file" in refused(pe.apply_edits, PROGRAM, uneven),
          "lines moved by DIFFERENT amounts are not a match")

    msg = refused(pe.apply_edits, PROGRAM, [("    def fall(self):\n        self.speed -= 1", "x")])
    check("`def fall(self):`, is line 12" in msg,
          f"a SEARCH whose first line is there but the rest differs says where ({msg[:80]!r})")
    msg = refused(pe.apply_edits, PROGRAM, [("    def jump(self):", "x")])
    check("no line of the file reads `def jump(self):`" in msg,
          f"one whose first line is nowhere says that ({msg[:80]!r})")
    check("empty SEARCH" in refused(pe.apply_edits, PROGRAM, [("  \n", "x = 1")]),
          "an empty SEARCH names no place, and is refused")

    padded = [("\n\n        self.speed = -8\n\n", "\n        self.speed = -9\n")]
    check(pe.apply_edits(PROGRAM, padded) == PROGRAM.replace("= -8", "= -9"),
          "blank lines at the ends of SEARCH and REPLACE are not part of the match")
    gone = pe.apply_edits(PROGRAM, [("    def flap(self):\n        self.speed = -8\n", "")])
    check("flap" not in gone and gone.count("\n") == PROGRAM.count("\n") - 2,
          "an empty REPLACE deletes the lines, leaving no blank line in their place")

    chained = pe.apply_edits(PROGRAM, [
        ("        self.speed = -8", "        self.speed = -8\n        self.flaps = 1"),
        ("        self.flaps = 1", "        self.flaps += 1")])
    check("self.flaps += 1" in chained and "self.flaps = 1" not in chained,
          "each edit applies to the text the edits before it left")
    msg = refused(pe.apply_edits, PROGRAM, [("passed * 10", "passed * 5"),
                                            ("passed * 10", "passed * 2")])
    check("edit 2" in msg and "not in the file" in msg,
          "an edit to text an earlier one replaced is refused, not applied elsewhere")
    msg = refused(pe.apply_edits, PROGRAM, [("passed * 10", "passed * 5"),
                                            ("    def nowhere(self):", "x")])
    check("edit 2" in msg, "a second edit that fails refuses the whole patch (nothing is returned)")

    feed = "a = 1\n\x0c\nb = 2\nb = 2\n"
    msg = refused(pe.apply_edits, feed, [("b = 2", "b = 3")])
    check("lines 3, 4" in msg,
          f"lines are counted by newline only, so a form feed does not shift them ({msg[:60]!r})")


# ── 3. the outline ──────────────────────────────────────────────────────────

JS = src("""
    const W = 400;
    function draw(ctx) {
      ctx.fillRect(0, 0, W, W);
    }
    async function load() { return 1; }
    class Game {
      update(dt) {
        if (dt > 1) {
          dt = 1;
        }
      }
    }
    const step = (dt) => {
      return dt * 2;
    };
    let tick = function () {
      return 0;
    };
""")


def test_outline() -> None:
    check.section("3. outline_parts and outline_text: what a long file's outline shows")
    py = PROGRAM.replace("    def flap(self):", "    @staticmethod\n    def flap(self):")
    parts = {p.name: p for p in pe.outline_parts(py, "python")}
    check(set(parts) == {"Bird", "__init__", "flap", "fall", "score", "__main__"},
          f"Python: every class, method, function, and the main guard ({sorted(parts)})")
    check(parts["flap"].start == parts["flap"].head - 1 and parts["flap"].line == "    def flap(self):",
          "a decorated function starts at its decorator; its line is copied exactly")
    lines = py.split("\n")
    check(lines[parts["Bird"].end - 1].strip() == "self.y += self.speed"
          and lines[parts["score"].end - 1].strip() == "return passed * 10",
          "and each ends where the syntax tree says it does")

    broken = PROGRAM.replace("def score(passed):", "def score(passed)")
    heads = [(p.name, p.head, p.end) for p in pe.outline_parts(broken, "python")]
    check(("score", 17, 17) in heads and ("Bird", 4, 4) in heads,
          f"a file that does not parse is outlined by its def lines, ends unknown ({heads[:2]})")

    js = {p.name: p for p in pe.outline_parts(JS, "js")}
    check({"draw", "load", "Game", "update", "step", "tick"} <= set(js) and "if" not in js,
          f"JS: functions, a class, a method, arrow and function expressions; `if (...) {{` is not one ({sorted(js)})")
    check(js["draw"].end == 4 and js["Game"].end == 12 and js["update"].end == 11
          and js["step"].end == 15 and js["load"].end == js["load"].head,
          "each ends at the brace closing it at its own indentation; a one-line one on its line")
    page = "<html>\n<style>\nb {}\n</style>\n<script>\n" + JS + "</script>\n</html>\n"
    names = [p.name for p in pe.outline_parts(page, "html")]
    check(names[:2] == ["style", "/style"] and "script" in names and "draw" in names,
          f"a page: its style and script sections, and the functions in them ({names[:4]})")
    check(pe.outline_parts("just some notes\n", "text") == [], "a text file has no outline")

    text = pe.outline_text(pe.outline_parts(PROGRAM, "python"), 10_000)
    check("    4 | class Bird:" in text and "   12 |     def fall(self):" in text,
          "each row is the line number, a bar, and the line exactly")
    many = "".join(f"def f{i}(x):\n    return x\n\n\n" for i in range(200))
    parts = pe.outline_parts(many, "python")
    text = pe.outline_text(parts, 1500, keep=[parts[150].head])
    gaps = [int(n) for n in re.findall(r"\.\.\. (\d+) more", text)]
    shown = re.findall(r"\| def (f\d+)\(", text)
    check(len(text) <= 1500 and "f150" in shown,
          f"over its cap, the outline keeps the part being shown ({len(text)} chars)")
    check(len(shown) + sum(gaps) == 200, "and every row it leaves out is counted in a gap")
    scattered = pe.outline_text(parts, 1500, keep=[p.head for p in parts[::3]])
    check(len(scattered) <= 1500 and scattered.count("... ") > 30,
          f"kept rows scattered through the file open a gap after nearly every one, and "
          f"that still fits its cap ({len(scattered)} chars, {scattered.count('... ')} gaps)")
    nested = "class A:\n" + "".join(f"    def m{i}(self):\n        pass\n" for i in range(60)) + \
        "".join(f"def top{i}():\n    pass\n" for i in range(5))
    text = pe.outline_text(pe.outline_parts(nested, "python"), 600)
    check(all(f"def top{i}()" in text for i in range(5)) and "class A:" in text,
          "after those, top-level parts are kept before nested ones")
    check("(no functions or classes were found)" in pe.outline_text([], 500),
          "an empty outline says so")


# ── 4. the view of a long file ──────────────────────────────────────────────

def _chunks(view: str) -> list[tuple[int, int, str]]:
    return [(int(a), int(b), body) for a, b, body in re.findall(
        r"Lines (\d+)-(\d+), exactly as in the file:\n```\w*\n(.*?)\n```", view, re.DOTALL)]


def test_view() -> None:
    check.section("4. file_view: the whole file, or its outline and the parts that matter")
    view, whole = pe.file_view(PROGRAM, kind="python", budget=len(PROGRAM))
    check(whole is True and view == PROGRAM, "a file that fits is shown whole, exactly")

    big = "import random\n\n\n" + "".join(
        f"def f{i}(x):\n    y = x + {i}\n    return y * 2\n\n\n" for i in range(300))
    lines = big.split("\n")
    crash = big.split("\n").index("    y = x + 150") + 1
    view, whole = pe.file_view(big, kind="python", budget=4000, focus_lines=[crash])
    got = _chunks(view)
    check(whole is False and view.startswith("Outline:") and len(view) <= 4000,
          f"one that does not: the outline first, inside the budget ({len(view)} chars)")
    check(any(a <= crash <= b for a, b, _ in got) and "def f150(x):" in view
          and "    return y * 2" in view,
          "the function holding the focus line is shown")
    check("    y = x + 149" not in view and "    y = x + 151" not in view,
          "that function alone, not the lines around it, when it is short enough to be 'the part'")
    check(all(body == "\n".join(lines[a - 1:b]) for a, b, body in got) and got,
          "every part shown is copied exactly, with its true line numbers")
    check("def f20(x):\n    y = x + 20" not in view, "and the parts far from it are not")

    view, _ = pe.file_view(big, kind="python", budget=4000, focus_names=["f7", "nope", "f8"])
    check("    y = x + 7" in view and "    y = x + 8" in view,
          "named parts are shown; a name that is not in the file is skipped")
    view, _ = pe.file_view(big, kind="python", budget=4000)
    check(any(a == 1 for a, _b, _ in _chunks(view)) and "import random" in view,
          "with nothing to focus on, the top of the file is shown")

    huge = "def run():\n" + "".join(f"    step_{i} = {i}\n" for i in range(400)) + "\n\nrun()\n"
    lines = huge.split("\n")
    view, _ = pe.file_view(huge, kind="python", budget=2500, focus_lines=[300])
    got = _chunks(view)
    check(any(a <= 300 <= b for a, b, _ in got) and "    step_298 = 298" in view
          and "    step_5 = 5" not in view,
          "a part too long to be 'the part' is shown as the lines around the focus")
    view, _ = pe.file_view(huge, kind="python", budget=2500, focus_lines=[0, 9999])
    check([(a, b) for a, b, _ in _chunks(view)] == [(1, 40)],
          "focus lines outside the file are ignored: it is as if there were none")
    view, _ = pe.file_view(huge, kind="python", budget=1200, focus_names=["run"])
    got = _chunks(view)
    check(len(view) <= 1200 and got and got[0][0] == 1,
          f"a named part longer than the budget: as much of it as fits, from its head ({len(view)})")
    view, _ = pe.file_view(huge, kind="python", budget=800, focus_lines=[300])
    check(len(view) <= 800 and "    step_290 = 290" in view and "    step_310 = 310" in view,
          "lines around a focus that do not all fit are taken from BOTH sides of it")

    cls = "class Game:\n" + "".join(f"    def m{i}(self):\n        return {i}\n\n" for i in range(50))
    at = cls.split("\n").index("        return 25") + 1
    view, _ = pe.file_view(cls, kind="python", budget=1500, focus_lines=[at])
    check("        return 25" in view and "        return 21\n" not in view,
          "a line in a method shows that method, not the class around it")
    view, _ = pe.file_view(cls, kind="python", budget=1000, focus_names=["Game", "m40"])
    check("    def m40(self):\n        return 40\n" in view and "class Game:" in view,
          "a class asked for BEFORE its own method is shown after it, so the method is not "
          "crowded out (measured: flappy-bird's class hid the method the change was in)")
    with_fn = cls + "\n\ndef helper():\n    return 'h'\n"
    at40 = with_fn.split("\n").index("        return 40") + 1
    view, _ = pe.file_view(with_fn, kind="python", budget=1000, focus_lines=[at40],
                           focus_names=["Game", "helper"])
    check("def helper():\n    return 'h'" in view and "        return 40" in view,
          "and so is a class holding the focus line: it does not crowd out another part asked for")

    filler = "".join(f"def g{i}():\n    return {i}\n\n\n" for i in range(200))
    longfn = "def run():\n" + "".join(f"    s{i} = {i}\n" for i in range(200)) + "\n\n" + filler
    view, _ = pe.file_view(longfn, kind="python", budget=5000, focus_names=["run"])
    check("    s150 = 150" in view and "    s170 = 170" not in view,
          "a named part is shown for at most 160 lines, even when more would fit")


# ── 5. page lines, for a page too long to repair whole ──────────────────────

def test_page_lines() -> None:
    check.section("5. page_lines: the smoke test's script line, as a line of the page")
    page = src("""
        <html>
        <body>
        <canvas id="c"></canvas>
        <script>
        const a = 1;
        function loop() {
          undefinedHelper();
        }
        </script>
        <script>
        const b = 2;
        broken();
        </script>
        </body></html>
    """)
    lines = page.split("\n")
    run = ["ReferenceError: undefinedHelper is not defined | at loop (inline-script-1.js:3:3)"]
    got = page_lines(page, run)
    check(got == [lines.index("  undefinedHelper();") + 1],
          f"a stack's inline-script-1.js:3 is the page line of that call ({got})")
    syntax = ["inline-script-2: C:\\Temp\\nova-html-smoke-x\\inline-script-2.js:2"]
    got = page_lines(page, syntax)
    check(got == [lines.index("broken();") + 1],
          f"node --check's form names the second script's line ({got})")
    got = page_lines(page, ["at x (inline-script-1.js:2:1)", "at y (inline-script-2.js:2:1)"],
                     local_scripts=1)
    check(got == [lines.index("function loop() {") + 1],
          f"with one project .js file loaded first, script 2 is the first inline one ({got})")
    check(page_lines(page, ["at z (inline-script-9.js:1:1)", "no location"]) == [],
          "a script the page does not have, or no location: nothing")
    twin = page.replace("const b = 2;\nbroken();", "const a = 1;\nfunction loop() {\n  undefinedHelper();\n}")
    tl = twin.split("\n")
    got = page_lines(twin, ["at loop (inline-script-2.js:3:3)"])
    check(got == [len(tl) - tl[::-1].index("  undefinedHelper();")],
          f"two identical scripts: the second one's line is in the second ({got})")


# ── 6. the builder's budget and prompt helpers ──────────────────────────────

class _Runtime:
    def __init__(self, ctx: Any) -> None:
        self.context_tokens = ctx


def test_builder_helpers() -> None:
    check.section("6. the builder's patch budget, compile check and prompt")
    spare = builder_mod._FILE_TOKENS + builder_mod._PATCH_PROMPT_TOKENS
    budget = builder_mod._patch_view_budget
    check(budget(_Runtime(8192), "python") == int((8192 - spare) * 3.2)
          and budget(_Runtime(8192), "html") == int((8192 - spare) * 2.5),
          f"8,192 tokens: about {int((8192 - spare) * 3.2)} characters of Python, "
          f"{int((8192 - spare) * 2.5)} of HTML, the reply's budget kept")
    check(budget(_Runtime(16384), "python") > budget(_Runtime(8192), "python"),
          "a larger context shows more")
    saved = os.environ.get("NOVA_CONTEXT_TOKENS")
    try:
        os.environ["NOVA_CONTEXT_TOKENS"] = "12000"
        check(budget(object(), "python") == int((12000 - spare) * 3.2),
              "a runtime that reports no window gets the configured one")
        check(budget(_Runtime("lots"), "python") == int((12000 - spare) * 3.2),
              "and so does one that reports something that is not a number")
        os.environ["NOVA_CONTEXT_TOKENS"] = "eight thousand"
        check(budget(object(), "python") == int((8192 - spare) * 3.2),
              "a malformed setting falls back to the documented 8,192")
        os.environ["NOVA_CONTEXT_TOKENS"] = "100"
        check(budget(object(), "python") == 2000, "and nothing shows less than 2,000 characters")
    finally:
        if saved is None:
            os.environ.pop("NOVA_CONTEXT_TOKENS", None)
        else:
            os.environ["NOVA_CONTEXT_TOKENS"] = saved

    err = builder_mod._compile_error_text
    check(err("x = 1\n", "m.py") is None, "valid Python: no compile error")
    got = err("def f(:\n    pass\n", "m.py")
    check(got is not None and got.startswith("SyntaxError") and "(line 1)" in got,
          f"a syntax error is named with its line ({got!r})")
    check(err("x = 1\x00\n", "m.py") is not None, "a NUL byte is not 'compiles'")

    prompt = builder_mod._patch_prompt(rel="main.py", task="Fix the crash.", view=PROGRAM,
                                       whole=True, kind="python", n_lines=22,
                                       chars=len(PROGRAM), retry_reason="")
    check(f"```python\n{PROGRAM}\n```" in prompt and "Fix the crash." in prompt
          and "COMPLETE" not in prompt and "do NOT return the whole file" in prompt,
          "the whole-view prompt holds the file and the task, and never asks for the complete file")
    check("<<<<<<< SEARCH\n" in prompt and "\n=======\n" in prompt and ">>>>>>> REPLACE" in prompt,
          "and shows the edit block's exact form")
    part = builder_mod._patch_prompt(rel="main.py", task="t", view="Outline: ...", whole=False,
                                     kind="python", n_lines=900, chars=40_000,
                                     retry_reason="edit 1's SEARCH is not in the file")
    check("900 lines (40000 characters), too many to show whole" in part
          and "Outline: ..." in part,
          "a partial view says it is partial, and how long the file is")
    check("YOUR LAST EDITS COULD NOT BE USED: edit 1's SEARCH is not in the file." in part,
          "a retry is told exactly why the last edits were refused")


def main() -> None:
    test_parsing()
    test_applying()
    test_outline()
    test_view()
    test_page_lines()
    test_builder_helpers()
    check.finish()


if __name__ == "__main__":
    main()
