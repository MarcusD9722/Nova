from __future__ import annotations

"""Shared intent classification for Nova's routing decisions.

One place to answer "is this a question?" — previously every subsystem invented
its own rule with different behavior:
  - core/project_builder.py QUESTION_LEAD_RE: anchored to the literal first word
  - the old ChatDecider's _TASK_REQUEST_RE/_NON_TASK_CHAT_RE: anchored at 0
    (that module was deleted once it became unreachable; the lesson is why
     this file exists, so it is kept here rather than the dangling path)

That anchoring is exactly how "I meant What other improvements can we make to
the flappy-bird game?" got treated as a work request instead of a question: the
"I meant" preamble hid the "What", so Nova went and edited code instead of
answering. This module strips conversational preamble first, and doesn't require
a literal "?" for WH-questions.
"""

import re

__all__ = ["is_question", "strip_preamble", "is_purely_conversational"]

# Conversational lead-ins that hide a sentence's real shape. Applied repeatedly
# so "So, Nova, I meant what..." reduces to "what...".
_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"i\s+(?:meant|mean|said|was\s+asking)|"
    r"so|well|actually|wait|also|and|but|ok|okay|hmm|um|uh|"
    r"hey\s+nova|hi\s+nova|nova|hey|hi|"
    r"please|just|quick\s+question|question"
    r")\b[\s,:—-]*",
    re.IGNORECASE,
)

# WH-words essentially never open an imperative in natural English, so they
# signal a question on their own — no "?" needed (people drop it constantly).
_WH_LEAD_RE = re.compile(
    r"^\s*(?:what|why|how|which|who|whom|whose|where|when)\b",
    re.IGNORECASE,
)

# Polar auxiliaries CAN open a real request ("Can you build me a game"), so
# these only count as a question when the sentence actually ends in "?".
_POLAR_LEAD_RE = re.compile(
    r"^\s*(?:is|are|was|were|do|does|did|can|could|would|will|shall|should|have|has|had|am)\b",
    re.IGNORECASE,
)

# "any ideas?", "thoughts?", "right?" — trailing-? with no imperative verb.
_IMPERATIVE_LEAD_RE = re.compile(
    r"^\s*(?:make|create|build|code|write|develop|start|begin|add|set\s+up|put|wire|hook|"
    r"fix|improve|change|update|remove|delete|give|show\s+me|open|run|try)\b",
    re.IGNORECASE,
)


def strip_preamble(text: str) -> str:
    """Remove conversational lead-ins so the sentence's real shape is visible."""
    t = (text or "").strip()
    for _ in range(6):  # bounded; handles stacked openers like "So, Nova, I meant ..."
        new = _PREAMBLE_RE.sub("", t, count=1).strip()
        if new == t or not new:
            break
        t = new
    return t or (text or "").strip()


# ── "Could any tool possibly help with this?" ───────────────────────────────
# Every chat turn spends a full 900-token *thinking* generation on the agent
# loop's "do I need a tool?" decision — including for "good morning", where the
# answer can only be no. On one 9B that is several seconds of the 30-60s Marcus
# was waiting.
#
# The direction of risk matters here. Wrongly skipping the decider would cost
# Nova a capability silently (she just wouldn't reach for a tool); wrongly
# running it only costs the time we already spend. So this is an ALLOWLIST of
# shapes that are unambiguously social, and anything unrecognised keeps the
# full tool loop. It is meant to stay narrow.

