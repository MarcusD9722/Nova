from __future__ import annotations

"""Does this message AUTHORISE modifying a project on disk?

One deterministic gate in front of every `ProjectBuilder.improve()` call, and
the reason it exists:

    Marcus:  "I had worked on your code these last few days trying to improve
              your overall performance and sturdiness."
    Nova:    *starts editing blue-and-tower-defense-and-i-want-you-to*

    Marcus:  "I wasnt trying to have you upgrade anything. I was just making
              small talk."
    Nova:    *starts editing it again*

    Marcus:  "Stop making false improvements. Im trying to run tests on you."
    Nova:    *starts editing it a third time*

The old rule was: if the sentence contains a word matching
`improv*|enhanc*|upgrad*|polish*|refactor*|fix*|extend*`, that is "continuation
intent"; if no project is named, use `last_active()`. So a KEYWORD was
sufficient authority to write to the filesystem — including inside a sentence
telling her to stop.

THE INVARIANT
-------------
A project action keyword is not authorisation. Mutation requires BOTH
affirmative intent to perform it AND a target that resolves safely. Ambiguity
fails CLOSED, into conversation, because the cost of wrongly discussing
something is a sentence and the cost of wrongly editing is Marcus's files.

WHY THIS IS DETERMINISTIC
-------------------------
A stochastic classifier is welcome to help decide what to TALK about; it is not
evidence for crossing a side-effect boundary. Everything here is inspectable
regex and every refusal names its reason, so a wrong answer can be read off the
verdict rather than guessed at.
"""

import re
from dataclasses import dataclass

from core.intent import is_question, strip_preamble

__all__ = [
    "MutationVerdict",
    "authorize_project_mutation",
    "is_project_selection",
    "asks_current_project",
    "describes_a_change",
    "cancels_pending_change",
    "defers_a_change",
    "withdraws_pending_change",
    "carries_a_proposal",
    "is_bare_approval",
    "approves_without_naming_a_change",
    "qualified_project_name",
    "requests_project_removal",
    "classify_removal",
    "REMOVAL_NONE",
    "REMOVAL_WHOLE_PROJECT",
    "REMOVAL_INSIDE_PROJECT",
    "REMOVAL_AMBIGUOUS",
    "removal_object_tokens",
    "REMOVAL_UNSUPPORTED",
]


# TWO VOCABULARIES, and the difference is the whole point.
#
# `_ACTION_VERB` is broad on purpose — stem-plus-anything — because "does this
# sentence talk about changing something at all" is a question about topic, and
# "improving", "deleted", "changes" all count.
#
# `_IMPERATIVE_VERB` is the BARE COMMAND FORM only. An English imperative is
# always the bare stem: "Delete the project", never "Deleting the project".
# Detection and grammar are different questions and were previously answered by
# the same pattern, so `delet\w*` matched "Deleting" and a start-anchored
# imperative check read a gerund SUBJECT as a command.
#
# An earlier fix subtracted gerunds afterwards by vetoing any opening word
# spelled `\w+ing`. That is lexical, not grammatical, and it broke "Bring up
# the project and add a pause button" — `bring` ends in "ing" — which also made
# `bring up`, one of this module's own declared openers, unreachable. Encoding
# the grammar directly means there is nothing to subtract.

#: Anything that TALKS about changing a project, in any inflection.
_ACTION_VERB = (
    r"(?:improv\w*|enhanc\w*|upgrad\w*|polish\w*|refactor\w*|fix\w*|extend\w*|"
    r"add\w*|implement\w*|appl(?:y|ies|ied)|chang\w*|updat\w*|rewrit\w*|"
    r"build\w*|creat\w*|mak\w*|continu\w*|resum\w*|finish\w*|complet\w*|"
    r"remov\w*|delet\w*|set\s+up|wire\s+up|hook\s+up|work\s+on|"
    r"keep\s+(?:working|going))"
)
_ACTION_VERB_RE = re.compile(r"\b" + _ACTION_VERB + r"\b", re.IGNORECASE)

#: The same verbs in the only form an imperative can take. No `\w*`: adding one
#: back is what let a gerund through, so the closed list IS the safety property.
_IMPERATIVE_VERB = (
    r"(?:improve|enhance|upgrade|polish|refactor|fix|extend|add|implement|"
    r"apply|change|update|rewrite|build|create|make|continue|resume|finish|"
    r"complete|remove|delete|set\s+up|wire\s+up|hook\s+up|work\s+on|"
    r"keep\s+(?:working|going))"
)

# ── VETOES ──────────────────────────────────────────────────────────────────
# Each of these means "whatever else this sentence contains, it is not an
# instruction to change a project."

#: "Stop making false improvements." / "Don't improve anything." / "Never
#: upgrade that automatically." A prohibition names the action precisely
#: because the user does NOT want it.
_PROHIBITION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:stop|quit|cease|halt|don'?t|do\s+not|never|hold\s+off)\b"
    r"|\b(?:don'?t|do\s+not|never|stop|quit|cease)\s+(?:\w+\s+){0,3}" + _ACTION_VERB + r"\b"
    r"|\bno\s+need\s+to\s+(?:\w+\s+){0,2}" + _ACTION_VERB + r"\b",
    re.IGNORECASE,
)

#: "I wasnt trying to have you upgrade anything." / "I wasn't asking you to fix
#: flappy-bird." / "I did not ask you to improve the project." A denial that a
#: request was ever made is the opposite of making one.
_DENIAL_RE = re.compile(
    r"\bi\s+(?:wasn'?t|wasnt|was\s+not|didn'?t|did\s+not|never|don'?t|do\s+not)\b"
    r"|\b(?:wasn'?t|wasnt|weren'?t|didn'?t|did\s+not|not)\s+(?:\w+\s+){0,3}"
    r"(?:ask\w*|tell\w*|want\w*|tr(?:y|ied|ying)|mean\w*|request\w*|say\w*)\b"
    r"|\bnot\s+(?:asking|telling|trying|requesting|suggesting)\b",
    re.IGNORECASE,
)

