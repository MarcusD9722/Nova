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

__all__ = ["MutationVerdict", "authorize_project_mutation"]


#: Verbs that would actually change a project.
_ACTION_VERB = (
    r"(?:improv\w*|enhanc\w*|upgrad\w*|polish\w*|refactor\w*|fix\w*|extend\w*|"
    r"add\w*|implement\w*|appl(?:y|ies|ied)|chang\w*|updat\w*|rewrit\w*|"
    r"build\w*|creat\w*|mak\w*|continu\w*|resum\w*|finish\w*|complet\w*|"
    r"remov\w*|delet\w*|set\s+up|wire\s+up|hook\s+up|work\s+on|"
    r"keep\s+(?:working|going))"
)
_ACTION_VERB_RE = re.compile(r"\b" + _ACTION_VERB + r"\b", re.IGNORECASE)

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
    r"^\s*(?:please\s+|now\s+|then\s+|also\s+|and\s+)*" + _ACTION_VERB + r"\b",
    re.IGNORECASE,
)

#: "Can you fix the collision bug", "let's add a restart button", "I want you
#: to refactor it" — a request rather than a bare imperative.
_REQUEST_RE = re.compile(
    r"^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?" + _ACTION_VERB + r"\b"
    r"|^\s*(?:let'?s|lets)\s+(?:please\s+)?" + _ACTION_VERB + r"\b"
    r"|^\s*i\s+(?:want|need|would\s+like|'?d\s+like)\s+you\s+to\s+" + _ACTION_VERB + r"\b",
    re.IGNORECASE,
)

#: "Go ahead and apply those improvements." / "Yes, implement it." — approval
#: of something already proposed. Requires an action verb as well, so a bare
#: "yes" never authorises anything on its own.
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
    if _REQUEST_RE.match(core):
        return MutationVerdict(True, "affirmative: direct request")
    if _AFFIRMATIVE_LEAD_RE.match(core) and _ACTION_VERB_RE.search(core):
        return MutationVerdict(True, "affirmative: approval of a proposed action")
    if complaint:
        return MutationVerdict(True, "affirmative: complaint about work in flight")

    # Nothing here says "do it". Fail closed: talk about it instead.
    return MutationVerdict(False, "no affirmative instruction")