_CONVERSATIONAL_RE = re.compile(
    r"^\s*(?:"
    r"(?:hi|hey|hello|yo|sup|howdy|morning|afternoon|evening)\b"
    r"|good\s+(?:morning|afternoon|evening|night)\b"
    r"|(?:good\s?)?night\b|goodbye\b|bye\b|see\s+(?:you|ya)\b|talk\s+(?:to\s+you\s+)?later\b"
    r"|thanks?\b|thank\s+you\b|ty\b|appreciate\s+(?:it|that)\b"
    r"|(?:ok|okay|kk|cool|nice|sweet|awesome|great|perfect|sounds\s+good|"
    r"got\s+it|gotcha|sure|yeah|yep|yup|nope|nah|alright|fine)\b"
    r"|(?:lol|lmao|haha+|hehe)\b"
    r"|how\s+(?:are|r)\s+(?:you|u)\b|how'?s\s+it\s+going\b|how\s+have\s+you\s+been\b"
    r"|what'?s\s+up\b|wassup\b"
    r"|i'?m\s+(?:good|fine|ok|okay|tired|exhausted|beat|happy|sad|hungry|back|home)\b"
    r"|(?:long|rough|good|great|tough)\s+day\b"
    r"|love\s+(?:you|ya)\b|miss\s+(?:you|ya)\b"
    r")",
    re.IGNORECASE,
)

# Any hint that live data, a file, a person's record, or an action is involved
# vetoes the fast path even if the message opens conversationally
# ("hey, what's the weather" must still reach the tool loop).
_TOOL_SIGNAL_RE = re.compile(
    r"\b(?:weather|temperature|forecast|rain|snow|time|date|today|tomorrow|tonight|"
    r"remind|reminder|calendar|schedule|meeting|appointment|email|mail|inbox|"
    r"search|google|look\s+up|find|lookup|news|price|stock|score|"
    r"map|maps|directions?|route|navigate|drive|nearest|closest|address|"
    r"project|build|code|program|script|app|game|file|folder|document|read|write|"
    r"remember|recall|forget|memory|note|goal|task|todo|"
    r"discord|message|send|post|play|open|run|execute|install|download|"
    r"image|picture|photo|screen|camera|generate|draw|"
    # Trouble words: "I'm tired of this bug in main.py, can you look at it"
    # opens exactly like smalltalk and is unmistakably work.
    r"bug|fix|broken|crash|error|failing|debug|look\s+at|check\s+on|"
    r"who|what|where|when|why|how\s+(?:do|much|many|far|long)|which"
    r")\b",
    re.IGNORECASE,
)

#: A filename is always about work, whatever the sentence around it looks like.
_FILENAME_RE = re.compile(
    r"\b[\w\-]+\.(?:py|js|jsx|ts|tsx|json|md|txt|html|css|yaml|yml|sh|ps1|toml|ini|csv|log)\b",
    re.IGNORECASE,
)

#: Whole-message social phrases that happen to contain a veto word. "what's
#: up" is a greeting, not a question about anything. Anchored to the ENTIRE
#: message, so "what's up with the printer" is untouched and still gets tools.
_SOCIAL_OVERRIDE_RE = re.compile(
    r"^(?:"
    r"what'?s\s+up|whats\s+up|what\s+is\s+up|what'?s\s+new|what'?s\s+good|"
    r"how\s+are\s+(?:you|ya|u)(?:\s+doing)?|how'?s\s+it\s+going|how\s+have\s+you\s+been|"
    r"how\s+was\s+your\s+day|what\s+are\s+you\s+up\s+to"
    r")[\s.,!?]*$",
    re.IGNORECASE,
)

#: Longer than this and it is not the kind of throwaway social line this
#: fast path is for, whatever it opens with.
_MAX_CONVERSATIONAL_WORDS = 12


# ── What the work is called ─────────────────────────────────────────────────
#
# The subject-and-outcome shapes: "did THE BUILD finish?", "did MY GAME BUILD
# work?", "has THE JOB run?", "are THE TESTS passing?".
#
# Measured before these existed (2026-11-07): 43 of 51 natural ways of asking
# whether the work went right matched nothing. Every shape in
# `_WORK_STATUS_RE` took a pronoun or "everything" as its subject, and none
# took the work's own name. A miss means the reply is written with no record in
# front of the model, which is the condition behind the audit's C4 false claim.
#
# A false match is not free either, so these stay closed shapes rather than a
# keyword sniff. A matched turn carries the record, and a reply that claims
# completion is then corrected against it: "did the laundry finish?" answered
# "it's all done!" would be told that a project is failing.

