from __future__ import annotations

"""Nova's autonomous project builder.

When Marcus asks Nova to build something ("make a snake game called Serpent"),
she creates projects/<slug>/, plans the files with the LLM, writes them,
validates Python files compile, records progress in PROJECT.md and project
memory facts, then reports completion with improvement suggestions.

State model:
- COMPLETION STATE IS NOT STORED HERE. Since Stage 14 it is derived by
  core.completion_service.CompletionService from durable acceptance criteria
  and evidence, every time it is asked for. This module records what it
  observed and asks; it cannot assign "complete".
- projects/<slug>/PROJECT.md is a PROJECTION of that verdict plus the things
  it genuinely is the record of — the brief, the file list, how to run it, the
  progress narrative and suggestions — so "where did we leave off?" works
  across sessions. It was described here as "the on-disk source of truth" and
  that is no longer true of its status line.
- Memory facts (entity="project:<slug>") likewise mirror the derived state so
  chat grounding and semantic search can surface it.
- All writes are confined to projects_dir (slug-sanitized, no traversal).
"""

import ast
import asyncio
import os
import re
from core.project_names import (
    WIN_RESERVED, canonical_project_slug, is_project_dir,
    list_project_dirs, safe_live_component,
)
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from core.event_bus import BUS, clip
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object

logger = get_logger(__name__)

