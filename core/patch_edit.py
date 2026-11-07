from __future__ import annotations

"""Find-and-replace edits, for files too long to rewrite whole.

WHY THIS EXISTS (test program, 2026-11-07)
------------------------------------------
Every repair, fix and improvement the project builder makes used to ask the
model for "the COMPLETE corrected file". For a long file that cannot work, for
two reasons that do not depend on each other:

* The prompts showed the file cut to a window of 6,000 to 9,000 characters, so
  the model never saw the end of a longer one and could not return it. An
  improvement to an 11,152-character main.py kept the 51 of its 80 functions
  it had been shown, and logged "Changed: main.py. Run check passed."
* The reply has 3,000 tokens, shared with the model's reasoning. Measured with
  the model's own tokenizer on the projects on this machine, Python runs about
  4 characters to a token and HTML about 2.8, so a 12,000-character program is
  already as long as the whole reply could be.

So a long file is changed here instead. The model is shown the file, or, when
even that does not fit, its outline and the parts the change concerns. It
replies with small SEARCH/REPLACE blocks, and they are applied to the FULL
file on disk. Nothing the model was not shown can be lost, because nothing it
does not name is touched.

WHAT AN EDIT MUST PASS
----------------------
* Every SEARCH matches exactly ONE place. An edit that could mean two places is
  refused, not guessed.
* All the edits apply, or none do. Half an improvement is a program nobody
  asked for.
* The match is exact. Failing that: exact text inside a line; then the lines
  with whitespace at their ends ignored and, if need be, all moved by one
  consistent indentation, with the replacement moved the same way. The first
  of those that finds anything decides. Nothing looser: a fuzzy match is an
  edit somewhere the model did not mean.

Every refusal says what was wrong in words the model is shown on its retry.
"""

import ast
import os
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

#: The three marker lines of an edit block. Five to nine marker characters,
#: because a model that writes six or eight means the same thing. The divider
#: must start its line: an indented row of "=" is a line of code, such as a
#: docstring's underline, not the middle of an edit.
_SEARCH_RE = re.compile(r"^[ \t]*<{5,9}[ \t]*SEARCH[ \t]*$")
_DIVIDER_RE = re.compile(r"^={5,9}[ \t]*$")
_REPLACE_RE = re.compile(r"^[ \t]*>{5,9}[ \t]*REPLACE[ \t]*$")


class PatchError(ValueError):
    """Edits that cannot be used. The message is what the model is told on its
    retry, so it names the edit and says what was wrong with it."""


# ── parsing ─────────────────────────────────────────────────────────────────

def parse_edits(reply: str) -> list[tuple[str, str]]:
    """The SEARCH/REPLACE blocks in a reply, in order, as (search, replace).

    Anything outside a block, prose or code fences, is ignored. A reply that
    stops inside a block was cut off, and is refused whole rather than used in
    part: the edits before the cut were written to go with the one after it.
    """
    edits: list[tuple[str, str]] = []
    state = ""
    search: list[str] = []
    replace: list[str] = []
    for line in (reply or "").replace("\r\n", "\n").split("\n"):
        if state == "":
            if _SEARCH_RE.match(line):
                state, search = "search", []
        elif state == "search":
            if _DIVIDER_RE.match(line):
                state, replace = "replace", []
            elif _SEARCH_RE.match(line) or _REPLACE_RE.match(line):
                raise PatchError(f"edit {len(edits) + 1} has no ======= line between "
                                 "its SEARCH part and its REPLACE part")
            else:
                search.append(line)
        else:
            if _REPLACE_RE.match(line):
                edits.append(("\n".join(search), "\n".join(replace)))
                state = ""
            elif _SEARCH_RE.match(line):
                raise PatchError(f"edit {len(edits) + 1} has no >>>>>>> REPLACE line "
                                 "to end it")
            else:
                replace.append(line)
    if state:
        raise PatchError(f"the reply stops in the middle of edit {len(edits) + 1}, "
                         "so it was cut off")
    if not edits:
        raise PatchError("the reply has no edit blocks in it")
    return edits


# ── applying ────────────────────────────────────────────────────────────────

def apply_edits(text: str, edits: Iterable[tuple[str, str]]) -> str:
    """`text` with every edit applied, in order: all of them, or PatchError.

    Each edit applies to the text as the edits before it left it. So two edits
    to neighbouring lines both work, and an edit to text an earlier one already
    replaced fails with a reason instead of landing somewhere else.
    """
    out = text
    for n, (search, replace) in enumerate(edits, start=1):
        out = _apply_one(out, search, replace, n)
    return out