_WORK_DET = (r"(?:the|my|your|our|that|this|those|these|"
             r"all\s+(?:of\s+)?(?:the|my|your|our))\s+")

#: Up to two words before the noun, because they are usually the project or the
#: feature ("the flappy-bird build", "the pause menu changes"), and no list
#: could name those.
_ANY_MODIFIERS = r"(?:[\w'’-]+\s+){0,2}"

#: Said to Nova, these are her work whatever modifies them.
_OPEN_WORK = (_ANY_MODIFIERS + r"(?:builds?|compil(?:e|es|ation)|"
              r"deploy(?:s|ments?)?|install(?:s|ations?)?|scripts?|apps?|"
              r"changes?|edits?|fix(?:es)?|improvements?|updates?)")
#: These are as often somebody else's -- the road work, the promo code, the
#: school project, her new job -- so they take only modifiers that keep them
#: Nova's.
_THE_WORK = (r"(?:(?:planned|remaining|pending|scheduled|background|whole|"
             r"project|code|build|test)\s+)?work")
_THE_CODE = r"(?:(?:new|game|project|test|build|updated|fixed)\s+)?code"
_THE_PROJECT = (r"(?:(?:new|current|whole|game|coding|code|software|test)\s+)?"
                r"projects?")
_THE_TASKS = (r"(?:(?:background|scheduled|queued|pending|remaining|next|last|"
              r"first|planned|build|test)\s+)?(?:tasks?|steps?)")
_THE_JOBS = r"(?:(?:background|scheduled|queued|build|test|nightly)\s+)?jobs?"
#: These also name things that are not software -- a blood test, the Chiefs
#: game, a TV programme -- so they take only the verbs only software takes.
#: "Did the tests pass?" is about the work; "is the test done?" is not decided
#: to be.
_THE_TESTS = _ANY_MODIFIERS + r"(?:tests?|test\s+(?:suite|run)s?)"
_THE_SOFTWARE = _ANY_MODIFIERS + r"(?:games?|programs?)"

#: How the work went, after "did", after "has/have/had", after "is/are/was".
_DID_ANY = (r"(?:finish|complete|succeed|pass|work|run|build|compile|fail|"
            r"break|crash|go\s+(?:through|ok(?:ay)?|alright|right|wrong|well|"
            r"smoothly))")
_HAS_ANY = (r"(?:been\s+)?(?:finished|completed|succeeded|passed|worked|run|"
            r"ran|built|compiled|failed|broken|crashed|gone\s+through|done)")
_IS_ANY = (r"(?:done|finished|complete|completed|working|passing|failing|"
           r"broken|built|compiled|ready|successful|running|in\s+progress)")

#: (subject, after "did", after "has", after "is")
_WORK_SUBJECTS = (
    (_OPEN_WORK, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_WORK, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_CODE, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_PROJECT, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_TASKS, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_JOBS, _DID_ANY, _HAS_ANY, _IS_ANY),
    (_THE_TESTS, r"(?:pass|fail|run)", r"(?:been\s+)?(?:passed|failed|run|ran)",
     r"(?:passing|failing|passed|failed|running|green|red)"),
    (_THE_SOFTWARE, r"(?:build|compile|crash)",
     r"(?:been\s+)?(?:built|compiled|crashed)",
     r"(?:built|compiling|crashing|working\s+(?:now|yet|again))"),
)

_OUTCOME_ADVERB = (r"(?:(?:all|actually|really|even|ever|finally|successfully|"
                   r"properly|already|still|now|currently)\s+)?")