#: "I improved the project yesterday." / "I had worked on your code…" A report
#: of work already done is not a request to do work now. This is the shape that
#: opened the live conversation.
_RETROSPECTIVE_RE = re.compile(
    r"\b(?:i|we|i'?ve|we'?ve|i'?d)\s+(?:had\s+|have\s+|already\s+|just\s+)*(?:been\s+)?"
    r"(?:improved|upgraded|fixed|refactored|enhanced|polished|extended|"
    r"worked|working|made|built|changed|updated|wrote|written|"
    r"tested|testing|talking|talked|discussing|discussed|checking|checked|"
    r"was|were)\b",
    re.IGNORECASE,
)

#: "improve your overall performance", "your runtime could be improved" — the
#: target is NOVA, not a project. This is why `last_active()` was reached at
#: all: nothing checked whether the thing to be improved was even a project.
_SELF_TARGET_RE = re.compile(
    r"\byour\s+(?:code|codebase|code\s?base|source|runtime|performance|"
    r"behaviou?rs?|responses?|answers?|speed|latency|memory|personality|"
    r"sturdiness|reliability|stability|upgrades?|improvements?)\b"
    r"|\b(?:improv\w*|upgrad\w*|enhanc\w*|fix\w*|refactor\w*|polish\w*)\s+"
    r"(?:you|yourself)\b",
    re.IGNORECASE,
)

#: "Should we refactor it?", "I think improving it might help", "tell me how
#: you would improve it" — deliberation about a possible action.
_DELIBERATIVE_RE = re.compile(
    r"\b(?:should\s+(?:we|i|you)|could\s+(?:we|i|you)|shall\s+we|what\s+if|"
    r"do\s+you\s+think|i\s+think|i\s+wonder|i\s+guess|maybe\s+we|perhaps\s+we|"
    r"might\s+(?:help|be|work|want)|tell\s+me\s+how|how\s+would\s+you|"
    r"what\s+(?:improvements?|changes?|else)|would\s+it\s+be)\b",
    re.IGNORECASE,
)

