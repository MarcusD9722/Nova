from __future__ import annotations

"""Nova's autonomous project builder.

When Marcus asks Nova to build something ("make a snake game called Serpent"),
she creates projects/<slug>/, plans the files with the LLM, writes them,
validates Python files compile, records progress in PROJECT.md and project
memory facts, then reports completion with improvement suggestions.

State model:
- projects/<slug>/PROJECT.md is the on-disk source of truth (brief, status,
  files, how to run, progress log, suggestions) so "where did we leave off?"
  works across sessions.
- Memory facts (entity="project:<slug>") mirror status/summary/next steps so
  chat grounding and semantic search can surface them.
- All writes are confined to projects_dir (slug-sanitized, no traversal).
"""

import asyncio
import os
import re
from core.project_names import (
    WIN_RESERVED, canonical_project_slug, safe_live_component,
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


def _python_stack_note() -> str:
    """Tell the planner which graphics stack actually exists on this machine.

    pygame is not part of Nova's environment; planning a pygame game would
    just fail the run check with ModuleNotFoundError. tkinter ships with the
    standard library and is verified present.
    """
    import importlib.util

    if importlib.util.find_spec("pygame") is not None:
        return "use python (pygame is installed and allowed for graphical games)"
    return (
        "use python with ONLY the standard library — tkinter for graphical games; "
        "pygame and other third-party packages are NOT installed"
    )

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
STATUS_WORDS_RE = re.compile(r"\b(?:where (?:did|do) we leave off|left off|leave off|status|progress|where were we)\b", re.IGNORECASE)
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

#: Tokens after which a title boundary cannot be proven either way.
_AMBIGUOUS_CONTINUATIONS = frozenset({
    "with", "without", "using", "that", "which", "who", "whom", "whose",
    "for", "from", "in", "on", "at", "about", "to", "by", "into", "over",
    "under", "when", "while", "if", "unless", "until", "so", "because",
})

#: Connectives that can introduce a first-person requirement clause. Broader than
#: `_ACTION_CONNECTIVES` because "but I want it simple" and "so I can run it
#: offline" are requirements too.
_CLAUSE_CONNECTIVES = frozenset({"and", "then", "plus", "also", "but", "so",
                                 "or", "while", "although", "though"})

#: A title does not continue into a clause about the speaker or the thing.
_SUBJECT_PRONOUNS = frozenset({"i", "we", "you", "it", "they", "he", "she",
                               "there", "that's", "its", "lets", "let's"})

NAME_AMBIGUOUS = "__NOVA_PROJECT_NAME_AMBIGUOUS__"


def resolve_name_boundary(name: str) -> str:
    """Where the title ends. Returns the name, or NAME_AMBIGUOUS to ask.

    Case-insensitive by construction: every comparison lowercases first, so the
    same words produce the same answer however they were capitalised.
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

        # (4) anything else that could open a requirement is unprovable.
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


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class ProjectBuilder:
    def __init__(self, *, projects_dir: Path, llm: Any, llm_semaphore: asyncio.Semaphore, memory: Any,
                 models: Any | None = None) -> None:
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
            except Exception:
                pass
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
        if not self._projects_dir.exists():
            return []
        return sorted(
            p.name for p in self._projects_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_") and (p / "PROJECT.md").exists()
        )

    def known_slug_in_text(self, text: str) -> str | None:
        lowered = (text or "").lower()
        compact = re.sub(r"[^a-z0-9]", "", lowered)
        exact_slug: list[str] = []
        spaced_slug: list[str] = []
        compact_only: list[str] = []
        for s in self.list_projects():
            part_count = len([p for p in s.split("-") if p])
            if s in lowered:
                exact_slug.append(s)
                continue
            # Very long slugs (many hyphen-separated words) are often just
            # sentence-like artifacts from earlier routing mistakes. Avoid
            # treating those as a spaced phrase match inside normal prose.
            if part_count <= 6 and s.replace("-", " ") in lowered:
                spaced_slug.append(s)
                continue
            # Slugs are single mashed-together words ("flappybird") but users
            # naturally type them with spaces ("flappy bird") — compare with
            # all separators stripped from both sides too. Guarded by a
            # minimum length so short slugs don't match unrelated text.
            s_compact = s.replace("-", "")
            if part_count <= 4 and len(s_compact) >= 5 and s_compact in compact:
                compact_only.append(s)
        # Priority order:
        # 1) Exact hyphenated slug mention typed by the user (strongest signal)
        # 2) Spaced slug phrase mention ("flappy bird")
        # 3) Compact fuzzy match ("flappybird")
        # This prevents complaint text from outranking an explicitly named
        # project, e.g. "flappy-bird is still frozen...".
        if exact_slug:
            return max(exact_slug, key=len)
        if spaced_slug:
            return max(spaced_slug, key=len)
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
            # An unquoted capture that ran into the regex's own length limit is a
            # PREFIX, not a name. Fail closed rather than name a project after a
            # truncated fragment.
            if len(raw) >= _MAX_RAW_NAME:
                return NEEDS_NAME, t
            name = resolve_name_boundary(raw)
            if name == NAME_AMBIGUOUS or not name:
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
                resolved = resolve_name_boundary(name)
                if (resolved != NAME_AMBIGUOUS and resolved
                        and _looks_like_a_title(resolved)):
                    return resolved, t
                return NEEDS_NAME, t
        return NEEDS_NAME, t

    # ── Memory + PROJECT.md state ────────────────────────────────────────────

    async def _save_fact(self, slug: str, attribute: str, value: str) -> None:
        try:
            await self._memory.add_fact(entity=f"project:{slug}", attribute=attribute, value=value[:400], confidence=0.95)
        except Exception:
            pass

    async def _set_last_active(self, slug: str) -> None:
        try:
            await self._memory.add_fact(entity="projects", attribute="last_active", value=slug, confidence=0.95)
        except Exception:
            pass

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
        try:
            if self._project_path(slug).is_dir():
                return slug
        except Exception:  # noqa: BLE001
            return None
        try:
            await self._memory.add_fact(entity="projects", attribute="last_active",
                                        value="", confidence=0.95)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _read_project_md(self, slug: str) -> str:
        path = self._project_path(slug) / "PROJECT.md"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _write_project_md(
        self,
        slug: str,
        *,
        brief: str,
        status: str,
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

        content = (
            f"# {slug}\n\n"
            f"## Brief\n{brief.strip()}\n\n"
            f"## Status\n{status}\n\n"
            f"## Summary\n{summary.strip() or '(pending)'}\n\n"
            f"## Files\n{files_md}\n\n"
            f"## How to run\n{run.strip() or '(pending)'}\n\n"
            f"## Progress log\n" + "\n".join(all_log) + "\n\n"
            f"## Next steps / suggestions\n{sugg_md}\n"
        )
        (path / "PROJECT.md").write_text(content, encoding="utf-8")

    # ── LLM helpers ──────────────────────────────────────────────────────────

    async def _llm_json(self, prompt: str, max_tokens: int = 900) -> dict[str, Any] | None:
        # Planning benefits from native reasoning — thinking happens in the
        # background and is stripped before JSON extraction. One retry covers
        # the occasional run where reasoning eats the whole token budget and
        # the JSON arrives truncated.
        runtime, sem = self._handle("planner")
        for _ in range(2):
            async with sem:
                raw = await runtime.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=0.15,
                    thinking=True,
                )
            obj = extract_first_json_object(raw or "")
            if obj:
                return obj
        return None

    async def _llm_file(self, prompt: str) -> str:
        # Writing/rewriting an actual source file — the `coder` role.
        runtime, sem = self._handle("coder")
        async with sem:
            raw = await runtime.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=_FILE_TOKENS,
                temperature=0.2,
                stop=[],
                thinking=True,
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

            build_log = [f"Wrote {len(written)} file(s). Build complete."]
            if run_note:
                build_log.append(run_note)
            if test_note:
                build_log.append(test_note)
            # Honest state: a crash is "needs attention"; an unresolved generated
            # test (which may itself be wrong) is the softer "needs review".
            run_ok = run_note is None or run_note.startswith("Run check passed")
            tests_inconclusive = bool(test_note and test_note.startswith("Logic tests inconclusive"))
            status = "needs attention" if not run_ok else ("needs review" if tests_inconclusive else "complete")
            self._write_project_md(
                slug, brief=brief, status=status, summary=summary, files=files, run=run,
                log_lines=build_log,
                suggestions=suggestions,
            )
            await self._save_fact(slug, "status", status)
            await self._save_fact(slug, "summary", summary)
            if suggestions:
                await self._save_fact(slug, "next_steps", "; ".join(suggestions))
            await self._save_fact(slug, "last_worked", _now_str())

            BUS.publish(
                "project.completed",
                {"project": slug, "summary": clip(summary, 200), "files": written, "run": clip(run, 120),
                 "suggestions": suggestions, "run_note": clip(run_note or "", 200),
                 "test_note": clip(test_note or "", 200), "status": status},
            )
            logger.info("project_build_complete", project=slug, files=len(written))
        except Exception as e:  # noqa: BLE001
            logger.warning("project_build_failed", project=slug, error=str(e)[:300])
            try:
                self._write_project_md(slug, brief=brief, status="error", log_lines=[f"Build failed: {e}"])
                await self._save_fact(slug, "status", f"error: {str(e)[:200]}")
            except Exception:
                pass
            BUS.publish("project.error", {"project": slug, "error": clip(e, 240)})
        finally:
            self._active.pop(slug, None)

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
            "- Use plain `assert` statements and the standard library only.\n"
            f"- The file must run as `python {test_name}` and exit non-zero if any check fails.\n"
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
            except Exception:
                pass  # keep the original test if the review broke it

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
    def _run_test_file(project_dir: Path, test_file: str, timeout_s: float = 20.0) -> str | None:
        """Run a generated test file. None = passed (exit 0); a string is the failure output."""
        import subprocess
        import sys

        try:
            proc = subprocess.run(
                [sys.executable, test_file],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "Test run timed out (possible infinite loop or blocking call)."
        except Exception as e:  # noqa: BLE001
            return str(e)[:400]
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-1500:]
        return None

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
            run_ok = run_note is None or run_note.startswith("Run check passed")
            tests_inconclusive = bool(test_note and test_note.startswith("Logic tests inconclusive"))
            status = "needs attention" if not run_ok else ("needs review" if tests_inconclusive else "complete")

            old_brief = re.search(r"## Brief\n(.*?)\n\n", project_md, re.DOTALL)
            improve_log = [f"Improved: {', '.join(dict.fromkeys(changed))} — {summary}"]
            if run_note:
                improve_log.append(run_note)
            if test_note:
                improve_log.append(test_note)
            self._write_project_md(
                slug,
                brief=(old_brief.group(1) if old_brief else instructions),
                status=status,
                summary=summary,
                log_lines=improve_log,
            )
            await self._save_fact(slug, "status", status)
            await self._save_fact(slug, "last_worked", _now_str())
            BUS.publish("project.completed", {"project": slug, "summary": clip(summary, 200), "files": changed,
                                              "mode": "improve", "run_note": clip(run_note or "", 200),
                                              "test_note": clip(test_note or "", 200), "status": status})
        except Exception as e:  # noqa: BLE001
            logger.warning("project_improve_failed", project=slug, error=str(e)[:300])
            BUS.publish("project.error", {"project": slug, "error": clip(e, 240), "mode": "improve"})
        finally:
            self._active.pop(slug, None)

    # ── Status for chat ──────────────────────────────────────────────────────

    def status_text(self, slug: str) -> str:
        # Same rule: an existing identity resolves to itself.
        slug = safe_live_component(slug)
        md = self._read_project_md(slug)
        if not md:
            return f"I don't have a project called {slug} yet."

        def section(name: str) -> str:
            m = re.search(rf"## {name}\n(.*?)(?:\n## |\Z)", md, re.DOTALL)
            return m.group(1).strip() if m else ""

        status = section("Status") or "unknown"
        summary = section("Summary")
        sugg = section("Next steps / suggestions")
        log = section("Progress log").splitlines()
        last = log[-1].lstrip("- ").strip() if log else ""

        parts = [f"Project {slug}: {status}."]
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