def _apply_one(text: str, search: str, replace: str, n: int) -> str:
    want = _trim_blank_ends(search.split("\n"))
    put = _trim_blank_ends(replace.split("\n"))
    if not want:
        raise PatchError(f"edit {n} has an empty SEARCH, which names no place in the file")
    ends_nl = text.endswith("\n")
    lines = split_lines(text)
    k = len(want)
    starts = range(len(lines) - k + 1)

    def spliced(at: int, new: list[str]) -> str:
        out = lines[:at] + new + lines[at + k:]
        return "\n".join(out) + ("\n" if ends_nl and out else "")

    # 1. The lines, exactly.
    hits = [i for i in starts if lines[i:i + k] == want]
    if hits:
        return spliced(_only(hits, n), put)

    # 2. Exact text inside a line, or running across lines: a model that
    #    copies `total / count` out of the line it is in.
    needle = "\n".join(want)
    count = text.count(needle)
    if count == 1:
        return text.replace(needle, "\n".join(put), 1)
    if count > 1:
        raise PatchError(f"edit {n}'s SEARCH matches {count} places; include more of "
                         "the lines around it so it matches only one")

    # 3. The lines, ignoring whitespace at their ends, and all moved by one
    #    indentation if need be: a model that copied a method without its
    #    class's four spaces. REPLACE is moved the same way. (Moved by nothing,
    #    this is the lines with only their trailing whitespace ignored.)
    moved = [(i, off) for i in starts
             if (off := _offset(lines[i:i + k], want)) is not None]
    if moved:
        at = _only([i for i, _ in moved], n)
        prefix, base = dict(moved)[at]
        return spliced(at, _reindent(put, base=base, prefix=prefix, n=n))

    raise _missing(n, want, lines)


def _only(hits: list[int], n: int) -> int:
    if len(hits) > 1:
        at = ", ".join(str(h + 1) for h in hits[:4])
        raise PatchError(f"edit {n}'s SEARCH matches {len(hits)} places (lines {at}); "
                         "include a line or two around the change so it matches only one")
    return hits[0]


def split_lines(text: str) -> list[str]:
    """The lines of `text`, counted the way Python and node count them: by
    newline only. `str.splitlines` also breaks at a form feed or a U+2028, so a
    file holding one would have every line number after it out by one."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _trim_blank_ends(lines: list[str]) -> list[str]:
    a, b = 0, len(lines)
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return lines[a:b]


def _lead(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _offset(window: list[str], want: list[str]) -> tuple[str, str] | None:
    """(the file's indentation, the SEARCH's) when `window` is `want` with one
    for the other on every line, or None."""
    base = os.path.commonprefix([_lead(w) for w in want if w.strip()])
    prefix: str | None = None
    for have, w in zip(window, want):
        if not w.strip():
            if have.strip():
                return None
            continue
        body = w.rstrip()[len(base):]
        line = have.rstrip()
        if not line.endswith(body):
            return None
        p = line[: len(line) - len(body)]
        if p.strip() or (prefix is not None and p != prefix):
            return None
        prefix = p
    return None if prefix is None else (prefix, base)


def _reindent(put: list[str], *, base: str, prefix: str, n: int) -> list[str]:
    out: list[str] = []
    for r in put:
        if not r.strip():
            out.append("")
        elif r.startswith(base):
            out.append(prefix + r[len(base):])
        else:
            raise PatchError(f"edit {n}'s SEARCH only matched with a different "
                             "indentation, and its REPLACE is indented less than its "
                             "SEARCH, so it cannot be moved the same way; copy the "
                             "indentation exactly")
    return out


def _missing(n: int, want: list[str], lines: list[str]) -> PatchError:
    first = next(w.strip() for w in want if w.strip())
    shown = first if len(first) <= 70 else first[:67] + "..."
    at = [i + 1 for i, ln in enumerate(lines) if ln.strip() == first]
    if at:
        where = ", ".join(str(a) for a in at[:3])
        return PatchError(f"edit {n}'s SEARCH is not in the file: its first line, "
                          f"`{shown}`, is line {where}, but the lines after it are "
                          "different there; copy the SEARCH lines exactly")
    return PatchError(f"edit {n}'s SEARCH is not in the file: no line of the file "
                      f"reads `{shown}`; copy the SEARCH lines exactly from the file")


# ── what the model is shown ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Part:
    """A function, class or section of a file, by 1-based line number."""

    name: str
    head: int     # the line that opens it
    start: int    # its first line, decorators included
    end: int      # its last line; equal to `head` when it is not known
    line: str     # the opening line, exactly as it is in the file


def file_kind(rel: str) -> str:
    low = rel.lower()
    if low.endswith(".py"):
        return "python"
    if low.endswith((".html", ".htm")):
        return "html"
    if low.endswith((".js", ".mjs")):
        return "js"
    return "text"


_PY_HEAD_RE = re.compile(r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_]\w*)")
_PY_MAIN_RE = re.compile(r"^if[ \t]+__name__[ \t]*==")
_JS_HEADS = (
    re.compile(r"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function\b[ \t]*\*?[ \t]*"
               r"([A-Za-z_$][\w$]*)[ \t]*\("),
    re.compile(r"^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)"),
    re.compile(r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*="
               r"[ \t]*(?:async\b[ \t]*)?(?:function\b|\([^)]*\)[ \t]*=>|[A-Za-z_$][\w$]*[ \t]*=>)"),
    # A method in a class body. The words that also take "(...) {" are not names.
    re.compile(r"^[ \t]+(?:static[ \t]+)?(?:async[ \t]+)?"
               r"(?!(?:if|for|while|switch|catch|function|return|else|do|try|with)\b)"
               r"([A-Za-z_$][\w$]*)[ \t]*\([^)]*\)[ \t]*\{[ \t]*$"),
)
_TAG_RE = re.compile(r"^[ \t]*<(/?(?:script|style|body|canvas))\b", re.IGNORECASE)


def outline_parts(text: str, kind: str) -> list[Part]:
    """Every function, class and (for a page) script or style section, in order."""
    lines = split_lines(text)
    if kind == "python":
        parts = _python_parts(text, lines)
        return parts if parts is not None else _python_heads(lines)
    if kind in ("html", "js"):
        return _js_parts(lines, tags=kind == "html")
    return []


def _python_parts(text: str, lines: list[str]) -> list[Part] | None:
    """From the syntax tree, which knows where each one ENDS. None when the
    file does not parse, which is the case a syntax repair is for."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):  # quiet-ok: a file that does not parse is outlined by its def lines instead
        return None
    parts: list[Part] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            parts.append(Part(node.name, node.lineno, start,
                              node.end_lineno or node.lineno, lines[node.lineno - 1]))
    for node in tree.body:
        if isinstance(node, ast.If) and _PY_MAIN_RE.match(lines[node.lineno - 1]):
            parts.append(Part("__main__", node.lineno, node.lineno,
                              node.end_lineno or node.lineno, lines[node.lineno - 1]))
    return sorted(parts, key=lambda p: (p.head, -p.end))