def _outcome_shapes() -> str:
    """Every "did/has/is + the work + how it went" shape, as one alternation."""
    shapes = []
    for subject, did, has, is_ in _WORK_SUBJECTS:
        for aux, verb in ((r"did", did), (r"(?:has|have|had)", has),
                          (r"(?:is|are|was|were)", is_)):
            shapes.append(aux + r"\s+" + _WORK_DET + subject + r"\s+"
                          + _OUTCOME_ADVERB + verb)
    # "Does it work now?" -- only with now/yet/again/properly, because "does
    # that work for you?" is agreement, not a question about the work.
    nouns = "|".join(subject for subject, *_ in _WORK_SUBJECTS)
    shapes.append(r"(?:does|do)\s+(?:(?:it|that|this|they)\s+|" + _WORK_DET
                  + r"(?:" + nouns + r")\s+)" + _OUTCOME_ADVERB
                  + r"work\s+(?:now|yet|again|properly|correctly)")
    return "|".join(shapes)


_WORK_OUTCOME_RE = re.compile(r"\b(?:" + _outcome_shapes() + r")\b",
                              re.IGNORECASE)

#: "How did the tests go?" Not jobs, games or a single test: "how did the job
#: go?" and "how did the test go?" are as often about somebody's day.
_HOW_SUBJECTS = ("(?:" + "|".join((_OPEN_WORK, _THE_WORK, _THE_CODE,
                                   _THE_PROJECT, _THE_TASKS))
                 + r"|" + _ANY_MODIFIERS + r"(?:tests|test\s+(?:suite|run)s?))")

#: A claim or question whose outcome belongs to something else is not about
#: the work: "everything worked out WITH THE DENTIST", "it all went through AT
#: THE BANK", "they finished THEIR HOMEWORK". A complement that names the work
#: still counts ("with the new build"), and so does one with no determiner at
#: all ("with Chrome").
_ANY_WORK_NP = "(?:" + "|".join(subject for subject, *_ in _WORK_SUBJECTS) + ")"


def _elsewhere(prepositions: str) -> str:
    """A lookahead refusing an outcome that belongs to something else."""
    return (r"(?!\s+(?:out\s+)?(?:"
            # a place or a person: "with the dentist", "at school"
            r"(?:" + prepositions + r")\s+(?:"
            r"(?:the|my|your|his|her|their|our|a|an)\s+(?!" + _ANY_WORK_NP
            + r"\b)|(?:school|work|home|church|the\s+gym|the\s+office)\b)"
            # an object that belongs to somebody: "they finished THEIR
            # HOMEWORK", "they finished MY CAR". After the vague subjects this
            # is applied to, a possessive object is never the work itself:
            # nobody says "everything finished my build".
            r"|(?:his|her|their|my|your|our)\s+"
            r"))")


#: For claims and "did everything...?": with/at only, because "it all worked
#: out IN THE END" is still about the work.
_ELSEWHERE = _elsewhere("with|at")
#: For "did anything break?" and "any errors?", where in/on name a place too:
#: "did something break IN THE KITCHEN?", "any errors ON MY BILL?".
_ELSEWHERE_ANYWHERE = _elsewhere("with|at|in|on")