#: A TRAILING deferral clause: "..., but not yet.", "..., not yet though."
#:
#: `_DEFERRED_PROHIBITION_RE` needs a negated imperative ("don't change it
#: yet"), so a bare trailing "not yet" was invisible and the sentence was
#: treated as an ordinary authorised instruction. Measured: "Actually make
#: the bird fall slower instead, not yet though." edited the project
#: immediately, having been told not to.
#:
#: DELIBERATELY ANCHORED AT THE END. "not yet" far more often describes the
#: WORLD than the timing of the request, and reading those as deferrals
#: would refuse work that was actually asked for:
#:
#:     "The score is not showing yet, add it."    an instruction, now
#:     "It is not done yet, keep going."          an instruction, now
#:     "Make it slower, but not yet."             a deferral
#:
#: Only a clause that CLOSES the sentence is the speaker qualifying their
#: own request.
_TRAILING_DEFERRAL_RE = re.compile(
    r"(?:,|;|-)?\s*(?:but|though|although)?\s*"
    r"\b(?:not|no)\s+(?:just\s+)?yet"
    r"(?:\s+(?:though|however))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)


_VETOES = (
    ("prohibition", _PROHIBITION_RE),
    # A deferral is not a prohibition: it wants the change, later. It gets its
    # own name so the refusal reason says which one refused, and so the caller
    # can turn it into a pending proposal rather than dropping it.
    ("deferral", _TRAILING_DEFERRAL_RE),
    ("denial", _DENIAL_RE),
    ("retrospective", _RETROSPECTIVE_RE),
    ("self_target", _SELF_TARGET_RE),
    ("deliberative", _DELIBERATIVE_RE),
)

# ── AFFIRMATIVE EVIDENCE ────────────────────────────────────────────────────

#: "Improve flappy-bird's collision handling." — an imperative opening.
_IMPERATIVE_RE = re.compile(
    r"^\s*(?:please\s+|now\s+|then\s+|also\s+|and\s+)*" + _IMPERATIVE_VERB + r"\b",
    re.IGNORECASE,
)

#: "Can you fix the collision bug", "let's add a restart button", "I want you
#: to refactor it" — a request rather than a bare imperative. Bare forms again:
#: every one of these frames is followed by a command verb.
_REQUEST_RE = re.compile(
    r"^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?" + _IMPERATIVE_VERB + r"\b"
    r"|^\s*(?:let'?s|lets)\s+(?:please\s+)?" + _IMPERATIVE_VERB + r"\b"
    # "I'd like you to add X". The old alternation required whitespace between
    # "i" and "'d", so the contracted form - the one people actually type - never
    # matched and an explicit request read as no instruction at all.
    r"|^\s*i(?:'?d)?\s+(?:want|need|would\s+like|like)\s+you\s+to\s+" + _IMPERATIVE_VERB + r"\b"
    # "Help me add a pause button" is a request, not a description.
    r"|^\s*(?:please\s+)?help\s+me\s+(?:to\s+)?" + _IMPERATIVE_VERB + r"\b",
    re.IGNORECASE,
)

#: `re.escape` escapes spaces (they are significant under re.VERBOSE), so a
#: phrase comes back as "switch\ to" and the separator to rewrite is that
#: escaped pair, not a bare space. Getting this wrong produces an alternation
#: that silently matches nothing.
_ESCAPED_SPACE = chr(92) + " "


def _alt(phrases: tuple[str, ...]) -> str:
    """One alternation built FROM a list, so tests can enumerate the real thing.

    Longest first: regex alternation is first-match-wins, so "switch over to"
    has to be tried before "switch to" or the trailing "over to" is left for the
    rest of the pattern to choke on.
    """
    parts = [re.escape(p).replace(_ESCAPED_SPACE, r"\s+").replace(" ", r"\s+")
             .replace("'", "'?")
             for p in sorted(phrases, key=len, reverse=True)]
    return "(?:" + "|".join(parts) + ")"


#: Verbs that open a project without changing it. On their own they authorise
#: nothing, which is why they are not action verbs.
OPENER_STARTERS = (
    "open", "look at", "go to", "pull up", "bring up", "check", "read",
    "review", "inspect", "load",
)
_OPENER_VERB = _alt(OPENER_STARTERS)

#: Phrases that mean "make this project the one we're on". Same job as an
#: opener — they bring a project into view and change nothing inside it — so
#: the compound-imperative grammar below has to know about them too.
#:
#: It did not, and that was a defect: `_COMPOUND_IMPERATIVE_RE` only knew the
#: OPENERS, so "Switch to calc-tool and add a memory button." did not read as
#: an instruction, `is_project_selection()` therefore accepted it, and because
#: selection is resolved before mutation the action half was silently dropped.
#: Nova switched project and did nothing. "Open calc-tool and add a memory
#: button." worked the whole time, which is why a sample-based suite missed it.
SELECTION_STARTERS = (
    "let's work on", "let's look at", "let's switch to", "let's go back to",
    "let's return to", "let's jump into", "let's jump back into",
    "let us work on", "let us look at",
    "switch to", "switch over to",
    "go back to", "come back to", "head back to",
    "jump back to", "jump back into",
    "back to", "return to",
    "go to", "go into", "jump to", "jump into",
    "open", "pull up", "bring up", "load",
)
_SELECT_VERB = _alt(SELECTION_STARTERS)

#: Everything that brings a project into view without changing it. The union is
#: what "did this sentence merely point at a project, or point AND instruct?"
#: has to be asked against.
_ENTRY_VERB = "(?:" + _OPENER_VERB + "|" + _SELECT_VERB + ")"

#: Conversational lead-ins that carry no intent of their own.
_LEADIN = (
    r"(?:please\s+|now\s+|then\s+|also\s+|and\s+|"
    r"ok(?:ay)?[,.]?\s+|alright[,.]?\s+|right[,.]?\s+|so[,.]?\s+|"
    r"anyway[,.]?\s+)*"
)

#: "Open the flappy-bird project and add a pause button." — one imperative
#: sentence whose FIRST verb merely opens something and whose SECOND clause is
#: the real instruction.
#:
#: `_IMPERATIVE_RE` only ever looked at the opening verb, so this whole shape
#: was refused as "no affirmative instruction" (measured in Stage 13A). Nova
#: then discussed a change the user had plainly asked her to make.
#:
#: The action verb must come directly after `and`/`then` — i.e. in an
#: imperative clause of its own. Requiring it merely to appear SOMEWHERE would
#: authorise "Open the project and tell me what you would change", which
#: contains "change" and is a question about intent, not an instruction.
_COMPOUND_IMPERATIVE_RE = re.compile(
    r"^\s*" + _LEADIN + _ENTRY_VERB + r"\b"
    r".*?\b(?:and|then)\s+(?:please\s+|also\s+|now\s+)*" + _IMPERATIVE_VERB + r"\b",
    re.IGNORECASE | re.DOTALL,
)

#: "Go ahead and apply those improvements." / "Yes, implement it." — approval
#: of something already proposed. Requires an action verb as well, so a bare
#: "yes" never authorises anything on its own.
#:
#: KNOWN GAP, deliberately left closed (Stage 13A): a bare "Go ahead." / "Do
#: that." / "Yes, do it." carries no action verb and is refused. Those are
#: genuine approvals when Nova has just proposed something, but this gate is
#: context-free by design, and the message alone cannot tell an approval of a
#: pending proposal from an idle "go ahead" in conversation. Closing it needs a
#: pending-proposal signal threaded in from conversation state, not a looser
#: pattern here — loosening would let "go ahead" in small talk mutate a project,
#: which is the exact failure this module exists to prevent.
_AFFIRMATIVE_LEAD_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|alright|go\s+ahead|go\s+for\s+it|"
    r"do\s+it|please)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MutationVerdict:
    """Whether a message authorises changing a project, and why."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # `if verdict:` reads naturally at call sites
        return self.allowed


def authorize_project_mutation(text: str, *, complaint: bool = False) -> MutationVerdict:
    """Deterministic authorisation for a project-modifying action.

    `complaint` is the caller's CONTINUATION_COMPLAINT_RE result ("that didn't
    work", "it's still broken"). A complaint about work Nova just delivered is
    affirmative — the user is telling her the delivered result is wrong — but it
    is still subject to every veto, so "I wasn't complaining" cannot mutate.
    """
    raw = (text or "").strip()
    if not raw:
        return MutationVerdict(False, "empty message")

    for name, veto in _VETOES:
        if veto.search(raw):
            return MutationVerdict(False, f"vetoed: {name}")

    # A question asks; it does not instruct. (Checked after the vetoes so the
    # refusal reason stays specific.)
    if is_question(raw):
        return MutationVerdict(False, "vetoed: question")

    core = strip_preamble(raw)
    if _IMPERATIVE_RE.match(core):
        return MutationVerdict(True, "affirmative: imperative instruction")
    if _COMPOUND_IMPERATIVE_RE.match(core):
        return MutationVerdict(True, "affirmative: compound imperative instruction")
    if _REQUEST_RE.match(core):
        return MutationVerdict(True, "affirmative: direct request")
    if _AFFIRMATIVE_LEAD_RE.match(core) and _ACTION_VERB_RE.search(core):
        return MutationVerdict(True, "affirmative: approval of a proposed action")
    if complaint:
        return MutationVerdict(True, "affirmative: complaint about work in flight")

    # Nothing here says "do it". Fail closed: talk about it instead.
    return MutationVerdict(False, "no affirmative instruction")


# ── SELECTION: which project are we on, without touching it ─────────────────
#
# A third answer was missing. Every message was either "change this project" or
# "not a project message", so "Let's work on the calculator" and "Go back to
# Flappy Bird" resolved a slug, failed the mutation gate, and fell through —
# leaving `projects/last_active` empty. Nova could not say what she was working
# on because nothing had ever recorded it.
#
# Selecting is not mutating. It records focus and writes nothing to the
# project, so it is deliberately NOT gated by `authorize_project_mutation`: the
# cost of wrongly selecting is that Nova is pointed at the wrong project and
# the next sentence corrects her, which is not the cost that gate exists for.

#: "Let's work on X" / "Switch to X" / "Go back to X" / "Open X".
_SELECT_PROJECT_RE = re.compile(
    r"^\s*" + _LEADIN + _SELECT_VERB + r"\b", re.IGNORECASE)

#: "What project are we working on?" / "Which project is this?"
_CURRENT_PROJECT_Q_RE = re.compile(
    r"\b(?:what|which)\s+project\s+(?:are\s+we|am\s+i|are\s+you|is\s+this|"
    r"were\s+we|do\s+we)\b"
    r"|\bwhat(?:'?s| is)\s+(?:the\s+)?(?:current|active)\s+project\b"
    r"|\bwhich\s+one\s+are\s+we\s+(?:on|working\s+on)\b",
    re.IGNORECASE,
)


def is_project_selection(text: str) -> bool:
    """Does this message ask to make a project current, without changing it?

    False for a compound imperative — "Open flappy-bird and add a pause button"
    names a real action, and treating it as mere selection would downgrade an
    explicit instruction to conversation, which is the failure I2 covers.
    False for prohibitions and questions for the same reasons the mutation gate
    refuses them.

    "Let's work on X" IS selection even though the mutation gate reads it as a
    request: the sentence names no action to perform, so pointing at the project
    and waiting is the honest response. Starting an autonomous edit off it is
    the over-eager behaviour this whole module exists to stop.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if _PROHIBITION_RE.search(raw) or _DENIAL_RE.search(raw):
        return False
    if is_question(raw):
        return False
    core = strip_preamble(raw)
    if _COMPOUND_IMPERATIVE_RE.match(core):
        return False
    return bool(_SELECT_PROJECT_RE.match(core))


def asks_current_project(text: str) -> bool:
    """"What project are we working on?" — a read of the current pointer."""
    return bool(_CURRENT_PROJECT_Q_RE.search((text or "").strip()))


# ── CANCELLATION AND CONTEXTUAL APPROVAL ─────────────────────────
#
# A pending proposal had create, replace and consume but no CANCEL, and the two
# halves of that failed in opposite directions:
#
#   "Actually don't."          left the plan pending, so a later approval ran it
#   "Don't make that change."  was STORED as the new plan, so a later approval
#                              executed the cancellation as an instruction
#
# Both measured on 3278f39. Cancelling has to invalidate the proposal, and a
# cancellation must never become one.

_CANCEL_RE = re.compile(
    r"^\s*(?:actually[,.]?\s+|no[,.]?\s+|oh[,.]?\s+)*"
    r"(?:don'?t|do\s+not)\s*[.!]*$"
    # Clause-initial only: "I never mind waiting" is not a cancellation.
    r"|(?:^|[.!?,;]\s*)(?:never\s?mind|nevermind)\b"
    r"|\bforget\s+(?:it|that|those|the\s+(?:change|idea)|about\s+(?:it|that))\b"
    r"|\bcancel\s+(?:that|it|the\s+change|those)\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:bother|make\s+(?:that|the|any)\s+chang\w*|"
    r"do\s+(?:that|it))\b"
    r"|\bleave\s+(?:it|that|them)\s+(?:the\s+way\s+(?:it|they)\s+(?:is|are|was|were)|"
    r"as\s+(?:it\s+)?is|alone|be)\b"
    r"|\b(?:scrap|drop|skip|shelve)\s+(?:that|it|the\s+change)\b"
    r"|\bon\s+second\s+thought\s*,?\s*(?:no|don'?t)\b",
    re.IGNORECASE,
)

#: "Go ahead." / "Do that." / "Yes, do it." — approval that names NO action.
#:
#: Context-free this is not authority and `authorize_project_mutation` still
#: refuses it, deliberately and unchanged: an idle "go ahead" in conversation
#: is the same string. What makes it an instruction is a valid pending proposal
#: FOR THE CURRENT PROJECT, which only the turn path can know. Hence a separate
#: predicate that reports the SHAPE, and a caller that supplies the context.
#:
#: Anchored at both ends on purpose. "Go ahead and delete everything" names an
#: action and belongs to the ordinary gate, not here.
_BARE_APPROVAL_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,.]?\s+|alright[,.]?\s+|sure[,.]?\s+|"
    r"yes[,.]?\s+|yeah[,.]?\s+|yep[,.]?\s+|right[,.]?\s+|"
    r"please[,.]?\s+|fine[,.]?\s+)*"
    r"(?:go\s+ahead|go\s+for\s+it|do\s+(?:that|it|so)|"
    r"please\s+do)"
    r"\s*[.!]*\s*$",
    re.IGNORECASE,
)


