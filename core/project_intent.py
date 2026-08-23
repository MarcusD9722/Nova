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
    "qualified_project_name",
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

_VETOES = (
    ("prohibition", _PROHIBITION_RE),
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
    r"|^\s*i\s+(?:want|need|would\s+like|'?d\s+like)\s+you\s+to\s+" + _IMPERATIVE_VERB + r"\b",
    re.IGNORECASE,
)

#: Verbs that open a project without changing it. On their own they authorise
#: nothing, which is why they are not action verbs.
_OPENER_VERB = (
    r"(?:open|look\s+at|go\s+to|pull\s+up|bring\s+up|check|read|review|"
    r"inspect|load)"
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
    r"^\s*(?:please\s+|now\s+|then\s+|also\s+)*" + _OPENER_VERB + r"\b"
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
    r"^\s*(?:ok(?:ay)?[,.]?\s+|alright[,.]?\s+|right[,.]?\s+|so[,.]?\s+|"
    r"now[,.]?\s+|anyway[,.]?\s+)*"
    r"(?:"
    r"(?:let'?s|lets|let\s+us)\s+(?:please\s+)?(?:work\s+on|look\s+at|"
    r"switch\s+to|go\s+back\s+to|return\s+to|jump\s+(?:back\s+)?(?:in)?to)"
    r"|switch(?:\s+over)?\s+to"
    r"|(?:go|come|head|jump)\s+back\s+(?:to|into)"
    r"|back\s+to"
    r"|(?:go|jump)\s+(?:in)?to"
    r"|(?:open|pull\s+up|bring\s+up|load)"
    r")\b",
    re.IGNORECASE,
)

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