#: Questions whose only honest source is the durable record, not the
#: conversation. "What failed?" after a restart has no answer in a transcript
#: that begins after the restart, and answering it from what Nova happens to
#: remember saying is how a confident wrong answer gets produced.
_WORK_STATUS_RE = re.compile(
    r"\b("
    r"what happened|what went wrong|what failed|what broke|"
    r"what(?:'s| is| was)?\s+(?:still\s+)?(?:pending|outstanding|left|remaining)|"
    r"what(?:'s| is| was)?\s+(?:been\s+)?(?:cancelled|canceled)|"
    r"what can (?:be )?resume|what(?:'s| is)? resumable|can (?:we|you) resume|"
    r"what should happen next|what(?:'s| is) next|where (?:are|were) we|"
    r"what(?:'s| is| are) (?:you|nova) working on|"
    r"what(?:'s| is) the status|how(?:'s| is) it going with|"
    # "Did it build?" and "did that pass?" are the same question as "did it
    # work?" (widened 2026-11-07, with the subject-and-outcome shapes).
    r"did (?:it|that) (?:all |actually |really |even )?"
    r"(?:work|finish|succeed|run|build|compile|pass|fail|crash)|"
    # "Is anything still running?" - the first thing a person asks on
    # coming back to a machine that restarted, and a question whose only
    # honest source is the record: the transcript begins AFTER the restart.
    # A subject is required, so "is the tap still running" is not this.
    r"(?:is|are) (?:anything|it|they|those|the (?:tasks?|steps?|jobs?|work|build))"
    r"(?: still| currently)? (?:running|going|in progress|under ?way)|"
    r"anything (?:still |currently )?(?:running|in progress|under ?way)|"
    # The same question aimed at Nova rather than at the queue.
    # "working on" takes a THING here, not a time: "are you working on
    # Sunday?" is a question about a diary, not about a queue.
    r"(?:are you|is nova) (?:still |currently )?working on"
    r"(?! (?:sun|mon|tues|wednes|thurs|fri|satur)day\b| (?:the )?weekend| holidays?| tonight| tomorrow| later)|"
    # Only with a WORK noun, so it does not swallow "what's going on
    # with your day".
    r"what(?:'s| is) going on with (?:the|my|your|our) "
    r"(?:work|tasks?|steps?|jobs?|build|project|goals?)|"
    # COMPLETION questions about a project. Measured before adding
    # these: five of nine natural phrasings attached no record at all,
    # so "did everything pass?" was answered from an empty prompt while
    # a criterion sat FAILING with its error on disk.
    r"did (?:you|nova) (?:finish|complete) (?:it|that|this|them|everything|all of (?:it|them)|the (?:project|build|work|game|app|script|code))|"
    r"did (?:they all|all of them) pass|"
    # "Did everything work?" Not with "they all", which is as often people.
    r"did (?:everything|it all|all of it) (?:actually |really |even )?"
    r"(?:pass|work|finish|succeed|complete|build|compile|run|go through)"
    + _ELSEWHERE + r"|"
    # "Did anything fail?" -- the survey form of "what failed?".
    r"did (?:anything|something) (?:else )?(?:fail|break|crash|go wrong)"
    + _ELSEWHERE_ANYWHERE + r"|"
    r"(?:any|were there any|are there any|was there an?|"
    r"did (?:it|that|you) (?:get|hit|throw|see|run into) any) "
    r"(?:errors?|exceptions?|failures?)" + _ELSEWHERE_ANYWHERE + r"|"
    r"(?:is|are) (?:it|that|this|they|the (?:project|build|work)) (?:all )?done|"
    r"(?:which|what) (?:requirement|criteri(?:on|a)|check|test)s? "
    r"(?:is|are) (?:failing|outstanding|left|still)|"
    r"can i (?:use|try|run|ship) (?:it|this|that) (?:now|yet)|"
    r"(?:is|are) (?:it|they) ready (?:now|yet|to use)|"
    r"(?:any|are there any|there aren.t any) (?:failures?|failing|broken) "
    r"(?:left|remaining|still)|"
    r"what still needs (?:doing|work|verif\w*|check\w*|prov\w*)|"
    r"(?:is|are) (?:it|that) (?:complete|finished)|"
    # "looks like we're done here" / "you finished all of it" - claims
    # about the work in the second person, which the premise detector
    # misses because it requires the SCOPE word before the outcome word
    # and these put it after. Restricted to we/you so "I am done with my
    # coffee" stays a sentence about coffee.
    r"(?:we|you)(?:'re| are| have|'ve)? (?:all )?done\b|"
    r"(?:we|you) (?:have |'ve )?finished (?:all of )?(?:it|them|everything)|"
    # How people actually ask, measured on real weights. None of the shapes
    # above matched "How's that project coming along?", so NO record was
    # attached and the reply came out of the model's head: "It's all set up and
    # ready to go... plus the tests are written too", about a build that had
    # just failed and written nothing (audit 2026-11-02, C4).
    #
    # Attaching the record to one extra turn costs tokens, and sometimes a
    # wrong correction (see `_elsewhere`). Not attaching it costs the truth.
    r"coming along|how(?:'s| is|’s) (?:it|that|the|this) .{0,24}?going|"
    r"how(?:'s| is|’s) (?:it|that) coming|how far along|"
    r"any (?:progress|news|update|updates)|what(?:'s| is) the latest|"
    r"what (?:did|have) you (?:actually )?(?:verif\w+|check\w+|test\w+|prov\w+)|"
    r"how did (?:it|that|" + _WORK_DET + _HOW_SUBJECTS + r") go|"
    r"(?:is|was) (?:it|that) successful"
    r")\b", re.IGNORECASE)