#: "Okay, make that change." / "Yes, apply that." — an approval that DOES carry
#: an action verb, and therefore passes the ordinary gate, but whose object is a
#: pronoun pointing at something said earlier. It names no concrete change.
#:
#: Same hazard as a bare approval, one step later: with no proposal to resolve
#: "that" against, the builder was handed the approval's own words as the
#: instruction and started an edit that could not know what to do. Measured on
#: 3278f39 after the plan was correctly scoped away: the approval still ran, on
#: the wrong project, instructed with the sentence "Okay, make that change."
_ANAPHORIC_APPROVAL_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,.]?\s+|alright[,.]?\s+|sure[,.]?\s+|"
    r"yes[,.]?\s+|yeah[,.]?\s+|yep[,.]?\s+|right[,.]?\s+|"
    r"please[,.]?\s+|fine[,.]?\s+|then\s+|now\s+|and\s+)*"
    r"(?:go\s+ahead\s+and\s+)?"
    + _IMPERATIVE_VERB +
    r"\s+(?:that|it|those|them|these|this)"
    r"(?:\s+(?:chang\w*|edit|update|fix|improvement\s?))?"
    r"\s*[.!]*\s*$",
    re.IGNORECASE,
)


def approves_without_naming_a_change(text: str) -> bool:
    """Does this approve something described earlier without restating it?

    True for "Okay, make that change." and "Yes, apply those." — both of which
    the mutation gate rightly allows, and neither of which says WHAT to do. The
    caller must resolve them against a pending proposal and refuse honestly when
    there is none, rather than passing the sentence itself to a builder.
    """
    return bool(_ANAPHORIC_APPROVAL_RE.match((text or "").strip()))