_MAX_FILES = 5
# Budget shared by background reasoning AND the file content — too small and
# long files arrive truncated after a long think.
_FILE_TOKENS = int(os.getenv("NOVA_PROJECT_FILE_TOKENS", "3000").strip() or "3000")
_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9+\-_.]*)\n(.*?)```", re.DOTALL)
_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")


#: Packages a small generated project might reasonably reach for. Probed, not
#: assumed: the note names what is importable RIGHT HERE and nothing else.
#: Order is the order they are listed in, so the sentence reads the same way
#: every time.
_CANDIDATE_PACKAGES = (
    "pygame", "numpy", "pandas", "matplotlib", "requests", "httpx",
    "flask", "fastapi", "PIL", "rich", "pydantic", "bs4", "pytest",
)

#: Printed names, where the import name is not what a person would write.
_PACKAGE_LABELS = {"PIL": "pillow (PIL)", "bs4": "beautifulsoup4 (bs4)"}


def _installed_packages() -> list[str]:
    """Which of `_CANDIDATE_PACKAGES` can actually be imported."""
    import importlib.util

    found = []
    for name in _CANDIDATE_PACKAGES:
        try:
            if importlib.util.find_spec(name) is not None:
                found.append(name)
        except (ImportError, ValueError):
            # A broken or namespace-shadowed install is not something to plan
            # around. Treat it as absent.
            continue
    return found


def _python_stack_note() -> str:
    """Tell the planner what this machine actually has.

    This used to say "ONLY the standard library ... third-party packages are
    NOT installed" whatever was installed. pytest is installed, and the test
    runner now REQUIRES pytest-style tests, so the planner was being told the
    opposite of the truth and, with thinking on, looped on the contradiction —
    138 mentions of pytest in 25k characters, then finish=length (audit, H15).

    tkinter is checked separately: it ships with the standard library but is a
    genuinely optional build component on some Pythons.
    """
    import importlib.util

    available = _installed_packages()
    tk = importlib.util.find_spec("tkinter") is not None
    has_pytest = "pytest" in available
    libs = [_PACKAGE_LABELS.get(n, n) for n in available if n != "pytest"]

    parts = ["use python"]
    if libs:
        parts.append("these third-party packages ARE installed and may be imported: "
                     + ", ".join(libs))
    else:
        parts.append("no third-party runtime packages are installed, so use the "
                     "standard library")
    parts.append("tkinter is available for graphical programs" if tk
                 else "tkinter is NOT available, so no desktop GUI")
    if "pygame" not in available:
        parts.append("pygame is NOT installed")
    parts.append("tests are run with pytest, so write pytest-style tests"
                 if has_pytest else
                 "pytest is NOT installed, so tests must run under unittest")
    parts.append("do not import anything not named here, it will fail at runtime")
    return "; ".join(parts)

PROJECT_VERBS = r"(?:make|create|build|code|write|develop|start|begin)"
PROJECT_OBJECTS = (
    r"(?:game|app|application|script|website|site|webpage|web\s+page|tool|program|project|bot|calculator|simulation|visualizer|dashboard)"
)
START_RE = re.compile(rf"\b{PROJECT_VERBS}\b[^.?!]*?\b{PROJECT_OBJECTS}\b", re.IGNORECASE)
#: Longest raw capture the unquoted parser will consider. Hitting it means the
#: sentence ran past what the pattern can represent, so the capture is a PREFIX
#: and must not be treated as a complete name.
_MAX_RAW_NAME = 40

#: Quoted names get their own pass, before anything generic. An explicit opening
#: and matching closing quote states the boundary, so the whole span is taken —
#: punctuation, dots and ampersands included. Bounded only for input safety.
#: DELIMITER-SPECIFIC. Only the matching quote closes the name, so the OTHER
#: quote character is ordinary text inside it. A single pattern with
#: `[^"'\n]` excluded both, which meant `"Marcus's Game"` did not match the
#: quoted parser at all and fell through to the generic one — returning the
#: prefix `Marcus` while the contract promised an exact quoted span.
QUOTED_NAME_RES = (
    re.compile(r"\b(?:call(?:ed)?|named?)\s+(?:it\s+)?\"(?P<qname>[^\"\n]{1,200})\"",
               re.IGNORECASE),
    re.compile(r"\b(?:call(?:ed)?|named?)\s+(?:it\s+)?'(?P<qname>[^'\n]{1,200})'",
               re.IGNORECASE),
)

#: An opening quote right after the marker. Used only to detect an UNMATCHED
#: quote, which must fail closed rather than fall through to a prefix.
_OPEN_QUOTE_RE = re.compile(r"\b(?:call(?:ed)?|named?)\s+(?:it\s+)?(?P<q>[\"'])",
                            re.IGNORECASE)
NAME_RE = re.compile(
    r"\b(?:call(?:ed)?|named?)\s+(?:it\s+)?[\"']?"
    rf"([A-Za-z0-9][A-Za-z0-9 _\-]{{1,{_MAX_RAW_NAME}}})[\"']?",
    re.IGNORECASE,
)
# Explicit "…project <Name>" phrasing, e.g. "let's start a project Serpent" or
# "create a project named Cobra". Captures the trailing name after "project".
PROJECT_NAME_RE = re.compile(
    r"\bproject\s+(?P<marker>called\s+|named\s+)?[\"']?(?P<name>[A-Za-z0-9][A-Za-z0-9 _\-]{0,40}?)[\"']?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
# A bare "…project X" has no marker saying X is a title, so it swallows whatever
# follows the word "project" up to the end of the sentence. That is how
# "create a new project and I want you to use python" became the project
# `and-i-want-you-to-use-python`, and how the malformed
# `blue-and-tower-defense-and-i-want-you-to/` directory got created.
#
# A title does not START with a conjunction, preposition, pronoun or relativiser.
# Fourteen phrasings were reproduced ("…project and then…", "…project so it…",
# "…project that tracks…", "…project for me please", "…project using rust", …) and
# every one begins with one of these. Nine legitimate names — including
# "My Personal Finance Dashboard 2026" and "to-do-list" — begin with none of them.
#
# This guard applies ONLY to the unmarked form. "called X" / "named X" states
# outright that X is the title, so it is trusted as given.
_NOT_A_NAME_LEAD = frozenset("""
    and or but so then also plus that which who whom whose what when while where
    with without for to from into onto about around after before during under over
    using via as if unless until because since though although
    i you we they he she it me my your our their his her its
    is are was were be been being do does did done have has had
    can could will would shall should may might must
    please just really actually maybe perhaps
""".split())


def _looks_like_a_title(name: str) -> bool:
    """Is this capture a project NAME, or the rest of the sentence?"""
    first = (name or "").strip().split()
    return bool(first) and first[0].lower() not in _NOT_A_NAME_LEAD


#: Replies that are an acknowledgement, not a name. Nova asks "what should I
#: call the project?" and the answer arrives bare, so the only thing standing
#: between "sounds good" and a project directory called `sounds-good` is this
#: list plus `_looks_like_a_title`.
_NOT_A_PROJECT_NAME = frozenset("""
    yes yeah yep yup sure ok okay okey fine good great cool nice perfect
    please thanks thank cheers alright right correct exactly indeed absolutely
    whatever anything something idk dunno hmm hm uh um well
    go ahead start begin proceed continue do it done
    sounds looks seems works fair
    project app game script tool program site website thing one it that this
""".split())

#: Determiners that can open a real title ("My Budget", "The Last Stand") and
#: so must not count as content when deciding whether a reply is all filler.
_LEADING_DETERMINERS = frozenset({"a", "an", "the", "my", "our", "your", "its"})

#: Replies that hand the choice back instead of making it. "whatever you
#: think" would otherwise pass the all-filler test — "you" and "think" are
#: content words — and create a project directory called `whatever-you-think`.
_DEFERS_CHOICE_RE = re.compile(
    r"^(?:(?:you|u)\s+(?:decide|choose|pick|name|call)|"
    r"(?:it'?s\s+)?up\s+to\s+you|your\s+(?:call|choice|pick)|surprise\s+me|"
    r"(?:i\s+)?(?:don'?t|do\s+not)\s+(?:mind|care|know)|doesn'?t\s+matter|"
    r"(?:whatever|anything|something|any\s*thing)\b|"
    r"(?:no|not\s+a)\s+(?:idea|clue|preference))",
    re.IGNORECASE)


def plausible_project_name(name: str) -> bool:
    """Could this be what the person wants the project called?

    Used where a name arrives with no "called X" marker to vouch for it — the
    answer to Nova's own "what should I call it?". A false positive here
    creates a real directory with a nonsense name and a memory entry to match,
    so this is deliberately narrow.

    Not `_looks_like_a_title`: that rejects a leading pronoun or possessive
    because mid-sentence it opens a clause, while a DIRECT answer may perfectly
    well be "My Personal Finance Dashboard 2026". What cannot open an answer is
    a conjunction, preposition or relativiser, which is a continuation of some
    other sentence.
    """
    t = (name or "").strip().strip("\"'")
    if not t or len(t) > 60 or t.endswith("?"):
        return False
    words = t.split()
    if len(words) > 5:
        return False
    if not re.match(r"[A-Za-z0-9]", t):
        return False
    if _DEFERS_CHOICE_RE.match(t):
        return False
    lowered = [w.lower().strip(".,;:!?\"'") for w in words]
    if lowered[0] in (_AMBIGUOUS_CONTINUATIONS | _CLAUSE_CONNECTIVES
                      | _ACTION_CONNECTIVES):
        return False
    # "sounds good", "go ahead", "the game", "that one" — nothing but filler
    # once a leading determiner is set aside.
    content = [w for w in lowered if w not in _LEADING_DETERMINERS]
    if not content or all(w in _NOT_A_PROJECT_NAME for w in content):
        return False
    return bool(canonical_project_slug(t))
STATUS_WORDS_RE = re.compile(r"\b(?:where (?:did|do) we leave off|left off|leave off|status|progress|where were we)\b", re.IGNORECASE)

#: How people ACTUALLY ask (audit 2026-11-02, C4). "How's that project coming
#: along?" matched none of the words above, so the authoritative record was
#: never consulted and the answer came from the model — which said "It's all set
#: up and ready to go... plus the tests are written too" about a build that had
#: just failed and written nothing.
#:
#: These are broader and more ambiguous ("is it done?" could be about dinner), so
#: `_project_prepass` requires the message to reference the project or the work
#: before treating one as a status question. See `asks_project_status`.
STATUS_PHRASES_RE = re.compile(
    r"\b(?:"
    r"coming along|how(?:'s| is|’s) (?:it|that|the|this) .{0,24}?going|"
    r"how(?:'s| is|’s) (?:it|that) coming|how far along|how(?:'s| is) that going|"
    r"any (?:progress|news|update|updates)|what(?:'s| is) the latest|"
    r"(?:is|are) (?:it|that|this|they) (?:done|finished|ready|working)|"
    r"did (?:you|it) (?:finish|work|build|succeed)|"
    r"what (?:did|have) you (?:actually )?(?:verif\w+|check\w+|test\w+|prov\w+|do(?:ne)?)|"
    r"how did (?:it|that) go|"
    r"(?:is|was) (?:it|that) successful"
    r")\b", re.IGNORECASE)


def asks_project_status(text: str, *, references_work: bool) -> bool:
    """Is this a question about where the work stands?

    The narrow words ("status", "progress", "where did we leave off") are
    unambiguous on their own. The natural phrasings are not — "how's it going?"
    is usually small talk — so those need the message to be about the work.
    Answering small talk with a build report would be the same over-claiming
    error in the opposite direction.
    """
    raw = text or ""
    if STATUS_WORDS_RE.search(raw):
        return True
    return bool(references_work and STATUS_PHRASES_RE.search(raw))
RESUME_WORDS_RE = re.compile(r"\b(?:continue|resume|keep (?:working|going)|finish)\b", re.IGNORECASE)
# Inflection-aware: plain \b(?:improve)\b never matched "improvements"/"improving".
IMPROVE_WORDS_RE = re.compile(r"\b(?:improv\w*|enhanc\w*|upgrad\w*|polish\w*|refactor\w*|fix\w*|extend\w*)\b", re.IGNORECASE)
# Sentinel: a build was clearly requested but no name was given, so ask instead
# of scraping a name out of the sentence (which produced junk slugs like
# "what-other-improvements-can-we-make-to-the-flapp").
NEEDS_NAME = "__NOVA_NEEDS_PROJECT_NAME__"
IMPLEMENT_SUGG_RE = re.compile(
    r"\b(?:implement|apply|do|go ahead with|add)\b.{0,40}\b(?:those|these|the|your)\s+(?:improvements|suggestions|ideas|next steps)\b",
    re.IGNORECASE,
)
# Feature-request phrasing used when continuing an already-active project in
# casual conversation ("Yes. Let's set up the leaderboard please.") — these
# verbs alone don't imply a brand new project unless paired with an explicit
# NAME_RE match, so callers should only use this as a fallback trigger when a
# project is already known/active and no new project name was given.
BUILD_ACTION_RE = re.compile(
    r"\b(?:set\s+up|add(?:\s+in)?|put\s+in|wire\s+up|hook\s+up)\b",
    re.IGNORECASE,
)
# "That didn't work / I don't see it / look again" — a follow-up complaint about
# work we JUST did. Strong signal to continue improving the last-active project
# rather than dropping into the general agent (which then fumbles file paths).
CONTINUATION_COMPLAINT_RE = re.compile(
    r"\b(?:look again|try again|still\s+(?:doesn'?t|does not|not|isn'?t|is not|won'?t|broken|the same)|"
    r"still\s+(?:stuck|frozen)|(?:stuck|frozen)\s+on|"
    r"(?:it|this|that)\s+(?:is\s+)?broken|(?:won'?t|wont)\s+start|not\s+starting|"
    r"doesn'?t\s+work|does not work|not working|isn'?t working|didn'?t work|did not work|"
    r"i\s+(?:don'?t|do not|can'?t|cannot)\s+see|not there|isn'?t there|nothing happen(?:s|ed)?|"
    r"(?:it|that|nothing)\s+(?:didn'?t|did not|doesn'?t|does not)\s+(?:work|change|appear|show)|"
    r"you\s+(?:didn'?t|did not|forgot to))\b",
    re.IGNORECASE,
)
# Question detection now lives in core/intent.py (is_question), which is robust
# to preamble and missing punctuation — QUESTION_LEAD_RE's first-word anchor was
# the source of the "I meant what other improvements..." misroute.


# ── Where does a project NAME end and a REQUIREMENT begin? ───────────────────
#
# The previous answer used CAPITALISATION: "Rock and Roll Tracker" looked like a
# title, "Serpent and use Python" looked like a requirement. That is not a valid
# invariant for a voice-first assistant. An STT transcript does not preserve
# intentional title case, and neither does ordinary typing, so
#
#     "create a project called rock and roll tracker"
#
# would have been truncated to "rock" purely because it was spoken rather than
# typed. Changing only capitalisation must never change which words Nova believes
# belong to the name.
#
# So the decision is deterministic SYNTAX, evaluated case-insensitively:
#
#   1. QUOTED         -> the quoted span, exactly. Quoting states the boundary.
#   2. ACTION SUFFIX  -> a connective followed by an imperative VERB is a
#                        requirement, not title text: "and use Python",
#                        "and add levels", "then add a scoreboard".
#   3. CLEAN TITLE    -> no ambiguous continuation at all -> the whole capture.
#   4. AMBIGUOUS      -> ASK. "called Serpent with a dark theme" and a real title
#                        like "Man with a Plan" are the same syntax; nothing short
#                        of quotation separates them. Guessing either way is
#                        wrong, so Nova asks instead.
#
# `using` / `that` / `which` / `who` do NOT truncate on sight — a legitimate title
# may contain them — they make the boundary ambiguous, which routes to (4).
_ACTION_CONNECTIVES = frozenset({"and", "then", "plus", "also"})

#: Imperative verbs that begin a requirement clause. A closed list on purpose: a
#: noun/adjective lexicon would overfit, while "<connective> <verb>" is reliable.
_ACTION_VERBS = frozenset("""
    use uses using add adds adding keep keeps keeping make makes making
    include includes including support supports supporting track tracks tracking
    build builds building create creates creating write writes writing
    store stores storing save saves saving run runs running show shows showing
    display displays handle handles allow allows avoid avoids target targets
    deploy deploys test tests fix fixes remove removes delete deletes
    put puts set sets give gives let lets have has send sends
""".split())

#: Relative pronouns. After an explicit "called X", these open a clause ABOUT
#: the thing rather than more of its name: "called word-counter that counts
#: words in a text file" is a name plus a requirement, and asking "what should
#: I call the project?" there is answering a question nobody asked (audit,
#: H16). Unmarked, they stay as unprovable as anything else — "a project that
#: tracks spending" never said a title started.
#:
#: Kept narrow on purpose. A preposition is genuinely ambiguous ("Man with a
#: Plan", "Python for Beginners" are real titles), so those still ask; a title
#: containing a relative clause is rare, and quoting states it exactly.
_RELATIVISERS = frozenset({"that", "which", "who", "whom", "whose"})

#: Tokens after which a title boundary cannot be proven either way.
#:
#: "as" was missing, which is how "a game called Serpent as a single HTML file"
#: became the project `serpent-as-a-single-html-file` (audit, H16). It asks now.
_AMBIGUOUS_CONTINUATIONS = frozenset({
    "with", "without", "using", "for", "from", "in", "on", "at", "about",
    "to", "by", "into", "over", "under", "when", "while", "if", "unless",
    "until", "so", "because", "as", "like",
}) | _RELATIVISERS

#: Connectives that can introduce a first-person requirement clause. Broader than
#: `_ACTION_CONNECTIVES` because "but I want it simple" and "so I can run it
#: offline" are requirements too.
_CLAUSE_CONNECTIVES = frozenset({"and", "then", "plus", "also", "but", "so",
                                 "or", "while", "although", "though"})

#: A title does not continue into a clause about the speaker or the thing.
_SUBJECT_PRONOUNS = frozenset({"i", "we", "you", "it", "they", "he", "she",
                               "there", "that's", "its", "lets", "let's"})

NAME_AMBIGUOUS = "__NOVA_PROJECT_NAME_AMBIGUOUS__"


def resolve_name_boundary(name: str, *, marked: bool = False) -> str:
    """Where the title ends. Returns the name, or NAME_AMBIGUOUS to ask.

    Case-insensitive by construction: every comparison lowercases first, so the
    same words produce the same answer however they were capitalised.

    `marked` says the words came after an explicit "called X" / "named X",
    which changes what a RELATIVE CLAUSE means. Unmarked, nothing has said a
    title starts here, so "a project that tracks spending" is unprovable and
    Nova asks. Marked, the speaker has named the thing and is now describing
    it, so "called word-counter that counts words in a text file" is a name
    plus a requirement — it used to be answered with "What should I call the
    project?" when the name was right there in the sentence (audit, H16).

    This applies to relative pronouns ONLY. A preposition stays ambiguous
    either way, because "Man with a Plan" and "Python for Beginners" are real
    titles and no word list can tell them from "Serpent with a dark theme".
    """
    words = (name or "").split()
    if len(words) <= 1:
        return (name or "").strip()

    for i, w in enumerate(words[1:], start=1):   # never cut at the first word
        low = w.lower().strip(".,;:!?")
        nxt = words[i + 1].lower().strip(".,;:!?") if i + 1 < len(words) else ""

        # (2a) a connective followed by an imperative verb ends the title.
        if low in _ACTION_CONNECTIVES and nxt in _ACTION_VERBS:
            return " ".join(words[:i]).strip()

        # (2b) a connective followed by a SUBJECT PRONOUN also ends it:
        # "and I want Python", "and it should run offline", "but I want it
        # simple", "and we should add levels". This is the grammar of the
        # original live failure — "…and I want you to…" — and a title does not
        # continue into a first-person clause.
        if low in _CLAUSE_CONNECTIVES and nxt in _SUBJECT_PRONOUNS:
            return " ".join(words[:i]).strip()

        # (4a) a relative clause after an explicitly marked name describes the
        # thing; the title ended at the pronoun.
        if marked and low in _RELATIVISERS:
            return " ".join(words[:i]).strip()

        # (4b) anything else that could open a requirement is unprovable.
        if low in _AMBIGUOUS_CONTINUATIONS:
            return NAME_AMBIGUOUS

    # (3) nothing ambiguous anywhere: the whole capture is the title.
    return (name or "").strip()


#: Re-exported for callers that already imported it from this module.
_WIN_RESERVED = WIN_RESERVED


def slugify(name: str) -> str:
    """Compatibility wrapper. The contract lives in `core/project_names.py`.

    Kept because call sites and tests import it, but it no longer OWNS anything:
    ProjectBuilder and ProjectManager must agree on what a live project directory
    is called, and two independent implementations of that had already drifted.
    """
    return canonical_project_slug(name)


#: pytest's one-line summary: "3 passed in 0.12s", "1 failed, 10 passed in …",
#: "no tests ran in 0.01s".
_PYTEST_COUNT_RE = re.compile(r"\b(\d+)\s+(passed|failed|error|errors|xpassed|xfailed)\b")


def _collected_count(output: str) -> int:
    """How many tests pytest actually ran. 0 means nothing was proven.

    Parsed from the summary rather than trusted from the exit code, because exit
    0 is also what an empty run produces — the whole of H17.
    """
    if "no tests ran" in (output or "").lower():
        return 0
    return sum(int(m.group(1)) for m in _PYTEST_COUNT_RE.finditer(output or ""))


def _module_level_asserts(path: Path) -> int:
    """How many `assert` statements run at import time.

    A file of top-level asserts proves something when it exits 0, even though
    pytest collects no tests from it. Counted structurally rather than by
    grepping for the word, so a string containing "assert" is not evidence.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for node in tree.body if isinstance(node, ast.Assert))


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class ProjectStateError(RuntimeError):
    """The project exists, but its current-project pointer could not be stored.

    Deliberately distinct from `select()` returning None, which means "no such
    project". The caller has to tell "you don't have that" apart from "I could
    not record that", because only one of them is the user's mistake.
    """


class ProjectBuilder:
    def __init__(self, *, projects_dir: Path, llm: Any, llm_semaphore: asyncio.Semaphore, memory: Any,
                 models: Any | None = None, completion: Any | None = None,
                 announcer: Any | None = None) -> None:
        self._projects_dir = Path(projects_dir).resolve()
        self._llm = llm
        self._sem = llm_semaphore
        self._memory = memory
        # ModelRouter (optional). Without it, everything runs on the local model
        # exactly as before. With it, planning uses the `planner` role and file
        # generation uses `coder` — so pointing those at a stronger remote model
        # actually improves project building instead of only affecting deep mode.
        self._models = models
        self._active: dict[str, asyncio.Task] = {}
        # Stage 14: completion is DERIVED by this service from acceptance
        # criteria and evidence. The builder records what it observed and asks;
        # it no longer decides. Constructed here when the caller does not pass
        # one so that every path — including tests that build a bare
        # ProjectBuilder — goes through the same authority.
        from core.completion_events import CompletionAnnouncer
        from core.completion_service import CompletionService
        self._completion = completion or CompletionService(
            memory=memory, projects_dir=self._projects_dir)
        self._announcer = announcer or CompletionAnnouncer(memory=memory)

    @property
    def completion(self):
        """The authoritative completion evaluator for this builder."""
        return self._completion

    # ── Test-first repair (U8) ──────────────────────────────────────────────
    # The flappy-bird failure taught the lesson this implements: the run check
    # proves a program STARTS, never that it WORKS. A window frozen on frame one
    # starts perfectly. So when Marcus reports a bug, don't just regenerate code
    # and claim victory — first write a check that REPRODUCES the bug, prove it
    # fails, then fix until it passes.
    #
    # The critical subtlety: a check that passes BEFORE the fix is a BAD check
    # (it doesn't detect the reported problem). That is reported honestly rather
    # than treated as success, because "the test passed" would otherwise mean
    # nothing at all.

    async def write_repro_check(self, slug: str, path: Path, complaint: str,
                                source_files: list[str]) -> str | None:
        """Ask for a standalone script that FAILS while the reported bug exists.
        Returns the relative path of the check, or None if one couldn't be made."""
        listing = "\n".join(f"- {f}" for f in source_files[:8]) or "(none)"
        excerpt = ""
        for rel in source_files[:2]:
            try:
                excerpt += f"\n--- {rel} ---\n{(path / rel).read_text(encoding='utf-8')[:4000]}\n"
            except Exception:
                continue
        code = await self._llm_file(
            f"Marcus reports this problem with the project `{slug}`:\n\"{complaint}\"\n\n"
            f"Files:\n{listing}\n{excerpt}\n\n"
            "Write a STANDALONE Python check script that FAILS (exits non-zero) while that exact problem "
            "exists, and passes once it is fixed. Requirements:\n"
            "- import the project's module(s) WITHOUT running any GUI main loop\n"
            "- if the code is a GUI/tk app, drive it headlessly: create the object, call the relevant "
            "methods, advance timers/state manually, and ASSERT the behavior Marcus described\n"
            "- never call mainloop(); never wait on real user input; finish in under 10 seconds\n"
            "- print what it observed, then sys.exit(0) on pass or sys.exit(1) on fail\n"
            "Reply with ONLY the file content in a single fenced code block."
        )
        if self._looks_like_failed_generation(code):
            return None
        rel = "nova_check.py"
        (path / rel).write_text(code, encoding="utf-8")
        return rel

    async def run_repro_check(self, path: Path, rel: str, timeout_s: float = 20.0) -> tuple[bool, str]:
        """(passed, output). Passing = exit code 0."""
        import subprocess
        import sys as _sys

        def _run() -> tuple[bool, str]:
            try:
                proc = subprocess.run(
                    [_sys.executable, rel], cwd=str(path), capture_output=True,
                    text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                return False, "check timed out (it hung — treated as a failure)"
            except Exception as e:  # noqa: BLE001
                return False, f"could not run the check: {e}"[:400]
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            return proc.returncode == 0, out[-1500:]

        return await asyncio.to_thread(_run)

    @staticmethod
    def summarize_repair(reproduced: bool | None, passed_after: bool | None) -> str:
        """The honest one-liner. Never claims a fix that wasn't demonstrated."""
        if reproduced is None:
            return ("I couldn't write a check for that, so I changed the code but have NOT verified it "
                    "actually fixes what you described — please run it and tell me.")
        if not reproduced:
            return ("I wrote a check for that, but it PASSED before I changed anything — so it isn't "
                    "capturing the problem you're seeing. I made the change, but treat it as unverified "
                    "and tell me more about what you observe.")
        if passed_after:
            return ("I wrote a check that reproduced the problem (it failed), then fixed until that check "
                    "PASSES. That's verified, not assumed.")
        return ("I wrote a check that reproduced the problem, but I could NOT get it to pass. The bug is "
                "still there — I haven't fixed it.")

    def active_projects(self) -> set[str]:
        """Slugs with a build/improve task still running — used to refuse
        deleting a project out from under an in-flight build."""
        return {slug for slug, task in self._active.items() if task and not task.done()}

    def _handle(self, role: str) -> tuple[Any, asyncio.Semaphore]:
        """(runtime, semaphore) for a role — the local model when unrouted.

        The semaphore travels WITH the model: the local one serializes on the
        GPU, while a cloud handle carries its own concurrency, so a remote
        build no longer blocks local chat."""
        if self._models is not None:
            try:
                handle = self._models.for_role(role)
                return handle.runtime, handle.semaphore
            except Exception as e:  # noqa: BLE001
                # Still builds, on the local model. But a router that RAISES
                # is a bug -- a misconfigured role is already warned about
                # when the router is built -- and a build quietly running on
                # a different model than the one configured is exactly the
                # kind of thing nobody would ever notice.
                logger.warning("builder_model_route_failed", role=role,
                               error=f"{type(e).__name__}: {str(e)[:160]}",
                               fell_back_to="local")
        return self._llm, self._sem

    # ── Path safety ──────────────────────────────────────────────────────────

    def _project_path(self, slug: str) -> Path:
        # `slug` here is an identity Nova already has — from `list_projects()`,
        # `known_slug_in_text()`, `last_active`, or one it just canonicalised. It
        # must resolve to ITSELF. Re-canonicalising it turned a legacy directory
        # `My_Old.Project` into `my-old-project`, so an existence check reported
        # the pointer stale and a status read went looking in the wrong place —
        # and with both directories present it could verify against the sibling.
        # A canonical slug passes through `safe_live_component` unchanged.
        slug = safe_live_component(slug)
        path = (self._projects_dir / slug).resolve()
        if path.parent != self._projects_dir:
            raise ValueError(f"invalid project name: {slug}")
        return path

    def list_projects(self) -> list[str]:
        """Projects, by the ONE definition in `core.project_names`."""
        return list_project_dirs(self._projects_dir)

    #: A slug mention has to be a WHOLE token, not a run of letters inside a
    #: longer word. `"one" in "Is it done?"` is true, and it made a project
    #: called `one` the subject of a question that never mentioned it -- which
    #: then outranked the current project everywhere this resolver is used:
    #: selection, resume, status, mutation and the completion record attached
    #: to an answer. Measured, with projects `one` and `cat` on disk:
    #:
    #:     "Is it done?"             -> one     (d-ONE)
    #:     "what a catastrophe"      -> cat     (CAT-astrophe)
    #:     "the application is slow" -> cat     (appli-CAT-ion)
    #:     "that's a scone"          -> one     (sc-ONE)
    #:
    #: Lookarounds rather than , because slugs contain hyphens and  sits
    #: in the middle of `flappy-bird`.
    @staticmethod
    def _mention_span(needle: str, haystack: str) -> tuple[int, int] | None:
        """WHERE the name appears, or None. The span is what disambiguates.

        Two projects can both legitimately match one message: with `cat` and
        `cat-tracker` on disk, "open the cat tracker" matches `cat` as an exact
        token and `cat-tracker` as a spaced phrase. Tier priority alone picked
        `cat` -- and since a named project outranks the current one everywhere,
        "delete the cat tracker" resolved to `cat`, a destructive command
        aimed at the wrong project.

        Length alone is not the answer either: it would break the case tier
        priority exists for. "flappy-bird is still frozen" must resolve to
        `flappy-bird`, not to a project called `still-frozen` whose spaced form
        also appears -- and `still-frozen` is the longer name.

        The two cases differ in whether the matches OVERLAP. `cat` sits inside
        the span `cat tracker` occupies; `still frozen` sits somewhere else in
        the sentence entirely. So the span decides, and tier priority still
        decides between candidates that do not overlap.
        """
        if not needle:
            return None
        m = re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                      haystack)
        return (m.start(), m.end()) if m else None

    @classmethod
    def _mentions_token(cls, needle: str, haystack: str) -> bool:
        return cls._mention_span(needle, haystack) is not None

    @staticmethod
    def _drop_covered(cands: list[tuple[str, tuple[int, int]]]
                      ) -> list[str]:
        """Remove any name whose mention sits INSIDE another name's mention."""
        keep: list[str] = []
        for slug, (lo, hi) in cands:
            covered = any(
                other != slug and o_lo <= lo and hi <= o_hi
                and (o_hi - o_lo) > (hi - lo)
                for other, (o_lo, o_hi) in cands)
            if not covered:
                keep.append(slug)
        return keep

    #: The compact form of every run of up to four consecutive WORDS in the
    #: text. Compacting the whole string instead ("flappy bird" -> the entire
    #: message with separators stripped) destroys the boundaries the exact and
    #: spaced paths were just taught to respect, and lets a slug match inside a
    #: longer word all over again:
    #:
    #:     "unflappybirds everywhere" -> flappy-bird
    #:     "the notepads are cheap"   -> note-pad
    #:     "repackaged goods"         -> pack-age
    #:
    #: Joining word RUNS keeps both directions working -- "flappy bird" and
    #: "flappybird" both produce "flappybird" -- while "notepads" produces
    #: "notepads" and matches nothing. Four words because the caller only
    #: reaches this path for slugs of at most four parts.
    @staticmethod
    def _compact_windows(text: str, max_words: int = 4) -> set[str]:
        words = re.findall(r"[a-z0-9]+", text)
        out: set[str] = set()
        for i in range(len(words)):
            joined = ""
            for j in range(i, min(i + max_words, len(words))):
                joined += words[j]
                out.add(joined)
        return out

    def known_slug_in_text(self, text: str) -> str | None:
        lowered = (text or "").lower()
        compact = self._compact_windows(lowered)
        exact_slug: list[tuple[str, tuple[int, int]]] = []
        spaced_slug: list[tuple[str, tuple[int, int]]] = []
        compact_only: list[str] = []
        for s in self.list_projects():
            part_count = len([p for p in s.split("-") if p])
            span = self._mention_span(s, lowered)
            if span is not None:
                exact_slug.append((s, span))
                continue
            # Very long slugs (many hyphen-separated words) are often just
            # sentence-like artifacts from earlier routing mistakes. Avoid
            # treating those as a spaced phrase match inside normal prose.
            spaced = (self._mention_span(s.replace("-", " "), lowered)
                      if part_count <= 6 else None)
            if spaced is not None:
                spaced_slug.append((s, spaced))
                continue
            # Slugs are single mashed-together words ("flappybird") but users
            # naturally type them with spaces ("flappy bird") — compare with
            # all separators stripped from both sides too. Guarded by a
            # minimum length so short slugs don't match unrelated text.
            s_compact = s.replace("-", "")
            if part_count <= 4 and len(s_compact) >= 5 and s_compact in compact:
                # `compact` is a SET of word-run compactions, so this is an
                # equality test against whole words, not a substring scan.
                compact_only.append(s)
        # Priority order:
        # 1) Exact hyphenated slug mention typed by the user (strongest signal)
        # 2) Spaced slug phrase mention ("flappy bird")
        # 3) Compact fuzzy match ("flappybird")
        # This prevents complaint text from outranking an explicitly named
        # project, e.g. "flappy-bird is still frozen...".
        # A name that appears INSIDE another name's mention loses to it,
        # whichever tier each matched in: "the cat tracker" is one mention, not
        # two. Candidates that matched somewhere else in the sentence are
        # untouched, so tier priority still settles those.
        surviving = self._drop_covered(exact_slug + spaced_slug)
        exact = [s for s, _ in exact_slug if s in surviving]
        spaced = [s for s, _ in spaced_slug if s in surviving]
        if exact:
            return max(exact, key=len)
        if spaced:
            return max(spaced, key=len)
        # Longest compact match still wins among fuzzy-only candidates.
        return max(compact_only, key=len) if compact_only else None

    def is_building(self, slug: str) -> bool:
        # `slug` is an EXISTING identity (a builder slug, or one resolved from a
        # request), so it must resolve to itself. Canonicalising here would make a
        # legacy project's build invisible to the guard.
        task = self._active.get(safe_live_component(slug))
        return task is not None and not task.done()

    # ── Chat pre-pass detection ──────────────────────────────────────────────

    @staticmethod
    def extract_start_request(text: str) -> tuple[str, str] | None:
        """Return (name, brief) if the message asks to build a NAMED project.

        A name only comes from an explicit signal — "called X" / "named X"
        (NAME_RE) or "…project X" (PROJECT_NAME_RE). If a build is clearly
        requested but no name is given, returns (NEEDS_NAME, brief) so the
        caller asks what to call it rather than inventing a name from the
        sentence.
        """
        t = (text or "").strip()
        if not START_RE.search(t):
            return None

        # QUOTED FIRST, with its own parser. The generic regexes cap the capture
        # around 41 characters and only allow [A-Za-z0-9 _-], so a quoted title
        # was never actually exact: "Rock & Roll Tracker" came back as "Rock",
        # "My.Project_Name" as "My", and a 43-character title as a 41-character
        # PREFIX. A prefix silently accepted as a complete name is the same class
        # of bug as swallowing a requirement.
        #
        # The 48-character DIRECTORY limit belongs to the identity layer, not to
        # an accidental regex bound, so the human title is captured whole and
        # `canonical_project_slug` bounds the slug afterwards.
        # Try each delimiter, and prefer the match that starts EARLIEST — for
        # `'The "Best" Game'` both patterns can match, and the single-quoted one
        # is the real boundary.
        best = None
        for pat in QUOTED_NAME_RES:
            q = pat.search(t)
            if q and (best is None or q.start() < best.start()):
                best = q
        if best:
            quoted_name = best.group("qname").strip()
            if quoted_name:
                return quoted_name, t

        # An opening quote with no matching close is not a name Nova can trust.
        # Falling through to the generic parser here would accept a PREFIX of the
        # intended title, which is exactly what "quoted means exact" forbids.
        if _OPEN_QUOTE_RE.search(t):
            return NEEDS_NAME, t

        m = NAME_RE.search(t)
        if m:
            raw = m.group(1).strip()
            # MARKED: "called X" / "named X" states that X is the title, so a
            # trailing clause is cut off rather than making the whole thing
            # unprovable. "called word-counter that counts words" is a name
            # plus a requirement, not a question to ask back (audit, H16).
            name = resolve_name_boundary(raw, marked=True)
            if name == NAME_AMBIGUOUS or not name:
                return NEEDS_NAME, t
            # An unquoted capture that ran into the regex's own length limit is
            # a PREFIX, not a name — UNLESS the boundary fell inside it, in
            # which case everything the limit cut off was being discarded
            # anyway. Checking the limit first is what made "a script called
            # word-counter that counts words in a text file" ask for a name:
            # the capture stopped at exactly 40 characters, while the title
            # ended at "that", twelve characters in.
            if len(raw) >= _MAX_RAW_NAME and name == raw:
                return NEEDS_NAME, t
            return name, t
        m2 = PROJECT_NAME_RE.search(t)
        if m2 and m2.group("name").strip():
            name = re.sub(r"^(?:a|an|the|new|simple|small|little|basic)\s+", "",
                          m2.group("name").strip(), flags=re.IGNORECASE)
            # An unmarked capture must still look like a title; a marked one
            # ("called X" / "named X") is taken at its word.
            if name and (m2.group("marker") or _looks_like_a_title(name)):
                quoted = bool(re.search(r"[\"']" + re.escape(name) + r"[\"']", t))
                if quoted:
                    return name, t
                resolved = resolve_name_boundary(name,
                                                 marked=bool(m2.group("marker")))
                if (resolved != NAME_AMBIGUOUS and resolved
                        and _looks_like_a_title(resolved)):
                    return resolved, t
                return NEEDS_NAME, t
        return NEEDS_NAME, t

    # ── Memory + PROJECT.md state ────────────────────────────────────────────

    async def _save_fact(self, slug: str, attribute: str, value: str) -> None:
        try:
            await self._memory.add_fact(entity=f"project:{slug}", attribute=attribute, value=value[:400], confidence=0.95)
        except Exception as exc:  # noqa: BLE001
            # Every project fact — status, last_error, last_worked — comes
            # through here. Silence meant a degraded memory lost all of them
            # without a single line to find it by.
            logger.warning("project_fact_write_failed", project=slug,
                           attribute=attribute, error=str(exc)[:160])

    async def _set_last_active(self, slug: str) -> bool:
        """Store the current-project pointer. False if it did not get stored.

        Build and improve treat this as best-effort — the edit itself is the
        point and a lost pointer is a nuisance. `select()` cannot: its ENTIRE
        job is moving the pointer, so a swallowed failure there turns into Nova
        announcing a switch that never happened.
        """
        try:
            await self._memory.add_fact(entity="projects", attribute="last_active", value=slug, confidence=0.95)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("last_active_write_failed", slug=slug)
            return False

    async def select(self, slug: str) -> str | None:
        """Make an EXISTING project current. Changes nothing inside it.

        The pointer was only ever written as a side effect of build/improve, so
        a conversation that merely moved between projects left it empty and
        "what are we working on?" had no authoritative answer. Selecting is its
        own act now.

        Returns the slug it settled on, or None if no such project exists —
        selection must never invent one.
        """
        slug = safe_live_component(slug)
        if slug not in self.list_projects():
            return None
        # READ BACK, do not merely check that the write did not raise. A store
        # that accepts a write and does not persist it is indistinguishable
        # from success at the call site, and the failure being closed here is
        # Nova announcing "we're on X now" when the pointer never moved.
        if not await self._set_last_active(slug) or await self.last_active() != slug:
            raise ProjectStateError(slug)
        return slug

    async def last_active(self) -> str | None:
        """The current project, or None — never one that no longer exists.

        The pointer lives in memory and the project is a directory, and those can
        disagree: a delete whose memory update failed, a manual removal, a crash,
        an older bug. Trusting the pointer made "resume where we left off" resolve
        to a deleted project, so existence is VERIFIED here rather than assumed.

        Self-healing the stale value is best-effort; correctness does not depend on
        it succeeding. The property that matters is that a stale pointer is never
        returned as the current project.
        """
        try:
            fact = await self._memory.get_latest_fact(entity="projects", attribute="last_active")
        except Exception:
            return None
        slug = (fact.value or "").strip() if fact else ""
        if not slug:
            return None
        # `is_dir()` was not enough. A directory with no identity document is
        # not a project anywhere else — select refuses it, status reports "I
        # don't have a project called that", conversation cannot name it — and
        # returning it here made it the CURRENT project regardless. Measured
        # with a seeded `projects/orphan-dir/`.
        try:
            if is_project_dir(self._project_path(slug)):
                return slug
        except Exception:  # noqa: BLE001
            return None
        try:
            await self._memory.add_fact(entity="projects", attribute="last_active",
                                        value="", confidence=0.95)
        except Exception as exc:  # noqa: BLE001
            # The pointer still names a project that is gone, so "it" can
            # resolve to something deleted on the next turn.
            logger.warning("stale_last_active_not_cleared",
                           project=slug, error=str(exc)[:160])
        return None

    def _read_project_md(self, slug: str) -> str:
        path = self._project_path(slug) / "PROJECT.md"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""       # no PROJECT.md yet
        except OSError as e:
            # It exists but cannot be opened. Still "" -- callers treat that
            # as "no project notes" -- but said, because "there are no notes"
            # and "there are notes I could not read" are different facts.
            logger.warning("project_md_unreadable", project=slug,
                           error=f"{type(e).__name__}: {str(e)[:160]}")
            return ""

    def _acceptance_section(self, verdict: Any) -> str:
        """The acceptance contract and its evidence, as PROJECT.md sees it.

        PROJECT.md is a PROJECTION now. It used to be an independent authority
        that could say `complete` on its own, two lines below a log entry
        contradicting it. Everything here is read from the derived verdict, so
        the file cannot disagree with the evaluator — if it is wrong, they are
        both wrong, which is a different and more findable kind of wrong.
        """
        if verdict is None:
            return ""
        lines: list[str] = []
        seal = {"human": "confirmed by a person",
                "auto": "sealed automatically from the request",
                "": "NOT SEALED — not established that these are all of it"}
        lines.append(f"State: **{verdict.state}** "
                     f"(requirement revision {verdict.revision})")
        lines.append(f"Contract: {seal.get(verdict.seal_mode, verdict.seal_mode)}")
        if verdict.reasons:
            lines.append(f"Why: {verdict.reasons[0]}")
        lines.append("")
        for st in verdict.criteria:
            mark = {"passed": "[x]", "failed": "[!]", "waived": "[x]"}.get(
                st.verdict, "[ ]")
            note = ""
            if st.verdict == "failed" and st.evidence:
                note = f" — {str(st.evidence.error or st.evidence.detail)[:120]}"
            elif st.verdict == "waived":
                note = " — accepted by a person"
            elif st.stale_reason:
                note = f" — {st.stale_reason[:120]}"
            elif st.verdict == "human_pending":
                note = " — waiting on you"
            opt = "" if st.criterion.required else " (optional)"
            lines.append(f"- {mark} {st.criterion.text}{opt}{note}")
            lines.append(f"      from: \"{st.criterion.origin_quote}\"")
        if verdict.legacy_note:
            lines.append("")
            lines.append(f"> Historical: {verdict.legacy_note}")
        return "\n".join(lines)

    def _write_project_md(
        self,
        slug: str,
        *,
        brief: str,
        status: str,
        verdict: Any = None,
        summary: str = "",
        files: list[dict[str, str]] | None = None,
        run: str = "",
        log_lines: list[str] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        path = self._project_path(slug)
        path.mkdir(parents=True, exist_ok=True)

        existing_log: list[str] = []
        old = self._read_project_md(slug)
        if old:
            m = re.search(r"## Progress log\n(.*?)(?:\n## |\Z)", old, re.DOTALL)
            if m:
                existing_log = [ln for ln in m.group(1).strip().splitlines() if ln.strip()]

        all_log = existing_log + [f"- {_now_str()} — {ln}" for ln in (log_lines or [])]
        files_md = "\n".join(f"- `{f['path']}` — {f.get('purpose', '')}" for f in (files or [])) or "(none yet)"
        sugg_md = "\n".join(f"- [ ] {s}" for s in (suggestions or [])) or "(none yet)"

        acceptance = self._acceptance_section(verdict)
        # The status line is the DERIVED state when there is one. `status` is
        # only used before a contract exists (planning, or an error).
        shown = getattr(verdict, "state", None) or status
        content = (
            f"# {slug}\n\n"
            f"## Brief\n{brief.strip()}\n\n"
            f"## Status\n{shown}\n\n"
            + (f"## Acceptance\n{acceptance}\n\n" if acceptance else "")
            + f"## Summary\n{summary.strip() or '(pending)'}\n\n"
            f"## Files\n{files_md}\n\n"
            f"## How to run\n{run.strip() or '(pending)'}\n\n"
            f"## Progress log\n" + "\n".join(all_log) + "\n\n"
            f"## Next steps / suggestions\n{sugg_md}\n"
        )
        (path / "PROJECT.md").write_text(content, encoding="utf-8")

    # ── LLM helpers ──────────────────────────────────────────────────────────

    async def _llm_json(self, prompt: str, max_tokens: int = 900) -> dict[str, Any] | None:
        """Ask the planner for one JSON object.

        Planning benefits from native reasoning, but reasoning can also eat the
        entire budget: with thinking on, a contradictory prompt produced 25k
        characters of deliberation and hit finish=length on every attempt, even
        at 5,000 tokens, and the real errors.json carries seven "generation came
        back empty" entries (audit, H15).

        So the attempts ESCALATE instead of repeating:
          1. thinking on, the caller's budget.
          2. thinking on, double the budget, for a think that was merely long.
          3. thinking OFF. A model that cannot stop deliberating cannot be
             out-budgeted, and the JSON shape here does not need a think.
        Each attempt is logged with what it got back, because "it returned
        nothing" three times with no record of why is how H15 stayed invisible.
        """
        runtime, sem = self._handle("planner")
        attempts = ((max_tokens, True), (max_tokens * 2, True), (max_tokens, False))
        for n, (budget, thinking) in enumerate(attempts, start=1):
            async with sem:
                raw = await runtime.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=budget,
                    temperature=0.15,
                    thinking=thinking,
                )
            obj = extract_first_json_object(raw or "")
            if obj:
                if n > 1:
                    logger.info("planner_json_recovered", attempt=n,
                                budget=budget, thinking=thinking)
                return obj
            logger.warning("planner_json_failed", attempt=n, budget=budget,
                           thinking=thinking, chars=len(raw or ""),
                           tail=(raw or "")[-160:])
        return None

    async def _llm_file(self, prompt: str) -> str:
        # Writing/rewriting an actual source file — the `coder` role.
        runtime, sem = self._handle("coder")
        kwargs: dict[str, Any] = {}
        try:
            # NO REPETITION PENALTY FOR CODE (audit, L12). The 1.15 that makes
            # prose read well was being applied to every generated source
            # file, and code repeats by design — `self.`, `return`, `if not`,
            # the same indentation on every line of a block. Penalising that
            # pushes the model off the correct token.
            from core.llm_runtime import CODE_REPEAT_PENALTY

            kwargs["repeat_penalty"] = CODE_REPEAT_PENALTY
        except ImportError:
            pass        # a runtime without the parameter keeps its default
        async with sem:
            raw = await runtime.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=_FILE_TOKENS,
                temperature=0.2,
                stop=[],
                thinking=True,
                **kwargs,
            )
        raw = (raw or "").strip()
        # Take the LARGEST fenced block, not the first: models often quote the
        # buggy snippet from the prompt before giving the full corrected file,
        # and grabbing the first fence would write the bug right back.
        blocks = _CODE_FENCE_RE.findall(raw)
        if blocks:
            return max(blocks, key=len).strip() + "\n"
        # No fence: strip leading prose line if it doesn't look like code
        return raw + "\n"

    # ── Build pipeline ───────────────────────────────────────────────────────

    async def start(self, *, name: str, brief: str, requested_by: UUID | None = None) -> dict[str, Any]:
        slug = slugify(name)
        if self.is_building(slug):
            return {"project": slug, "started": False, "reason": "already building"}

        path = self._project_path(slug)
        path.mkdir(parents=True, exist_ok=True)

        self._write_project_md(slug, brief=brief, status="building", log_lines=["Project started."])
        await self._save_fact(slug, "brief", brief)
        await self._save_fact(slug, "status", "building")
        await self._set_last_active(slug)

        BUS.publish("project.started", {"project": slug, "brief": clip(brief, 200)})
        task = asyncio.create_task(self._build(slug, brief))
        self._active[slug] = task
        return {"project": slug, "path": str(path), "started": True}

    async def _build(self, slug: str, brief: str) -> None:
        path = self._project_path(slug)
        try:
            # 1) Plan
            # STAGE 14: the request and its acceptance contract come first,
            # before a single file exists. Deriving criteria afterwards from
            # the generated code would ask the artifact to certify itself.
            await self._establish_contract(slug, brief)

            BUS.publish("project.progress", {"project": slug, "stage": "planning"})
            plan = await self._llm_json(
                "You are Nova, an expert software engineer. Plan a small, complete, WORKING project.\n"
                f"Request: {brief}\n\n"
                'Reply ONLY with JSON in this exact shape:\n'
                '{"summary": "one sentence", "language": "python|html", '
                '"files": [{"path": "main.py", "purpose": "..."}], "run": "how to run it"}\n'
                f"Rules: at most {_MAX_FILES} files; prefer ONE main file; relative paths only; "
                f"{_python_stack_note()} — or a single self-contained html file with inline js/css. "
                "No placeholder files, no README (PROJECT.md exists).",
                max_tokens=1400,
            )
            if not plan or not isinstance(plan.get("files"), list) or not plan["files"]:
                raise RuntimeError("planning failed: model did not return a valid file plan")

            # Tolerate loose plan shapes: entries may be bare strings
            # ("main.py") instead of {"path": ..., "purpose": ...} dicts.
            files: list[dict[str, str]] = []
            for f in plan["files"][:_MAX_FILES]:
                if isinstance(f, str):
                    f = {"path": f, "purpose": ""}
                if not isinstance(f, dict):
                    continue
                rel = str(f.get("path") or "").strip()
                if rel:
                    files.append({"path": rel, "purpose": str(f.get("purpose") or "").strip()})
            if not files:
                raise RuntimeError("planning failed: no usable file paths in plan")
            summary = str(plan.get("summary") or brief).strip()
            run = str(plan.get("run") or "").strip()

            self._write_project_md(
                slug, brief=brief, status="building", summary=summary, files=files, run=run,
                log_lines=[f"Planned {len(files)} file(s): " + ", ".join(f["path"] for f in files)],
            )

            # 2) Generate each file — CONCURRENTLY (U6).
            #
            # Generation is pure (prompt -> text) and the files don't depend on
            # each other, so they fan out. Concurrency is bounded by the MODEL's
            # own semaphore, which means this needs no tuning and no branching:
            # routed to cloud it runs NOVA_CLOUD_CONCURRENCY at a time; routed
            # locally the same code re-serializes on the 1-permit GPU semaphore,
            # exactly as before. Writing stays sequential and in plan order so
            # the on-disk result is deterministic.
            written: list[str] = []
            planned: list[tuple[str, Path, str]] = []
            for spec in files:
                rel = spec["path"].replace("\\", "/").lstrip("/")
                if ".." in rel.split("/"):
                    continue
                target = (path / rel).resolve()
                if not str(target).startswith(str(path)):
                    continue
                planned.append((rel, target, (
                    f"Write the COMPLETE contents of `{rel}` for this project.\n"
                    f"Project request: {brief}\n"
                    f"Plan summary: {summary}\n"
                    f"All files in project: {', '.join(f['path'] for f in files)}\n"
                    f"This file's purpose: {spec['purpose']}\n\n"
                    "Rules: fully working code, no placeholders, no TODO stubs, no explanations. "
                    "Where practical, put core rules (collision, scoring, physics, state transitions, "
                    "calculations) in small functions that run without a live window or network, so they "
                    "can be unit-tested. Keep the entry point under `if __name__ == \"__main__\":`. "
                    "Reply with ONLY the file content inside a single fenced code block."
                )))

            async def _generate(rel: str, prompt: str) -> str:
                BUS.publish("project.progress", {"project": slug, "stage": "writing", "file": rel})
                content = await self._llm_file(prompt)
                if self._looks_like_failed_generation(content):
                    # Retry once — the first attempt likely got truncated mid-reasoning.
                    content = await self._llm_file(prompt)
                if self._looks_like_failed_generation(content):
                    raise RuntimeError(f"generation for {rel} came back empty/incomplete")
                BUS.publish("project.progress", {"project": slug, "stage": "wrote", "file": rel})
                return content

            contents = await asyncio.gather(*(_generate(rel, prompt) for rel, _, prompt in planned))

            for (rel, target, _), content in zip(planned, contents):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(rel)

                # 3) Validate + one repair round for Python files
                if rel.endswith(".py"):
                    error = await asyncio.to_thread(self._py_compile_error, target)
                    if error:
                        BUS.publish("project.progress", {"project": slug, "stage": "repairing", "file": rel})
                        fixed = await self._llm_file(
                            f"This Python file has a syntax error. Fix it and return the COMPLETE corrected file.\n"
                            f"Error: {error}\n\nFile `{rel}`:\n```python\n{content[:6000]}\n```\n"
                            "Reply with ONLY the corrected file content in a single fenced code block."
                        )
                        target.write_text(fixed, encoding="utf-8")
                        error2 = await asyncio.to_thread(self._py_compile_error, target)
                        if error2:
                            raise RuntimeError(f"{rel} failed to compile after repair: {error2}")

            if not written:
                raise RuntimeError("no files were written")

            # 4) Run verification: execute the entry point and debug crashes.
            run_note = await self._verify_and_fix_runtime(slug, path, written)

            # 4b) Logic verification: generate + run headless tests of the core
            #     logic and fix the CODE on assertion failures. Catches bugs a
            #     launch check can't (inverted collision, wrong scoring, etc.).
            test_note = await self._generate_and_run_tests(slug, path, written, summary)

            # 4c) HTML projects. Both steps above return None without a .py
            #     file, and they returned it SILENTLY — so a 359-line browser
            #     game was reported as a "Complete playable snake game... Use
            #     arrow keys or WASD" with nothing having executed it, and with
            #     neither arrow keys nor WASD working (audit, H19).
            html_note = await self._smoke_test_html(slug, path, written)

            # 5) Suggestions
            BUS.publish("project.progress", {"project": slug, "stage": "reviewing"})
            sugg_obj = await self._llm_json(
                f"A project was just built: {summary}\nFiles: {', '.join(written)}\n"
                'Suggest exactly 3 concrete, small improvements. Reply ONLY with JSON: {"suggestions": ["...", "...", "..."]}',
                max_tokens=300,
            )
            suggestions = []
            for s in (sugg_obj or {}).get("suggestions", []):
                # Models sometimes return objects like {"improvement": "...", "rationale": "..."}
                if isinstance(s, dict):
                    s = s.get("improvement") or s.get("suggestion") or s.get("text") or ""
                s = str(s).strip()
                if s:
                    suggestions.append(s)
            suggestions = suggestions[:3]

            # "Build complete." used to be written here, before any validation
            # had happened, and stayed in the log of projects that crashed on
            # every run. What is true at this point is that files were written.
            build_log = [f"Wrote {len(written)} file(s)."]
            if run_note:
                build_log.append(run_note)
            if test_note:
                build_log.append(test_note)
            if html_note:
                build_log.append(html_note)

            # Prove the criteria, one at a time. `run_note` and `test_note` are
            # DIAGNOSTIC: a program that starts proves that a program starts,
            # and a batch of generated tests proves whatever it happened to
            # test. Neither is credit against a named requirement.
            await self._validate_criteria(slug, brief, written, run_note=run_note)

            verdict = await self._completion.evaluate(slug=slug)
            build_log.extend(self._evidence_log(verdict))
            self._write_project_md(
                slug, brief=brief, status=verdict.state, verdict=verdict,
                summary=summary, files=files, run=run, log_lines=build_log,
                suggestions=suggestions,
            )
            await self._save_fact(slug, "status", verdict.state)
            await self._save_fact(slug, "summary", summary)
            if suggestions:
                await self._save_fact(slug, "next_steps", "; ".join(suggestions))
            await self._save_fact(slug, "last_worked", _now_str())

            await self._announcer.announce(
                slug=slug, verdict=verdict, reason="build finished",
                extra={"summary": clip(summary, 200), "files": written,
                       "run": clip(run, 120), "suggestions": suggestions,
                       "run_note": clip(run_note or "", 200),
                       "test_note": clip(test_note or "", 200),
                       "html_note": clip(html_note or "", 200),
                       "contract": verdict.seal_mode, "mode": "build"})
            logger.info("project_build_finished", project=slug,
                        files=len(written), state=verdict.state)
        except Exception as e:  # noqa: BLE001
            logger.warning("project_build_failed", project=slug, error=str(e)[:300])
            try:
                verdict = await self._completion.evaluate(slug=slug)
                self._write_project_md(slug, brief=brief, status=verdict.state,
                                       verdict=verdict,
                                       log_lines=[f"Build failed: {e}"])
                await self._save_fact(slug, "status", verdict.state)
                await self._save_fact(slug, "last_error", str(e)[:200])
                await self._announcer.announce(slug=slug, verdict=verdict,
                                         reason=f"build failed: {str(e)[:120]}",
                                         extra={"mode": "build"})
            except Exception as exc:  # noqa: BLE001
                # NB: bound as `exc`, not `e` — `e` is the build failure and is
                # still read on the next line. Binding `e` here would delete it.
                logger.warning("build_failure_report_failed", project=slug,
                               error=str(exc)[:160])
            BUS.publish("project.error", {"project": slug, "error": clip(e, 240)})
        finally:
            self._active.pop(slug, None)

    async def _contract_basis(self, slug: str) -> tuple[int | None, str]:
        """(revision to carry from, its request text).

        Normally the current revision. But a revision can exist with NO
        criteria — `record_request` creates one before `carry_forward` runs,
        so a failed carry left one behind — and carrying forward from THAT
        silently drops the whole contract, which is how the second improve of
        any contracted project "succeeded" with zero criteria (audit, H20).
        So an empty current revision is stepped over, back to the last one that
        actually holds a contract.
        """
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            return None, ""
        current = int(req["revision"])
        for rev in range(current, 0, -1):
            rows = await self._memory.list_acceptance_criteria(
                project_name=slug, revision=rev)
            if rows:
                if rev != current:
                    logger.warning("contract_basis_stepped_back", project=slug,
                                   current=current, basis=rev,
                                   reason="the current revision holds no criteria")
                text = await self._request_text_at(slug, rev)
                return rev, text
        return current, str(req.get("request_text") or "")

    async def _request_text_at(self, slug: str, revision: int) -> str:
        """The words recorded for ONE revision.

        Not the current one's: carrying a contract forward from an earlier
        revision has to combine THAT revision's words, or the new requirement
        text would be built from a request nobody is carrying.
        """
        row = await self._memory.requirement_at(project_name=slug,
                                                revision=int(revision))
        if row is not None:
            return str(row.get("request_text") or "")
        req = await self._memory.current_requirement(project_name=slug)
        return str((req or {}).get("request_text") or "")

    @staticmethod
    def combined_request(previous: str, instruction: str) -> str:
        """The words the NEW revision is made of: what still stands, plus this.

        An improvement adds to what was asked; the original request is still
        in force. Recording the instruction ALONE is what broke H20: the
        planner decided which criteria carried by checking them against the
        previous request plus the instruction, while `carry_forward` then
        checked them against the instruction alone — so every criterion from
        the original request failed with "not a span of the request at
        revision 2", and the first improve of any contracted project failed.

        One definition, used by both, so they cannot disagree again.
        """
        prev = (previous or "").strip()
        instr = (instruction or "").strip()
        if not prev:
            return instr
        if not instr:
            return prev
        return f"{prev} {instr}"

    async def _plan_contract_change(self, slug: str, request: str,
                                    prev_rev: int | None
                                    ) -> tuple[dict[str, str], list[str]]:
        """Decide which existing criteria survive the new request.

        Returns (reanchor, drop). A criterion whose quote still appears in the
        new request carries unchanged. One whose wording moved needs a new span
        to point at, and one the user has removed must be retired EXPLICITLY —
        a criterion may never leave the contract by being forgotten.
        """
        if prev_rev is None:
            return {}, []
        from core.completion_contract import is_span_of

        old = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=prev_rev)
        if not old:
            return {}, []
        # Checked against the SAME text `_establish_contract` is about to
        # record for the new revision — see `combined_request`.
        combined = self.combined_request(
            await self._request_text_at(slug, prev_rev), request)
        reanchor: dict[str, str] = {}
        drop: list[str] = []
        for c in old:
            if is_span_of(c["origin_quote"], combined):
                continue
            # The wording moved. Ask what it corresponds to now rather than
            # guessing, and retire it if nothing does.
            drop.append(c["criterion_id"])
        return reanchor, drop

    async def _establish_contract(self, slug: str, request: str, *,
                                  previous_revision: int | None = None,
                                  reanchor: dict[str, str] | None = None,
                                  drop: list[str] | None = None) -> Any:
        """Record the request and its acceptance criteria, before any code.

        Returns the requirement revision. Sealing is attempted and may fail:
        a contract that does not cover the request is LEFT UNSEALED rather than
        forced, because an unsealed contract cannot reach COMPLETE and that is
        the honest outcome when Nova could not account for everything asked.
        """
        from core.project_acceptance import coverage_gaps, derive_criteria

        # THE RECORDED TEXT. For a revision that follows another, it is the
        # previous request plus this instruction — the same string
        # `_plan_contract_change` judged the carried criteria against. Record
        # the instruction alone and every criterion from the original request
        # stops being a span of its own requirement (audit, H20).
        recorded = request
        if previous_revision is not None:
            recorded = self.combined_request(
                await self._request_text_at(slug, previous_revision), request)

        rev = await self._completion.record_request(slug=slug,
                                                    request_text=recorded)
        if previous_revision is not None:
            try:
                await self._completion.carry_forward(
                    slug=slug, from_revision=previous_revision, to_revision=rev,
                    drop_criterion_ids=drop or [], reanchor=reanchor or {},
                    drop_reason="superseded by a later request")
            except ValueError:
                # A criterion could not be carried and was not dropped on
                # purpose. The revision now exists with no contract, and
                # letting that stand is how the contract disappeared on the
                # NEXT improve. Say so loudly and refuse — `_contract_basis`
                # will step back over this revision if anyone tries again.
                logger.warning("contract_carry_forward_failed", project=slug,
                               from_revision=previous_revision, to_revision=rev,
                               dropped=len(drop or []))
                BUS.publish("project.error",
                            {"project": slug, "mode": "contract",
                             "error": "a requirement could not be carried "
                                      "forward, so nothing was changed"})
                raise

        BUS.publish("project.progress",
                    {"project": slug, "stage": "acceptance_criteria"})
        criteria = await derive_criteria(request=request, ask_json=self._llm_json)
        if criteria:
            try:
                await self._completion.set_criteria(slug=slug, revision=rev,
                                                    criteria=criteria)
            except ValueError as e:  # a quote that did not survive validation
                logger.info("acceptance_criteria_rejected", project=slug,
                            error=str(e)[:200])

        existing = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=rev)
        # Coverage is judged against the RECORDED words, which for a follow-up
        # revision include the original request. The carried criteria cover
        # that half and the new ones cover the instruction, so a contract seals
        # only when the whole of what is in force is accounted for.
        gaps = coverage_gaps(recorded, [{"origin_quote": c["origin_quote"]}
                                        for c in existing])
        if existing and not gaps:
            await self._completion.seal_contract(slug=slug, revision=rev,
                                                 seal_mode="auto")
        else:
            logger.info("acceptance_contract_unsealed", project=slug,
                        revision=rev, gaps=len(gaps))
            BUS.publish("project.progress",
                        {"project": slug, "stage": "acceptance_incomplete",
                         "uncovered": gaps[:4]})
        return rev

    def _evidence_log(self, verdict: Any) -> list[str]:
        """Log lines describing what was and was not demonstrated."""
        lines = [f"Completion state: {verdict.state} "
                 f"(revision {verdict.revision})"]
        for st in verdict.failing:
            lines.append(f"FAILING — {st.criterion.text}")
        for st in verdict.outstanding:
            why = st.stale_reason or "not demonstrated"
            lines.append(f"outstanding — {st.criterion.text} ({why[:100]})")
        return lines

    async def _validate_criteria(self, slug: str, request: str,
                                 written: list[str], *,
                                 run_note: str | None = None) -> None:
        """Decide each machine criterion SEPARATELY, against what it examined.

        Nothing here maps one global check onto every criterion. A criterion
        for which no check can be written stays unproven and records why.
        """
        from core.project_acceptance import check_criterion

        path = self._project_path(slug)
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            return
        rows = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=int(req["revision"]))
        machine = [c for c in rows if c["verify_kind"] != "human"]
        if not machine:
            return

        runnable = [f for f in written if f.endswith(".py")]
        entry = next((f for f in runnable if "main" in Path(f).name.lower()), None)
        entry = entry or (runnable[0] if runnable else None)
        listing = ", ".join(written)
        code = ""
        if entry:
            try:
                code = (path / entry).read_text(encoding="utf-8", errors="replace")
            except OSError:
                code = ""

        def _declare(paths: list[str]) -> None:
            from core.completion_artifacts import declare_scaffold
            declare_scaffold(path, paths)

        for c in machine:
            # The context is captured BEFORE the check runs, so the verdict is
            # attributed to the code the check actually examined.
            ctx = await self._completion.begin_check(
                slug=slug, criterion_id=c["criterion_id"])
            if entry is None:
                verdict, detail = ("inconclusive",
                                   "there is no runnable entry point to check "
                                   "this against")
            else:
                BUS.publish("project.progress",
                            {"project": slug, "stage": "checking_criterion",
                             "criterion": clip(c["text"], 120)})
                verdict, detail = await check_criterion(
                    path=path, entry=entry, module=Path(entry).stem,
                    listing=listing, code=code, criterion=c, request=request,
                    ask_file=self._llm_file, declare_scaffold=_declare)
            await self._completion.record_verdict(context=ctx, verdict=verdict,
                                                  detail=detail[:400])
            self._announcer.criterion_result(
                slug=slug, criterion_id=c["criterion_id"],
                criterion_text=c["text"], verdict=verdict,
                revision=int(req["revision"]), detail=detail)

    async def _verify_and_fix_runtime(self, slug: str, path: Path, candidates: list[str]) -> str | None:
        """Execute the project's entry point and self-debug crashes (3 tries).

        Timeout expiring means the program started and stayed alive (games and
        UIs run indefinitely) — that counts as success. Returns a note for the
        build log, or None when run verification is disabled or there is
        nothing runnable.
        """
        if os.getenv("NOVA_PROJECT_RUN_CHECK", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        # Never treat a generated test file as the app's entry point.
        runnable = [
            f for f in candidates
            if f.endswith(".py") and not Path(f).name.startswith("test_") and Path(f).name != "tests.py"
        ]
        entry = next((f for f in runnable if "main" in Path(f).name.lower()), None)
        entry = entry or (runnable[0] if runnable else None)
        if entry is None:
            return None

        for attempt in range(3):
            BUS.publish("project.progress", {"project": slug, "stage": "run_check", "file": entry, "attempt": attempt + 1})
            run_error = await asyncio.to_thread(self._run_check, path, entry)
            if run_error is None:
                return f"Run check passed ({entry})."
            # A missing package is an environment problem, not a code bug —
            # report it honestly instead of letting the fix loop mutilate the
            # project trying to code around it.
            missing = _MISSING_MODULE_RE.search(run_error)
            if missing:
                return f"Run check blocked: package '{missing.group(1)}' is not installed on this machine."
            if attempt == 2:
                return f"Run check still failing after fixes: {run_error[:180]}"
            BUS.publish("project.progress", {"project": slug, "stage": "fixing_runtime_error", "file": entry})
            # Minimal-patch mode first: a small model applies a targeted
            # find/replace far more reliably than it regenerates a whole file
            # around one bug (whole-file mode tends to copy the bug through).
            if await self._fix_runtime_error_patch(path, entry, run_error):
                continue
            target = path / entry
            current = target.read_text(encoding="utf-8", errors="replace")
            fixed = await self._llm_file(
                f"This Python program crashes when run. Fix the bug and return the COMPLETE corrected file.\n"
                f"Error output:\n{run_error[:1200]}\n\nFile `{entry}`:\n```python\n{current[:7000]}\n```\n"
                "Rules: keep all existing behavior, fix only the crash, and do not add any imports "
                "the file does not already have. Do NOT quote or restate the buggy code. "
                "Reply with EXACTLY ONE fenced code block containing the entire corrected file."
            )
            if self._looks_like_failed_generation(fixed, current):
                return f"Run check failed; fix generation came back empty: {run_error[:160]}"
            if fixed.strip() == current.strip():
                # The model returned the file unchanged — retrying the same
                # prompt is the only option left, so let the loop continue.
                BUS.publish("project.progress", {"project": slug, "stage": "fix_unchanged", "file": entry, "attempt": attempt + 1})
                continue
            target.write_text(fixed, encoding="utf-8")
            if await asyncio.to_thread(self._py_compile_error, target):
                target.write_text(current, encoding="utf-8")  # revert a broken "fix"
                return f"Run check failed; proposed fix did not compile: {run_error[:160]}"
        return None

    async def _fix_runtime_error_patch(self, path: Path, entry: str, run_error: str) -> bool:
        """Ask the LLM for a minimal find/replace patch and apply it.

        Returns True when a compiling change was applied. Verified: Qwen3.5-9B
        reliably produces the correct one-line patch here even for bugs it
        fails to fix in whole-file regeneration mode.
        """
        target = path / entry
        current = target.read_text(encoding="utf-8", errors="replace")
        lines = current.splitlines()
        frames = [int(n) for n in re.findall(r"line (\d+)", run_error)]
        ln = frames[-1] if frames else 0
        src = lines[ln - 1].strip() if 1 <= ln <= len(lines) else ""
        if ln:
            lo, hi = max(0, ln - 25), min(len(lines), ln + 10)
            window = "\n".join(lines[lo:hi])
        else:
            window = current[:2400]
        err_last = run_error.strip().splitlines()[-1] if run_error.strip() else ""

        obj = await self._llm_json(
            "A Python program crashed. Produce the SMALLEST fix as a find/replace patch.\n"
            f"Traceback:\n{run_error[:900]}\n\n"
            + (f"The crash is at line {ln}: `{src}`\n" if src else "")
            + f"The error is: {err_last}\n\n"
            f"Code around the crash:\n```python\n{window}\n```\n\n"
            'Reply ONLY with JSON: {"find": "exact text copied verbatim from the code", "replace": "corrected text"}\n'
            "Rules: the find text must appear in the code EXACTLY as written; keep the patch as small as "
            "possible (usually one line); the replace text must make the error impossible.",
            max_tokens=700,
        )
        find = str((obj or {}).get("find") or "")
        replace = str((obj or {}).get("replace") or "")
        if not find or find == replace or find not in current:
            return False

        # Apply on the crash line only when possible. A global str.replace is
        # unsafe here: `find` is often a substring of a correct line elsewhere
        # (e.g. patching `root.after(...)` would also mangle `self.root.after(...)`).
        trailing = "\n" if current.endswith("\n") else ""
        if ln and 1 <= ln <= len(lines) and find in lines[ln - 1]:
            new_lines = list(lines)
            new_lines[ln - 1] = new_lines[ln - 1].replace(find, replace)
            patched = "\n".join(new_lines) + trailing
        elif current.count(find) == 1:
            patched = current.replace(find, replace)
        else:
            return False  # ambiguous match — let whole-file regen handle it

        try:
            compile(patched, entry, "exec")
        except Exception:
            return False  # a patch that breaks the syntax is worse than none
        target.write_text(patched, encoding="utf-8")
        return True

    async def _smoke_test_html(self, slug: str, path: Path,
                               written: list[str]) -> str | None:
        """Run an HTML project headlessly, and fix it once if it is broken.

        AUDIT H19. Neither the run check nor the logic tests look at a project
        with no `.py` file, and they said nothing about it — so `serpent` was
        announced as a "Complete playable snake game" having never been
        executed, with a reversal guard that rejected every turn and WASD
        controls that did not exist.

        Returns the note for the build log, which is honest about what was
        established: that the page RUNS is not that the game is right.
        """
        if os.getenv("NOVA_PROJECT_HTML_CHECK", "1").strip().lower() in {
                "0", "false", "no", "off"}:
            return None
        pages = [f for f in written if f.lower().endswith((".html", ".htm"))]
        if not pages:
            return None
        from core.project_html_check import smoke_test_html

        # The entry page: index.html if there is one, else the first.
        page = next((f for f in pages if Path(f).name.lower().startswith("index")),
                    pages[0])
        target = path / page

        BUS.publish("project.progress",
                    {"project": slug, "stage": "html_smoke", "file": page})
        result = await smoke_test_html(target)
        if result.passed or not result.ran:
            # Passing, or nothing ran and the note says so.
            return f"{result.note()} ({page})"

        logger.info("html_smoke_failed", project=slug, page=page,
                    errors=result.errors[:2])

        # It ran and it is broken. ONE repair round, with the real error.
        BUS.publish("project.progress",
                    {"project": slug, "stage": "repairing", "file": page})
        before = target.read_text(encoding="utf-8", errors="replace")
        fixed = await self._llm_file(
            "This single-page HTML program fails when run headlessly.\n"
            "The errors, from a stub DOM that dispatches arrow keys and ticks "
            "requestAnimationFrame:\n"
            + "\n".join(f"- {e}" for e in (result.errors
                                           or ["it never drew anything"])[:4])
            + f"\n\nFix the CAUSE and return the COMPLETE corrected file "
              f"`{page}`.\nKeep every working feature. Do not add a build step, "
              "a framework, or anything loaded from the network.\n\n"
              f"```html\n{before[:9000]}\n```\n"
              "Reply with ONLY the corrected file in one fenced code block.")
        if self._looks_like_failed_generation(fixed):
            return (f"{result.note()} ({page}) The repair attempt came back "
                    "empty, so the file is unchanged.")

        target.write_text(fixed, encoding="utf-8")
        after = await smoke_test_html(target)
        if after.passed:
            return (f"{after.note()} ({page}) It failed the first time and was "
                    f"repaired: {result.errors[:1] or ['it never drew anything']}")
        # The fix did not fix it. Leave the project as it STARTED rather than
        # as a second broken version — the same invariant
        # `_generate_and_run_tests` keeps for Python.
        target.write_text(before, encoding="utf-8")
        return (f"{result.note()} ({page}) A repair was attempted and did not "
                "help, so the file was restored unchanged.")

    async def _generate_and_run_tests(self, slug: str, path: Path, written: list[str], summary: str) -> str | None:
        """Generate headless logic tests for the built code, run them, and fix
        the CODE (not the test) on assertion failures.

        This covers what a run check can't: a program that launches cleanly can
        still be logically wrong (inverted collision math, wrong scoring). A
        generated test asserts concrete expected outputs of the core logic and
        catches those bugs.

        Safety invariant: the entry file ends either improved so the tests pass
        (and it still launches), or byte-for-byte as it started — a wrong test
        can never leave the shipped code worse than it was.
        """
        if os.getenv("NOVA_PROJECT_LOGIC_TESTS", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        runnable = [
            f for f in written
            if f.endswith(".py") and not Path(f).name.startswith("test_") and Path(f).name != "tests.py"
        ]
        entry = next((f for f in runnable if "main" in Path(f).name.lower()), None)
        entry = entry or (runnable[0] if runnable else None)
        if entry is None:
            return None  # nothing importable to test (e.g. a pure HTML project)

        module = Path(entry).stem
        entry_path = path / entry
        original_main = entry_path.read_text(encoding="utf-8", errors="replace")
        test_name = f"test_{module}.py"

        test_src = await self._llm_file(
            f"Here is a Python program (`{entry}`):\n```python\n{original_main[:6500]}\n```\n\n"
            f"Write a test file `{test_name}` that imports from `{module}` and verifies the CORE LOGIC "
            "(calculations, collision, scoring, state transitions) using concrete example inputs with "
            "expected results you can justify.\n"
            "Rules:\n"
            f"- Import the REAL names defined above (e.g. `from {module} import ...`).\n"
            "- Test pure logic only: never open a window, sleep, play audio, or read input.\n"
            # SAY HOW IT WILL BE RUN. The old instruction was "must run as
            # `python <file>` and exit non-zero if a check fails", and what came
            # back was pytest-style — classes under `unittest.main()`, or bare
            # test functions with no main block — which exits 0 having run
            # nothing. The runner uses pytest now, so ask for pytest
            # (audit 2026-11-02, H17).
            "- Write top-level functions named `test_...` that use plain `assert`. "
            "No classes, no `unittest`, no `if __name__ == \"__main__\"` block.\n"
            f"- The file is run with `pytest {test_name}`, which is installed. "
            "Every check must live inside a `test_...` function so it actually runs.\n"
            "- If nothing can be tested without a live GUI or network, reply with EXACTLY: NO_TESTS\n"
            "Reply with ONLY the test file content in one fenced code block (or the bare word NO_TESTS)."
        )
        if not test_src or "NO_TESTS" in test_src.strip().upper()[:40]:
            return "No automated logic tests were applicable."

        # Self-review the expected values before trusting them. A small model
        # sometimes writes an assertion with a wrong expected result (e.g. a
        # board that isn't actually a win); that would flag correct code as
        # broken. Trace each assertion against the real code and fix only the
        # wrong expectations.
        reviewed = await self._llm_file(
            f"Program `{entry}`:\n```python\n{original_main[:5000]}\n```\n\n"
            f"A test written for it:\n```python\n{test_src[:4000]}\n```\n\n"
            "TRACE each assertion's input through the real code and verify the expected value is correct. "
            "Fix ONLY assertions whose expected value is wrong; leave correct ones exactly as they are. "
            "Keep the same imports and structure. Reply with ONLY the corrected test file in one fenced block."
        )
        if reviewed and not self._looks_like_failed_generation(reviewed, test_src):
            try:
                compile(reviewed, test_name, "exec")
                test_src = reviewed
            except Exception as e:  # noqa: BLE001 - SyntaxError, or ValueError for a NUL byte
                # Keep the original test: the review broke it. Recorded,
                # because otherwise "the review changed nothing" and "the
                # review returned code that does not compile" look the same,
                # and only the second says the reviewer is failing.
                logger.warning("builder_test_review_discarded", project=slug,
                               file=test_name,
                               error=f"{type(e).__name__}: {str(e)[:160]}")

        test_path = path / test_name
        test_path.write_text(test_src, encoding="utf-8")
        if await asyncio.to_thread(self._py_compile_error, test_path):
            test_path.unlink(missing_ok=True)  # a test we can't even compile is noise
            return "No reliable logic tests could be generated."

        last_err = ""
        tests_green = False
        for attempt in range(3):
            BUS.publish("project.progress", {"project": slug, "stage": "logic_test", "file": test_name, "attempt": attempt + 1})
            err = await asyncio.to_thread(self._run_test_file, path, test_name)
            if err is None:
                # Tests pass — confirm the (possibly-patched) code still launches.
                if await asyncio.to_thread(self._run_check, path, entry) is None:
                    tests_green = True
                break
            last_err = err
            # Only chase a code fix when a logic ASSERTION failed. An
            # ImportError/AttributeError means the TEST is malformed — don't
            # mutate correct code to satisfy a broken test.
            if "AssertionError" not in err or attempt == 2:
                break
            BUS.publish("project.progress", {"project": slug, "stage": "fixing_logic", "file": entry})
            current = entry_path.read_text(encoding="utf-8", errors="replace")
            fixed = await self._llm_file(
                f"Your program `{entry}` fails a logic test. The test encodes the INTENDED behavior; "
                f"fix the PROGRAM so the test passes.\nTest failure:\n{err[:1000]}\n\n"
                f"`{entry}`:\n```python\n{current[:7000]}\n```\n"
                "Rules: fix the logic bug in the program, not the test. Keep all other behavior. Do not add "
                "imports the file lacks. Reply with EXACTLY ONE fenced code block containing the whole file."
            )
            if self._looks_like_failed_generation(fixed, current):
                break
            try:
                compile(fixed, entry, "exec")
            except Exception:
                break  # never write a fix that breaks syntax
            entry_path.write_text(fixed, encoding="utf-8")

        if tests_green:
            return f"Logic tests passed ({test_name})."
        # Never ship code made worse by a failed fix attempt.
        entry_path.write_text(original_main, encoding="utf-8")
        # A persistently-failing GENERATED test is ambiguous — the model may
        # have written a wrong assertion, so don't brand correct code "broken".
        # Report it honestly as review-worthy and leave the code untouched.
        tail = last_err.strip().splitlines()[-1] if last_err.strip() else "unknown"
        return f"Logic tests inconclusive (code left unchanged; a test may be wrong): {tail[:150]}"

    @staticmethod
    def _run_test_file(project_dir: Path, test_file: str, timeout_s: float = 60.0) -> str | None:
        """Run a generated test file. None = tests RAN and passed.

        "EXIT 0" IS NOT "THE TESTS PASSED" (audit 2026-11-02, H17). This ran
        `python test_main.py` and read exit 0 as success, while the model writes
        pytest-style files:

          * a file of `class TestX:` with `unittest.main()` at the bottom prints
            "Ran 0 tests ... OK" and exits 0;
          * a file of bare `def test_*()` functions and no main block defines
            them and exits 0.

        Both reported "Logic tests passed" having executed NOTHING, and that is
        how a real bug shipped: todo-cli's own generated test catches its
        inverted sort under pytest, and the fix loop written for exactly that
        never fired.

        So: run pytest, and require that it actually collected something. A file
        that collects nothing falls back to a direct run (module-level asserts
        are a legitimate shape) but can only ever report INCONCLUSIVE — never a
        pass, because nothing proved anything.
        """
        import subprocess
        import sys

        def _run(args: list[str]) -> tuple[int, str]:
            try:
                proc = subprocess.run(
                    args, cwd=str(project_dir), capture_output=True, text=True,
                    timeout=timeout_s, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                return -9, "Test run timed out (possible infinite loop or blocking call)."
            except Exception as e:  # noqa: BLE001
                return -1, str(e)[:400]
            return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()

        code, out = _run([sys.executable, "-m", "pytest", test_file, "-q",
                          "-p", "no:cacheprovider"])
        if code == -9 or code == -1:
            return out
        # pytest's own exit codes: 0 all passed, 1 some failed, 2 interrupted,
        # 3 internal error, 4 usage error, 5 nothing collected.
        if code == 0 and _collected_count(out) > 0:
            return None
        if code == 1:
            return out[-1500:] or "a generated test failed"
        if code in (2, 3, 4) and "No module named pytest" not in out:
            return (f"pytest could not run the generated tests (exit {code}):\n"
                    f"{out[-900:]}")

        # Nothing collected, or pytest is unavailable. Run the file directly:
        # that executes module-level asserts, which is a real way to write a
        # check even though pytest cannot see it as a test.
        direct_code, direct_out = _run([sys.executable, test_file])
        if direct_code != 0:
            return (direct_out or f"exit code {direct_code}").strip()[-1500:]
        if _module_level_asserts(project_dir / test_file) > 0:
            # Real checks, really executed, all passed. The shape is unusual;
            # the evidence is not.
            return None
        return ("No test actually executed. pytest collected nothing from "
                f"{test_file} and running it directly asserted nothing, so this "
                "says nothing about whether the code works.")

    @staticmethod
    def _run_check(project_dir: Path, entry: str, timeout_s: float = 8.0) -> str | None:
        """Execute the entry point briefly. None = healthy (clean exit, or
        still running at the timeout — games/UIs run indefinitely). A string
        is the crash output for the fix loop.

        stdin is closed, so `input()`-driven console programs hit EOFError —
        that means "waiting for a human", not a bug, and counts as healthy.
        """
        import subprocess
        import sys

        try:
            proc = subprocess.run(
                [sys.executable, entry],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            # The process stayed alive (a game/UI event loop). That is normally
            # healthy — BUT a GUI toolkit (Tkinter, etc.) catches exceptions
            # raised inside its callbacks and prints the traceback to stderr
            # WITHOUT killing the process. Those are real bugs the run would
            # otherwise hide, so surface a traceback captured before the timeout.
            err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", errors="ignore")
            if err and "Traceback (most recent call last)" in err:
                return err.strip()[-1500:]
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)[:400]

        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
            if "EOFError" in output and "input" in output:
                return None  # interactive program waiting for a user, not a crash
            return output[-1500:]
        # Clean exit, but a swallowed GUI-callback traceback still means broken.
        if proc.stderr and "Traceback (most recent call last)" in proc.stderr:
            return proc.stderr.strip()[-1500:]
        return None

    @staticmethod
    def _py_compile_error(path: Path) -> str | None:
        """Syntax-check a Python file in-process (no bytecode files written)."""
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            compile(source, str(path), "exec")
            return None
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} (line {e.lineno})"
        except Exception as e:  # noqa: BLE001
            return str(e)[:300]

    @staticmethod
    def _looks_like_failed_generation(new_content: str, current: str = "") -> bool:
        """Detect an empty/near-empty LLM file response before it gets written.

        A truncated/unclosed <think> block gets stripped down to nothing (or
        near-nothing) by the LLM layer, but an empty or near-empty Python file
        still compiles cleanly — so `_py_compile_error` alone can't catch a
        "successful" write that actually wiped out real working code. Guard
        against that directly by comparing against the previous content.
        """
        stripped = new_content.strip()
        if len(stripped) < 10:
            return True
        current_stripped = current.strip()
        if current_stripped and len(stripped) < max(40, len(current_stripped) * 0.2):
            return True
        return False

    # ── Improve pipeline ─────────────────────────────────────────────────────

    async def improve(self, *, slug: str, instructions: str) -> dict[str, Any]:
        # An EXISTING identity, from list_projects()/known_slug_in_text()/
        # last_active — resolve it to itself, do not re-canonicalise it into a
        # different project.
        slug = safe_live_component(slug)
        path = self._project_path(slug)
        if not (path / "PROJECT.md").exists():
            return {"project": slug, "started": False, "reason": "unknown project"}
        if self.is_building(slug):
            return {"project": slug, "started": False, "reason": "already building"}

        await self._set_last_active(slug)
        BUS.publish("project.started", {"project": slug, "brief": clip(f"improve: {instructions}", 200), "mode": "improve"})
        task = asyncio.create_task(self._improve(slug, instructions))
        self._active[slug] = task
        return {"project": slug, "started": True, "mode": "improve"}

    async def _improve(self, slug: str, instructions: str) -> None:
        path = self._project_path(slug)
        project_md = self._read_project_md(slug)
        try:
            code_files = [
                p for p in sorted(path.rglob("*"))
                if p.is_file() and p.suffix in {".py", ".html", ".js", ".css", ".json", ".txt"} and p.name != "PROJECT.md"
            ][:8]
            listing = "\n".join(str(p.relative_to(path)) for p in code_files)

            # STAGE 14: an improvement request is a NEW requirement revision.
            # Recording it here invalidates evidence gathered for the previous
            # one, which is exactly what "the requirements changed" means -
            # and it happens BEFORE any file is touched.
            # The revision to build on is the last one that HOLDS a contract,
            # not simply the newest: a revision left behind with no criteria
            # would otherwise carry nothing forward and erase the contract
            # without anyone saying so (audit, H20).
            prev_rev, _prev_text = await self._contract_basis(slug)
            reanchor, drop = await self._plan_contract_change(
                slug, instructions, prev_rev)
            await self._establish_contract(slug, instructions,
                                           previous_revision=prev_rev,
                                           reanchor=reanchor, drop=drop)

            BUS.publish("project.progress", {"project": slug, "stage": "planning_improvements"})
            plan = await self._llm_json(
                f"You are Nova improving an existing project `{slug}`.\n"
                f"PROJECT.md:\n{project_md[:2500]}\n\nFiles:\n{listing}\n\n"
                f"Requested improvements: {instructions}\n\n"
                'Reply ONLY with JSON: {"changes": [{"path": "main.py", "what": "short description"}], "summary": "one sentence"}\n'
                "Rules: at most 3 files, only files that exist or one new file, relative paths only.\n"
                "IMPORTANT — this summary is written BEFORE the code is generated or tested, so it is a "
                "statement of INTENT, not an accomplishment. Describe what you will CHANGE, e.g. "
                "'rewrite the countdown to use nonlocal state and schedule the first tick'. Do NOT write "
                "'fixed', 'resolved', 'stabilized' or 'ensured' — nothing is verified at this point, and "
                "claiming a fix that didn't happen is worse than saying nothing."
            )
            changes = (plan or {}).get("changes") or []
            if not changes:
                raise RuntimeError("no improvement plan produced")

            changed: list[str] = []
            fail_reasons: list[str] = []
            for ch in changes[:3]:
                rel = str(ch.get("path") or "").replace("\\", "/").lstrip("/")
                if not rel or ".." in rel.split("/"):
                    continue
                target = (path / rel).resolve()
                if not str(target).startswith(str(path)):
                    continue

                current = ""
                if target.exists():
                    current = target.read_text(encoding="utf-8", errors="replace")[:7000]

                BUS.publish("project.progress", {"project": slug, "stage": "improving", "file": rel})
                file_prompt = (
                    f"Improve `{rel}` in project `{slug}`.\n"
                    f"Improvement to make: {ch.get('what')}\nOverall request: {instructions}\n\n"
                    f"Current content:\n```\n{current}\n```\n\n"
                    "Return the COMPLETE improved file (not a diff), fully working, no placeholders. "
                    "CRITICAL: if you add a new feature, function, screen, or button, make sure it is "
                    "actually CALLED and reachable by the code path that should trigger it — e.g. a "
                    "game-over/restart screen must be invoked when the game ends, a new button's handler "
                    "must be wired up. Never leave new code defined but never called. "
                    "Reply with ONLY the file content in a single fenced code block."
                )
                new_content = await self._llm_file(file_prompt)
                if self._looks_like_failed_generation(new_content, current):
                    # Retry once — the first attempt likely got truncated mid-reasoning.
                    new_content = await self._llm_file(file_prompt)
                if self._looks_like_failed_generation(new_content, current):
                    # Never overwrite existing working code with an empty/near-empty
                    # response — skip this file instead of destroying it.
                    fail_reasons.append(f"{rel}: generation came back empty/incomplete")
                    BUS.publish(
                        "project.progress",
                        {"project": slug, "stage": "skipped", "file": rel, "error": "generation came back empty/incomplete"},
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")
                changed.append(rel)

                if rel.endswith(".py"):
                    error = await asyncio.to_thread(self._py_compile_error, target)
                    if error and current:
                        # Revert on broken improvement rather than shipping a regression.
                        target.write_text(current, encoding="utf-8")
                        changed.remove(rel)
                        fail_reasons.append(f"{rel}: compile error after edit — {clip(error, 160)}")
                        BUS.publish("project.progress", {"project": slug, "stage": "reverted", "file": rel, "error": clip(error, 160)})

            if not changed:
                detail = "; ".join(fail_reasons) if fail_reasons else "no files matched the plan"
                raise RuntimeError(f"no files were successfully improved ({detail})")

            summary = str((plan or {}).get("summary") or instructions).strip()

            # Improvements get the same verification as fresh builds — a
            # compile-clean change can still crash at startup or break the logic.
            code_candidates = [str(p.relative_to(path)).replace("\\", "/") for p in sorted(path.rglob("*.py"))]
            run_note = await self._verify_and_fix_runtime(slug, path, code_candidates)
            test_note = await self._generate_and_run_tests(slug, path, code_candidates, summary)
            # Same gap on the improve path (audit, H19).
            html_note = await self._smoke_test_html(slug, path, code_candidates)
            old_brief = re.search(r"## Brief\n(.*?)\n\n", project_md, re.DOTALL)

            # WHAT ACTUALLY HAPPENED, not what was planned. `summary` is the
            # planner's statement of intent, written before any code existed;
            # publishing it as the outcome is how "implemented A, B and C"
            # got said about a project where only A exists.
            done = ", ".join(dict.fromkeys(changed)) or "nothing"
            improve_log = [f"Changed: {done}"]
            if fail_reasons:
                # These used to be discarded the moment ONE file succeeded, so
                # a skipped and a reverted requested change vanished from every
                # record while the project reported complete.
                improve_log.extend(f"NOT changed — {r}" for r in fail_reasons)
            if run_note:
                improve_log.append(run_note)
            if test_note:
                improve_log.append(test_note)
            if html_note:
                improve_log.append(html_note)

            await self._validate_criteria(slug, instructions, changed,
                                          run_note=run_note)
            verdict = await self._completion.evaluate(slug=slug)
            improve_log.extend(self._evidence_log(verdict))

            self._write_project_md(
                slug,
                brief=(old_brief.group(1) if old_brief else instructions),
                status=verdict.state,
                verdict=verdict,
                summary=f"requested: {summary}",
                log_lines=improve_log,
            )
            await self._save_fact(slug, "status", verdict.state)
            await self._save_fact(slug, "last_worked", _now_str())
            await self._announcer.announce(
                slug=slug, verdict=verdict, reason="improvement finished",
                extra={"requested": clip(summary, 200), "files": changed,
                       "not_changed": fail_reasons[:5], "mode": "improve",
                       "run_note": clip(run_note or "", 200),
                       "test_note": clip(test_note or "", 200),
                       "html_note": clip(html_note or "", 200),
                       "contract": verdict.seal_mode})
        except Exception as e:  # noqa: BLE001
            logger.warning("project_improve_failed", project=slug, error=str(e)[:300])
            BUS.publish("project.error", {"project": slug, "error": clip(e, 240), "mode": "improve"})
        finally:
            self._active.pop(slug, None)

    # ── Status for chat ──────────────────────────────────────────────────────

    async def status_text(self, slug: str) -> str:
        """What to tell a person about this project, RIGHT NOW.

        The state is DERIVED. This used to read `## Status` out of PROJECT.md,
        which made a projection the authority for everything chat said —
        measured: with the evaluator returning `failing`, a PROJECT.md left
        saying `complete` produced "Project calc: complete." Any stale file, an
        older build, or a hand edit could overrule the evidence.

        PROJECT.md is still read, for the things it IS the record of: the
        progress narrative and the suggestion list. Not for whether the work is
        done.
        """
        # Same rule: an existing identity resolves to itself.
        slug = safe_live_component(slug)
        md = self._read_project_md(slug)
        if not md:
            return f"I don't have a project called {slug} yet."

        def section(name: str) -> str:
            m = re.search(rf"## {name}\n(.*?)(?:\n## |\Z)", md, re.DOTALL)
            return m.group(1).strip() if m else ""

        verdict = await self._completion.evaluate(slug=slug)
        status = verdict.state
        summary = section("Summary")
        sugg = section("Next steps / suggestions")
        log = section("Progress log").splitlines()
        last = log[-1].lstrip("- ").strip() if log else ""

        parts = [f"Project {slug}: {status}."]
        if verdict.failing:
            parts.append("Failing: "
                         + "; ".join(s.criterion.text for s in verdict.failing[:3]) + ".")
        elif verdict.outstanding:
            parts.append("Still to prove: "
                         + "; ".join(s.criterion.text
                                     for s in verdict.outstanding[:3]) + ".")
        if verdict.state == "passing":
            parts.append("The checks that ran all pass, but final acceptance "
                         "is still outstanding.")
        # THE OTHER AXIS. Completion is about whether the artifact satisfies
        # the agreed criteria. The goal/task system is about whether the
        # planned WORK ran. They are independent -- measured: a project whose
        # criteria are demonstrated while its goal and every task failed --
        # and this sentence used to report only the first, so
        #
        #     "Project alpha: complete."
        #
        # was the whole answer to "what's the status of alpha?" for a project
        # whose work plan had failed outright. Neither axis may speak for the
        # other, so when they disagree, both are said.
        try:
            goals = await self._memory.list_goals(project_name=slug, limit=5)
        except Exception:  # noqa: BLE001
            goals = []
        failed_goals = [g for g in goals if str(g.get("status")) == "failed"]
        live_goals = [g for g in goals
                      if str(g.get("status")) in ("active", "blocked")]
        if failed_goals:
            titles = "; ".join(str(g.get("title") or "untitled")
                               for g in failed_goals[:3])
            parts.append(f"Note that the planned work did not all succeed: "
                         f"{len(failed_goals)} goal(s) failed ({titles}).")
        elif live_goals:
            titles = "; ".join(str(g.get("title") or "untitled")
                               for g in live_goals[:3])
            parts.append(f"There is still planned work outstanding: {titles}.")

        if verdict.legacy_note:
            parts.append(verdict.legacy_note)
        if summary and summary != "(pending)":
            parts.append(summary)
        if last:
            parts.append(f"Last activity: {last}")
        if self.is_building(slug):
            parts.append("I'm working on it right now — I'll report when it's done.")
        elif sugg and sugg != "(none yet)":
            pending = [ln.lstrip("- [ ]").strip() for ln in sugg.splitlines() if ln.strip().startswith("- [ ]")]
            if pending:
                parts.append("Suggested next steps: " + "; ".join(pending[:3]) + ". Say the word and I'll implement them.")
        return " ".join(parts)