#: A CLAIM about the work rather than a question about it: "so everything
#: finished, right?", "that all went through, yes?", "it's all done then".
#:
#: These are the most dangerous phrasing of all and were the ones missed. A
#: question invites Nova to look something up; a premise invites her to agree.
#: With no durable state in front of the model, agreeing is exactly what
#: happens - and agreeing that everything succeeded when a step failed is a
#: worse answer than any amount of vagueness.
#:
#: Two parts, both required: something that scopes the WORK (everything, it,
#: that, the tasks) and something that asserts an OUTCOME (finished, worked,
#: done, went through, succeeded). "Everything is fine" is not a claim about
#: task outcomes; "everything finished" is.
_WORK_PREMISE_RE = re.compile(
    r"\b(everything|it|that(?!\s+\w+\s+(?:was|were|is|are)\b)|those|they|the (?:tasks?|steps?|work|jobs?))\b"
    r"[^.?!]{0,40}?"
    r"\b(finish(?:ed)?|worked|work out|done|complete[ds]?|went through|"
    r"succeed(?:ed)?|ran ok(?:ay)?|all set|sorted)\b"
    # ...unless the outcome belongs somewhere else. "Everything worked out
    # with the dentist." matched until 2026-11-07, so a reply agreeing that
    # it was all good was then corrected against a failing project.
    + _ELSEWHERE,
    re.IGNORECASE)


#: The same claim with the work's own name as its subject: "so the build
#: passed?", "my game build worked, right?", "the changes went through?".
#:
#: Only verbs and adverbs may stand between the two, so "the changes to my
#: schedule are done" is not read as one. Updates, apps and installs are left
#: out on purpose: "the Windows update finally finished" is said to Nova about
#: the computer, and agreeing with it must not be corrected against a project.
#: Tests take only "passed", because "the tests are done" is as often a
#: hospital's.
_WORK_NOUN_PREMISE_RE = re.compile(
    r"\b" + _WORK_DET + r"(?:"
    + _ANY_MODIFIERS + r"(?:builds?|compil(?:e|es|ation)|deploy(?:s|ments?)?|"
    r"scripts?|changes?|edits?|fix(?:es)?|improvements?)"
    + "|" + "|".join((_THE_WORK, _THE_CODE, _THE_PROJECT, _THE_TASKS, _THE_JOBS))
    + r")\s+(?:(?:is|are|was|were|has|have|had|all|been|now|finally|actually|"
    r"really|already|successfully|properly|completely|fully)\s+){0,3}"
    r"(?:finish(?:ed)?|worked|work(?:ed)?\s+out|done|complete[ds]?|"
    r"went\s+through|gone\s+through|succeed(?:ed)?|ran\s+(?:ok(?:ay)?|fine)|"
    r"all\s+set|sorted|built|compiled|passed|ready|works|working)\b"
    r"|\b" + _WORK_DET + _THE_TESTS
    + r"\s+(?:(?:have|all|have\s+all|all\s+have)\s+)?passed\b",
    re.IGNORECASE)


#: Words that ask whether something is FINISHED, as opposed to asking about it
#: at all. On their own these decide nothing — "is the kettle ready?" and "is
#: the calculator ready?" are the same sentence — so they are only consulted
#: alongside a name that is known to BE a project. See
#: `RuntimeManager._completion_context`: the resolver knows which nouns are
#: projects, and no amount of regex ever will.
_COMPLETION_WORDS = re.compile(
    r"\b(done|finished|complete[ds]?|completion|ready|working|passing|passed|"
    r"failing|failed|broken|left|outstanding|remaining|usable|ship(?:pable)?)\b",
    re.IGNORECASE)