#: A prohibition scoped in TIME, recognised as a CLAUSE rather than by looking
#: for time words anywhere in the sentence.
#:
#: The clause shape is the whole point. Searching the message for "later" or
#: "eventually" classified these as deferrals:
#:
#:   "Don't fix the bug that happens later in the level."
#:   "Don't change the animation that appears later."
#:   "Don't modify the eventually-called cleanup function."
#:
#: In every one of them the temporal word describes the OBJECT, not the
#: prohibition, and treating them as deferrals stored a ban as pending work that
#: a later "Go ahead." would carry out. Measured on c86bfb1.
#:
#: So the qualifier has to modify the prohibition itself: it must follow the
#: negated verb across a SHORT object - at most three words - and sit at a
#: clause boundary. The short object is what does the discriminating: "Don't
#: change it yet" qualifies the prohibition, while "Don't change the animation
#: that appears later" puts a four-word relative clause in between.
#:
#: Anchoring to the end of the MESSAGE instead was the first attempt, and it
#: broke a real deferral: "...but don't change anything yet. First tell me what
#: you think should change." The journey caught it; the new tests did not.
#:
#: Bare "later" is not a qualifier at all — only the bound form "until later" —
#: because "the later animation" and "happens later" are ordinary object talk.
#: "never" is not an opener: it is a prohibition over all time, the opposite of
#: putting something off.
_DEFER_QUALIFIER = (
    r"(?:just\s+yet|yet|right\s+now|for\s+now|at\s+the\s+moment|"
    r"for\s+the\s+moment|for\s+the\s+time\s+being|"
    r"until\s+later|till\s+later|until\s+then)"
)

_DEFERRED_PROHIBITION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|no\s+need\s+to)\s+"
    r"(?:\w+\s+){0,3}?" + _IMPERATIVE_VERB + r"\b"
    r"(?:\s+[\w'-]+){0,3}?"
    r"\s+" + _DEFER_QUALIFIER +
    r"(?=[\s.,;!?]|$)",
    re.IGNORECASE,
)

#: "Don't change anything." / "Don't make any changes." — a prohibition whose
#: object is EVERYTHING. Unqualified, that is a withdrawal of whatever was on
#: the table; qualified by time it is the deferral half of a proposal, which is
#: why the caller checks `defers_a_change` first.
_GENERIC_PROHIBITION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|no\s+need\s+to)\s+"
    r"(?:make\s+any\s+chang\w*|chang\w*\s+anything|do\s+anything|"
    r"touch\s+anything|modify\s+anything|edit\s+anything|"
    r"chang\w*\s+any(?:thing)?)\b",
    re.IGNORECASE,
)

#: What a prohibition is ABOUT: the words after the negated verb.
#:
#: A withdrawal has to be tied to the proposal it refers to. Cancelling whatever
#: happens to be pending would mean "Don't change the physics." silently erased
#: an unrelated dark-mode proposal; cancelling nothing meant a withdrawn change
#: stayed pending and a later "Go ahead." executed it. Both are wrong, and the
#: object is what separates them.
_PROHIBITION_TARGET_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never|stop|no\s+need\s+to)\s+"
    r"(?:\w+\s+){0,3}?(?P<verb>" + _IMPERATIVE_VERB + r")\b"
    r"(?P<target>[^.;!?]*)",
    re.IGNORECASE,
)

#: An action verb under negation, used to find the parts of a sentence that are
#: NOT proposing anything.
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never|stop|no\s+need\s+to|can'?t|cannot)\s+"
    r"(?:\w+\s+){0,3}?" + _ACTION_VERB + r"\b",
    re.IGNORECASE,
)

# WHAT KIND of change, not just what it is about.
#
# Cancellation compared TARGET NOUNS only, so "Don't remove the menu." withdrew
# "Add a pause button to the menu." — both say "menu". Withdrawing an action
# nobody proposed is not a withdrawal, so the action family has to match too.
#
# Stems, because the vocabulary is inflected: "changing" and "changed" belong to
# the same family as "change".
_FAMILY_STEMS = (
    ("add", ("implement", "creat", "build", "extend", "wire", "hook", "add")),
    ("remove", ("remov", "delet")),
    ("modify", ("improv", "enhanc", "upgrad", "polish", "refactor", "rewrit",
                "chang", "updat", "appl", "fix", "mak")),
    ("continue", ("continu", "resum", "finish", "complet", "work", "keep")),
)


