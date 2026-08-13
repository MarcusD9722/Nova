from __future__ import annotations

"""Echo suppression: telling Nova's own voice apart from Marcus's.

Nova speaks through the speakers; the microphone hears it. Without filtering,
every reply becomes a new user message and the assistant talks to itself. But
over-filtering is worse than not filtering at all — dropping a real interruption
makes Nova feel deaf, which is precisely the failure barge-in exists to fix.

Three outcomes, not two:

  ECHO    the transcript is Nova's own speech coming back
  USER    genuine speech from Marcus
  MIXED   the mic caught the tail of Nova's sentence *and* Marcus talking over
          it — the interesting case, because the useful half is recoverable

The mixed case is the one that makes barge-in feel natural:

    Nova says     "The RTX 5090 has thirty-two gigabytes of memory"
    Mic hears     "...has thirty-two gigabytes, but what about the 5080?"
    Recovered     "but what about the 5080?"

Throwing that whole utterance away as echo would cost Marcus his question.

Every threshold here is biased toward USER. When the evidence is ambiguous the
verdict is genuine speech, because a false ECHO silently discards what someone
said, while a false USER merely produces one confused reply.
"""

import difflib
import re
from dataclasses import dataclass

ECHO = "echo"
USER = "user"
MIXED = "mixed"

_TOKEN = re.compile(r"[a-z0-9']+")

#: Fraction of the transcript that must be accounted for by Nova's own speech
#: before the whole thing is called echo.
_ECHO_COVERAGE = 0.75

#: A salvaged remainder shorter than this is not a question, it is noise.
_MIN_SALVAGE_TOKENS = 2

#: Below this many matched tokens, an apparent overlap is more likely a common
#: phrase ("what about the") than a real echo.
_MIN_ECHO_TOKENS = 3


@dataclass
class EchoVerdict:
    kind: str                 # ECHO | USER | MIXED
    text: str                 # what should be treated as the user's words
    matched_tokens: int = 0
    total_tokens: int = 0
    confidence: float = 0.0
    reason: str = ""

    @property
    def is_user_speech(self) -> bool:
        return self.kind in (USER, MIXED)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Normalised tokens with their offsets in the original string."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer((text or "").lower())]


def _similar(a: str, b: str) -> bool:
    """Token equality, tolerant of the small errors STT actually makes."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.82


def _longest_prefix_match(transcript: list[str], spoken: list[str]) -> int:
    """How many leading transcript tokens appear, in order, inside `spoken`.

    Tries every alignment and allows a bounded number of skips on the spoken
    side, because the microphone loses words that the speaker actually said.
    Returns the best matched prefix length.
    """
    if not transcript or not spoken:
        return 0

    best = 0
    for start in range(len(spoken)):
        if not _similar(transcript[0], spoken[start]):
            continue
        t = 0
        s = start
        misses = 0
        while t < len(transcript) and s < len(spoken):
            if _similar(transcript[t], spoken[s]):
                t += 1
                s += 1
                continue
            # Allow the spoken side to run ahead by a word or two (dropped audio)
            # before giving up on this alignment.
            if misses < 2 and s + 1 < len(spoken) and _similar(transcript[t], spoken[s + 1]):
                s += 2
                t += 1
                misses += 1
                continue
            break
        best = max(best, t)
    return best


def classify(transcript: str, spoken_recently: str) -> EchoVerdict:
    """Decide whether `transcript` is Nova's voice, Marcus's, or both."""
    text = (transcript or "").strip()
    if not text:
        return EchoVerdict(kind=USER, text="", reason="empty")

    t_tokens = _tokens(text)
    s_tokens = [tok for tok, _, _ in _tokens(spoken_recently)]

    if not s_tokens:
        return EchoVerdict(kind=USER, text=text, total_tokens=len(t_tokens),
                           reason="nothing was spoken recently")

    words = [tok for tok, _, _ in t_tokens]
    matched = _longest_prefix_match(words, s_tokens)
    total = len(words)
    coverage = matched / total if total else 0.0

    if matched < _MIN_ECHO_TOKENS:
        return EchoVerdict(kind=USER, text=text, matched_tokens=matched, total_tokens=total,
                           confidence=1.0 - coverage,
                           reason="overlap too short to be echo")

    if coverage >= _ECHO_COVERAGE:
        return EchoVerdict(kind=ECHO, text="", matched_tokens=matched, total_tokens=total,
                           confidence=coverage, reason="transcript is Nova's own speech")

    remainder = total - matched
    if remainder < _MIN_SALVAGE_TOKENS:
        return EchoVerdict(kind=ECHO, text="", matched_tokens=matched, total_tokens=total,
                           confidence=coverage, reason="nothing left after removing the echo")

    # Salvage: keep everything from the first unmatched token onward, sliced out
    # of the ORIGINAL string so capitalisation and punctuation survive.
    cut = t_tokens[matched][1]
    salvaged = text[cut:].strip().lstrip(",;:-— ").strip()
    if not salvaged:
        return EchoVerdict(kind=ECHO, text="", matched_tokens=matched, total_tokens=total,
                           confidence=coverage, reason="salvage was empty")

    return EchoVerdict(kind=MIXED, text=salvaged, matched_tokens=matched, total_tokens=total,
                       confidence=coverage,
                       reason="Nova's tail plus a genuine interruption")


class EchoFilter:
    """Stateful wrapper that pulls recent speech from a TurnRegistry."""

    def __init__(self, registry, *, window_s: float = 12.0) -> None:
        self._registry = registry
        self._window_s = window_s

    def check(self, transcript: str, *, conversation_id: str | None = None) -> EchoVerdict:
        segments = self._registry.recent_spoken(
            within_s=self._window_s, conversation_id=conversation_id
        )
        return classify(transcript, " ".join(s.text for s in segments))