def mentions_completion(text: str) -> bool:
    """Whether this turn contains a finishedness word at all.

    Deliberately loose, and deliberately never used alone.
    """
    return bool(_COMPLETION_WORDS.search(text or ""))


#: Words that can only be POINTING at something already in play. Their presence
#: is what separates "is it done?" from "how is the work going?" — the first is
#: a question about one thing and has a wrong answer if it describes a different
#: project; the second is a question about the work and has a wrong answer if it
#: leaves a failing project out.
_REFERENTIAL_RE = re.compile(
    r"\b(it|its|it's|that|this|they|them|those|these)\b", re.IGNORECASE)


def refers_to_one_thing(text: str) -> bool:
    """Is this turn pointing at a particular thing rather than surveying?

    Used only to decide the SCOPE of the completion record attached to an
    answer. Stage 14 established that "is it done?" must never be answered
    about a project the person did not mean — including when the current
    project no longer exists, where the honest answer is about nothing at all.
    Stage 15 found the other half: "how is the work going?" answered about one
    project omitted a different project that was failing.

    Both are true, and they are different questions. A pronoun is the cheapest
    honest signal that a question has one subject already in play; nothing here
    tries to work out WHICH thing it points at, only that it points.
    """
    return bool(_REFERENTIAL_RE.search(text or ""))


def asks_about_work(text: str) -> bool:
    """Is this turn about the state of Nova's own work?

    Deliberately closed shapes rather than a keyword sniff: this decides only
    whether to ATTACH authoritative state to the answer. Missing one leaves the
    model answering from memory, which after a restart is nothing at all.
    Attaching it to an unrelated turn is worse than prompt bloat: a reply that
    agrees the unrelated thing is done is then corrected against a project.

    Both a QUESTION about the work and a CLAIM about it count. The claim is the
    case that matters most: "so everything finished successfully, right?" asks
    Nova to agree, and she can only decline from the record.

    Either may name the work by a pronoun ("did it pass?") or by what it is
    ("did the build pass?"). The second was missing until 2026-11-07.
    """
    raw = text or ""
    return bool(_WORK_STATUS_RE.search(raw) or _WORK_OUTCOME_RE.search(raw)
                or _WORK_PREMISE_RE.search(raw)
                or _WORK_NOUN_PREMISE_RE.search(raw))


def is_purely_conversational(text: str) -> bool:
    """True only when NO registered tool could plausibly help.

    Conservative by construction: returns False for anything it does not
    positively recognise, so the caller keeps its normal tool-using path.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if _SOCIAL_OVERRIDE_RE.match(raw):
        return True

    core = strip_preamble(raw)
    if len(core.split()) > _MAX_CONVERSATIONAL_WORDS:
        return False
    # Veto on the RAW message, not just the stripped core — a tool word in the
    # preamble counts.
    if _TOOL_SIGNAL_RE.search(raw) or _FILENAME_RE.search(raw):
        return False
    # Match either form: strip_preamble removes "hey"/"so"/"ok", which are
    # themselves the greeting in "hey there, I was thinking about you".
    return bool(_CONVERSATIONAL_RE.match(raw) or _CONVERSATIONAL_RE.match(core))


def is_question(text: str) -> bool:
    """True when the message is ASKING something rather than instructing.

    Robust to preamble ("I meant ...", "So ...", "Nova, ...") and to a missing
    question mark on WH-questions.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    core = strip_preamble(raw)
    ends_q = core.rstrip().endswith("?") or raw.rstrip().endswith("?")

    # "What/why/how ..." — a question with or without punctuation.
    if _WH_LEAD_RE.match(core):
        return True
    # "Can you ...?" / "Should we ...?" — only with the question mark, since
    # "Can you build me X" is a genuine request.
    if ends_q and _POLAR_LEAD_RE.match(core):
        return True
    # Ends in "?" and doesn't open with an imperative verb -> treat as asking.
    if ends_q and not _IMPERATIVE_LEAD_RE.match(core):
        return True
    return False