def _family_of(word: str) -> str:
    w = (word or "").lower().strip()
    for family, stems in _FAMILY_STEMS:
        for stem in stems:
            if w.startswith(stem):
                return family
    return ""


def _positive_families(text: str) -> set:
    """Action families the message PROPOSES, ignoring anything negated."""
    raw = (text or "")
    spans = [m.span() for m in _NEGATED_ACTION_RE.finditer(raw)]
    out = set()
    for m in _ACTION_VERB_RE.finditer(raw):
        if any(a <= m.start() < b for a, b in spans):
            continue
        family = _family_of(m.group(0))
        if family:
            out.add(family)
    return out

#: Words that carry no identity, so they cannot decide what a withdrawal is
#: about. A target made only of these is a GENERIC withdrawal ("don't change
#: it") and refers to whatever is on the table.
_TARGET_STOPWORDS = frozenset("""
a an the this that these those it its them they he she his her their our my
your any anything something everything nothing some all both each either
to of in on at for with from into onto by about over under and or but so
is are was were be been being do does did done have has had
thing things stuff bit part now right just yet please really actually
""".split())


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower())
            if w not in _TARGET_STOPWORDS and len(w) > 2}


#: What a removal instruction is actually asking for. A boolean could not say:
#: "delete it from my projects" and "remove the bird from flappy-bird" are both
#: removals, and neither is a whole-project delete, but they need OPPOSITE
#: handling - one must not act at all, the other is ordinary edit work.
REMOVAL_NONE = "not_removal"
REMOVAL_WHOLE_PROJECT = "whole_project"
REMOVAL_INSIDE_PROJECT = "inside_project"
REMOVAL_AMBIGUOUS = "ambiguous"
REMOVAL_UNSUPPORTED = "unsupported_lifecycle"

#: Verbs that ask for a thing to stop existing.
_REMOVAL_VERB_RE = re.compile(
    r"\b(delete|deleting|remove|removing|erase|erasing|trash|purge|purging|"
    r"get\s+rid\s+of|throw\s+(?:it\s+)?away)\b", re.I)

#: Verbs for a lifecycle Nova does not have. A user reaching for "retire" or
#: "archive" means something real, and it is NOT delete: there is no retired
#: state to move a project into, no way back from one, and nothing that would
#: list it afterwards. Mapping them onto `project.delete` would answer a
#: question nobody asked with an irreversible action - and the live session
#: that started this whole thread opened with "retire with-you".
_UNSUPPORTED_LIFECYCLE_RE = re.compile(
    r"\b(retire|retiring|retired|archive|archiving|archived|"
    r"shelve|shelving|mothball)\b", re.I)

#: After one of these, whatever follows is WHERE the removal happens, not what
#: is being removed.
_CONTAINER_PREP_RE = re.compile(
    r"\b(from|in|inside|within|out\s+of|off\s+of)\b", re.I)

#: Words that cannot name anything on their own, so an object made only of
#: these identifies nothing.
_EMPTY_OBJECT = frozenset("""
a an the this that these those my our your its his her their
it them one ones thing things please just now then
""".split())

#: Generic words for the project as a whole. As the HEAD of the object they
#: mean the project itself ("delete the project"); as a modifier they describe
#: something inside it ("delete the project banner").
_PROJECT_NOUNS = frozenset({"project", "projects"})

#: Where the removal's own clause ends.
_CLAUSE_END_RE = re.compile(r"[,;:.!?]|\b(and|but|so|then|because)\b", re.I)

_REMOVAL_OBJECT_WINDOW = 60


def _normalise(text: str) -> str:
    return re.sub(r"[-_\s]+", " ", (text or "").strip().lower())


def _object_of(text: str, match) -> str:
    """The noun phrase the removal verb is acting on.

    Ends at a container preposition, because after "from"/"in" the project is
    the place the removal happens rather than the thing being removed. That one
    rule is what separates "remove the pause button FROM flappy-bird" from
    "remove flappy-bird".
    """
    window = text[match.end():match.end() + _REMOVAL_OBJECT_WINDOW]
    # The object also ends where the CLAUSE does. "delete with-you, I mean it"
    # names with-you; without this the object ran on into the next clause and
    # its head became "mean".
    clause = _CLAUSE_END_RE.search(window)
    if clause:
        window = window[:clause.start()]
    cut = _CONTAINER_PREP_RE.search(window)
    if cut:
        window = window[:cut.start()]
    return window.strip(" .,;:!?'\"")


def _head_and_tokens(obj: str) -> tuple[str, list[str]]:
    """The object's head noun and its content tokens.

    English puts the head of a noun phrase last: "the project banner" is a
    banner, "the flappy-bird project" is a project. Reading the head rather
    than scanning for a keyword anywhere is what stops a generic word appearing
    as a MODIFIER from claiming the whole project.
    """
    tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9'-]*", (obj or "").lower())
              if t not in _EMPTY_OBJECT]
    return (tokens[-1] if tokens else ""), tokens