def _python_heads(lines: list[str]) -> list[Part]:
    parts = []
    for i, line in enumerate(lines, start=1):
        m = _PY_HEAD_RE.match(line)
        if m:
            parts.append(Part(m.group(1), i, i, i, line))
        elif _PY_MAIN_RE.match(line):
            parts.append(Part("__main__", i, i, i, line))
    return parts


def _js_parts(lines: list[str], *, tags: bool) -> list[Part]:
    parts = []
    for i, line in enumerate(lines, start=1):
        name = next((m.group(1) for rx in _JS_HEADS if (m := rx.match(line))), None)
        if name is not None:
            parts.append(Part(name, i, i, _brace_end(lines, i), line))
        elif tags and (m := _TAG_RE.match(line)):
            parts.append(Part(m.group(1).lower(), i, i, i, line))
    return parts


def _brace_end(lines: list[str], head: int) -> int:
    """The line closing a block opened on line `head`: the first later line at
    the same indentation that starts with `}`. Generated code is indented
    consistently; when nothing closes it nearby, the end is not known."""
    line = lines[head - 1]
    if line.count("{") <= line.count("}"):
        return head
    lead = _lead(line)
    for j in range(head, min(len(lines), head + 800)):
        nxt = lines[j]
        if _lead(nxt) == lead and nxt.lstrip().startswith("}"):
            return j + 1
    return head


def outline_text(parts: list[Part], cap: int, *, keep: Iterable[int] = ()) -> str:
    """The outline as the model reads it, in about `cap` characters.

    When every row does not fit, the rows kept are, in order: the parts opening
    on a line in `keep` (the ones being shown), then the top-level ones, then
    the rest. Cutting from the top instead dropped the very function a crash
    was in from the outline of a long file. A gap says how many rows it hides.
    """
    head = ("Outline: the line that opens each function and class, with its line "
            "number. The text after `|` is exactly that line of the file, so it can "
            "be searched for too.")
    if not parts:
        return head + "\n(no functions or classes were found)"
    rows = [f"{p.head:>5} | {p.line.rstrip()}" for p in parts]
    if len(head) + sum(len(r) + 1 for r in rows) <= cap:
        return head + "\n" + "\n".join(rows)
    kept = set(keep)
    order = sorted(range(len(parts)), key=lambda i: (parts[i].head not in kept,
                                                     _lead(parts[i].line) != "", i))
    chosen: set[int] = set()
    size = len(head) + 1 + 16                  # its newline, and a closing gap
    for i in order:
        cost = len(rows[i]) + 1 + 16           # the row, and a gap it may open
        if size + cost <= cap:
            chosen.add(i)
            size += cost
    out: list[str] = []
    gap = 0
    for i, row in enumerate(rows):
        if i not in chosen:
            gap += 1
            continue
        if gap:
            out.append(f"  ... {gap} more")
            gap = 0
        out.append(row)
    if gap:
        out.append(f"  ... {gap} more")
    return head + "\n" + "\n".join(out)