def removal_object_tokens(text: str) -> list[list[str]]:
    """The content tokens of each thing a removal verb is acting on.

    `classify_removal` answers WHAT KIND of removal this is. This answers WHAT
    IT NAMES, which the caller needs for a question the classifier deliberately
    cannot answer on its own: whether the named thing could be a project other
    than the current one. The classifier is given one slug and knows nothing
    about the rest of the machine, and that is the right shape for it -- so the
    ambiguity check lives at the call site, which does know.

    Returns one token list per removal verb found, empty lists dropped.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    out: list[list[str]] = []
    for m in _REMOVAL_VERB_RE.finditer(raw):
        _, tokens = _head_and_tokens(_object_of(raw, m))
        if tokens:
            out.append(list(tokens))
    return out


def classify_removal(text: str, *, slug: str = "") -> str:
    """What kind of removal this is, if any.

    Returns one of REMOVAL_NONE / WHOLE_PROJECT / INSIDE_PROJECT / AMBIGUOUS /
    UNSUPPORTED.

    The rule that matters most is the one that is NOT here: a component of a
    hyphenated slug never identifies the whole project. "bird" is not
    flappy-bird and "tower" is not tower-defense, and treating them as the
    project pointed a permission-gated delete at something the user was asking
    to edit. Only the full identity counts, hyphens and spaces normalised, so
    "flappy bird" is still flappy-bird while "bird" is not.
    """
    raw = (text or "").strip()
    if not raw:
        return REMOVAL_NONE

    # An unsupported lifecycle verb is decided before anything else: whatever
    # its object is, Nova cannot do the thing being asked for.
    if _UNSUPPORTED_LIFECYCLE_RE.search(raw):
        return REMOVAL_UNSUPPORTED

    matches = list(_REMOVAL_VERB_RE.finditer(raw))
    if not matches:
        return REMOVAL_NONE

    identity = _normalise(slug)
    verdicts = []
    for m in matches:
        obj = _object_of(raw, m)
        head, tokens = _head_and_tokens(obj)
        flat = _normalise(obj)

        if not tokens:
            # "delete it", "remove that" - a removal with nothing to name.
            verdicts.append(REMOVAL_AMBIGUOUS)
            continue
        if identity and (flat == identity or head == identity
                         or flat.endswith(" " + identity)
                         or head == identity.replace(" ", "-")):
            verdicts.append(REMOVAL_WHOLE_PROJECT)
            continue
        if head in _PROJECT_NOUNS:
            # "delete the project", "trash this project" - the head IS the
            # project. A modifier ("the project banner") is not.
            verdicts.append(REMOVAL_WHOLE_PROJECT)
            continue
        verdicts.append(REMOVAL_INSIDE_PROJECT)

    # A sentence that asks for two different things is not a licence to pick
    # the destructive reading.
    if REMOVAL_AMBIGUOUS in verdicts:
        return REMOVAL_AMBIGUOUS
    if REMOVAL_WHOLE_PROJECT in verdicts and REMOVAL_INSIDE_PROJECT in verdicts:
        return REMOVAL_AMBIGUOUS
    return verdicts[0]


def requests_project_removal(text: str, *, slug: str = "") -> bool:
    """True only for an unambiguous whole-project removal.

    Kept as the narrow question callers usually want. Anything that needs to
    tell an ambiguous removal from a feature removal must use
    `classify_removal`, because a boolean cannot carry that difference - which
    is exactly how an ambiguous delete ended up starting an edit.
    """
    return classify_removal(text, slug=slug) == REMOVAL_WHOLE_PROJECT


def defers_a_change(text: str) -> bool:
    """Is this putting a change OFF, rather than forbidding it?

    Time-qualified, a prohibition still wants the change — later. Unqualified,
    it wants it not to happen. Only the first may become a pending proposal, and
    the qualifier has to modify the PROHIBITION rather than merely appear
    somewhere in the sentence.

    Two shapes qualify. A negated imperative with a time qualifier ("don't
    change anything yet"), and a trailing clause that qualifies the request the
    speaker just made ("..., but not yet"). The second was missing, and a
    sentence carrying it was executed immediately.
    """
    raw = (text or "").strip()
    return bool(_DEFERRED_PROHIBITION_RE.search(raw)
                or _TRAILING_DEFERRAL_RE.search(raw))


def carries_a_proposal(text: str) -> bool:
    """Does this turn say WHAT to change, or only WHEN NOT to?

    A deferral answers "not now". On its own it proposes nothing:

        "Don't change it yet."          only timing
        "Add a parallax background, but don't change anything yet."
                                        a proposal AND its timing

    THE TEST IS THE AUTHORISATION GRAMMAR, applied to what is left after the
    deferral clause is removed. Two weaker tests were tried first and both
    admitted things that are not proposals:

      "any content words remain"    "The game looks pretty good, so don't
                                    change anything yet." — measured on
                                    da27c9d, it replaced a real proposal

      "a positive action family"    `_ACTION_VERB` is deliberately broad TOPIC
                                    detection and matches inflections, so "I
                                    changed the menu yesterday, but don't
                                    change anything yet." read as a proposal.
                                    A retrospective is not a proposal.

      "a desire with an object"     "I want pizza, but don't change anything
                                    yet." — an arbitrary noun after "I want"
                                    is not a project change.

    Reusing `authorize_project_mutation` fixes all three at once and for the
    right reason: it is the module's only affirmative-instruction grammar, and
    it already vetoes retrospectives, denials, deliberation, questions and
    messages about Nova herself. A proposal is exactly a sentence that WOULD
    have authorised the change if the user had not deferred it.

    DESIRE-ONLY FORMS FAIL CLOSED. "I'd like a dark mode" is a real proposal to
    a human and indistinguishable, deterministically, from "I want pizza" — so
    neither becomes executable pending state. The explicit forms do work:
    "I'd like you to add a dark mode", "Help me add a dark mode", "Add a dark
    mode". Making every stray desire a future filesystem mutation is the wrong
    way to be wrong.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    # BOTH deferral forms come out before the grammar is applied. The
    # docstring's rule is "a sentence that WOULD have authorised the change if
    # the user had not deferred it", so any clause that does the deferring has
    # to go -- otherwise the re-check trips over the very deferral being
    # stripped and every deferred proposal reads as no proposal at all.
    rest = _DEFERRED_PROHIBITION_RE.sub(" ", raw)
    rest = _TRAILING_DEFERRAL_RE.sub(" ", rest).strip().rstrip(",;-").strip()
    if not rest:
        return False
    return bool(authorize_project_mutation(rest, complaint=False).allowed)


def withdraws_pending_change(text: str, pending: str) -> bool:
    """Does this prohibition call off the change that is actually pending?

    Tied to the proposal by what the prohibition is ABOUT. A target of only
    empty words ("don't change it") refers to whatever is on the table; a target
    naming something ("the physics") withdraws only a proposal that mentions it.
    """
    raw = (text or "").strip()
    if not raw or not (pending or "").strip():
        return False
    if defers_a_change(raw):
        return False
    m = _PROHIBITION_TARGET_RE.search(raw)
    if not m:
        return False
    target = _content_words(m.group("target"))
    if not target:
        # Anaphoric: "don't change it" names nothing, so it refers to whatever
        # is on the table and the action family cannot narrow it.
        return True
    # EVERY content word of the withdrawal has to be in the proposal, not just
    # one. Intersection let a shared container noun do the work: "Don't add a
    # settings icon to the menu." cancelled "Add a pause button to the menu."
    # on "menu" alone, and "Don't remove the reset button from the settings
    # screen." cancelled the debug-button proposal on three shared words while
    # disagreeing about the only one that mattered. Measured on da27c9d.
    #
    # Subset, not ratio: the reset-button case shares 3 of its 4 words and must
    # still not cancel. What disqualifies it is the word it does NOT share.
    if not target <= _content_words(pending):
        return False
    # The ACTION has to match as well. Sharing a noun is not withdrawal:
    # "Don't remove the menu." does not call off "Add a pause button to the
    # menu.", and cancelling on the noun alone silently dropped the proposal.
    proposed = _positive_families(pending)
    if not proposed:
        # A proposal with no action verb of its own ("I'd like a dark mode,
        # but don't change it yet") gives nothing to compare, so the target
        # match stands on its own rather than blocking every withdrawal.
        return True
    return _family_of(m.group("verb")) in proposed


def cancels_pending_change(text: str, pending: str = "") -> bool:
    """Does this message call OFF a change that was proposed but not approved?

    Three shapes, narrowest first:

      1. an explicit withdrawal      "never mind", "cancel that", "actually don't"
      2. a GENERIC prohibition       "don't change anything" — refers to whatever
                                     is on the table, so `pending` is not needed
      3. a SPECIFIC prohibition      "don't change the physics" — withdraws only
                                     a proposal it actually refers to

    `pending` is what makes (3) possible. Without it the choice was between
    cancelling whatever happened to be pending — so an unrelated prohibition
    erased a dark-mode proposal — and cancelling nothing, which left a withdrawn
    change pending for a later "Go ahead." to execute. Both were wrong.

    A correction ("keep the horizontal spacing, I meant the vertical opening")
    contains a refusal too and must NOT read as a cancellation: it replaces the
    proposal rather than withdrawing it.
    """
    raw = (text or "").strip()
    if _CANCEL_RE.search(raw):
        return True
    if _GENERIC_PROHIBITION_RE.search(raw) and not defers_a_change(raw):
        return True
    return withdraws_pending_change(raw, pending)


def is_bare_approval(text: str) -> bool:
    """Is this an approval that names no action?

    Reports the shape only. It is NOT authority on its own and no mutation may
    be performed on the strength of it alone — the caller has to match it
    against a real pending proposal for the project currently in play.
    """
    return bool(_BARE_APPROVAL_RE.match((text or "").strip()))


def describes_a_change(text: str) -> bool:
    """Does this message talk about changing something, in any inflection?

    Topic, not grammar — "improving", "changed", "a fix" all count. Used to
    decide whether a refused message was a PLAN worth remembering, as opposed
    to small talk that merely happened near a project.
    """
    return bool(_ACTION_VERB_RE.search((text or "").strip()))


#: Words that make "<word> project" generic rather than a name. "the project"
#: means whatever we are on; "the calculator project" names a specific one.
_GENERIC_PROJECT_QUALIFIER = (
    r"the|this|that|these|those|our|my|your|his|her|their|its|a|an|same|"
    r"whole|entire|other|another|current|active|new|old|last|next|first|"
    r"second|good|bad|big|small|little|main|only|what|which|whose|some|"
    r"any|every|no|one|each|both"
)

_QUALIFIED_PROJECT_RE = re.compile(
    r"\b(?!(?:" + _GENERIC_PROJECT_QUALIFIER + r")\s+projects?\b)"
    r"([A-Za-z][\w-]*)\s+projects?\b",
    re.IGNORECASE,
)


def qualified_project_name(text: str) -> str | None:
    """The NAME in "the calculator project", or None for a bare "the project".

    This exists because of a measured fall-through. When a message named no
    RESOLVABLE project, the turn path substituted the last-active project as
    the target — which is right for "continue where we left off" and badly
    wrong for a message that names a different project by name. With
    flappy-bird open:

        "Let's work on the calculator project."
            -> started an autonomous improve OF FLAPPY-BIRD

    The user named one project and a different one got edited. Telling the two
    cases apart is exactly the question this answers: "the project" is generic
    and may borrow the current one, "the calculator project" may not.

    Only the single word before "project" is captured, so "the tower defense
    project" reports "defense". That is a wording limitation and not a
    correctness one: the caller has already failed to resolve any known project
    from the WHOLE message, and a real multi-word project resolves there
    ("Flappy Bird" -> flappy-bird) long before this is consulted.
    """
    m = _QUALIFIED_PROJECT_RE.search((text or "").strip())
    return m.group(1) if m else None