#: A part longer than this is not "the part this concerns": a crash in a
#: 400-line class is shown as the lines around it.
_MAX_PART_LINES = 160
_BEFORE, _AFTER = 30, 15
#: What one shown region's heading and fence cost, besides its lines.
_REGION_COST = 60


def file_view(text: str, *, kind: str, budget: int,
              focus_lines: Iterable[int] = (),
              focus_names: Iterable[str] = ()) -> tuple[str, bool]:
    """What a patch prompt shows of `text`, and whether that is all of it.

    The whole file when it fits in `budget` characters. Otherwise its outline,
    then the parts that matter, each copied exactly: the part around each of
    `focus_lines` (a crash line, a syntax error), then each part named in
    `focus_names`, in the order given except that one holding another comes
    after it, while they fit. With neither, the top of the file. A part too
    long to fit is shown as the lines nearest its focus.
    """
    if len(text) <= budget:
        return text, True
    lines = split_lines(text)
    n = len(lines)
    parts = outline_parts(text, kind)

    wanted: list[tuple[int, int, int]] = []          # (first, last, anchor)
    in_file = [ln for ln in dict.fromkeys(focus_lines) if 1 <= ln <= n]
    for ln in in_file:
        wanted.append(_around(parts, ln, n))
    named = [p for name in dict.fromkeys(focus_names) for p in parts if p.name == name]

    def holds_another(p: Part) -> bool:
        return (any(q is not p and p.start <= q.start and q.end <= p.end for q in named)
                or any(p.start <= ln <= p.end for ln in in_file))

    # A part that HOLDS another wanted one comes after it. Measured with the
    # real model on flappy-bird: it asked for the game's class first and then
    # three of its methods, the class filled the whole view from its first
    # line, and the method the change had to edit was never shown. The order
    # the parts were asked for is kept otherwise.
    for p in sorted(named, key=holds_another):
        last = p.end if p.end > p.head else min(n, p.head + 40)
        wanted.append((p.start, min(last, p.start + _MAX_PART_LINES - 1), p.head))
    if not wanted:
        wanted.append((1, min(n, 40), 1))             # its imports and constants

    outline = outline_text(parts, budget // 3, keep=[
        p.head for p in parts if any(a <= p.head <= b for a, b, _ in wanted)])
    room = budget - len(outline) - _REGION_COST * len(wanted)
    shown: set[int] = set()
    for first, last, anchor in wanted:
        new = [i for i in range(first, last + 1) if i not in shown]
        cost = sum(len(lines[i - 1]) + 1 for i in new)
        if cost <= room:
            shown.update(new)
            room -= cost
            continue
        for i in _outward(anchor, first, last):     # as much as fits, nearest first
            if i in shown:
                continue
            c = len(lines[i - 1]) + 1
            if c > room:
                break
            shown.add(i)
            room -= c

    fence = {"python": "python", "html": "html", "js": "javascript"}.get(kind, "")
    blocks = [f"Lines {a}-{b}, exactly as in the file:\n```{fence}\n"
              + "\n".join(lines[a - 1:b]) + "\n```"
              for a, b in _runs(sorted(shown))]
    return outline + "\n\n" + "\n\n".join(blocks), False


def _around(parts: list[Part], ln: int, n: int) -> tuple[int, int, int]:
    """The innermost function or class holding line `ln`, or the lines around it."""
    holding = [p for p in parts if p.start <= ln <= p.end and p.end > p.head]
    if holding:
        p = min(holding, key=lambda q: q.end - q.start)
        if p.end - p.start + 1 <= _MAX_PART_LINES:
            return p.start, p.end, ln
    return max(1, ln - _BEFORE), min(n, ln + _AFTER), ln


def _outward(anchor: int, first: int, last: int) -> Iterator[int]:
    yield anchor
    for d in range(1, last - first + 1):
        for i in (anchor - d, anchor + d):
            if first <= i <= last:
                yield i


def _runs(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for x in nums:
        if out and x == out[-1][1] + 1:
            out[-1] = (out[-1][0], x)
        else:
            out.append((x, x))
    return out
