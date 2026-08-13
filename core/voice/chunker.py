from __future__ import annotations

"""Speech chunker V2 — decide when a partial reply is safe to start speaking.

The V1 rule was one line:

    re.split(r"(?<=[.!?])\\s+", buffer)

which splits on any period followed by a space. That mispronounces Nova's own
subject matter constantly: "Dr. Chen", "3.5 TB", "e.g. the WD Gold", "J. R. R.",
"README.md file", "vs. the Exos" all become two utterances with an unnatural
pause and a dropped intonation contour.

The goal is not "split correctly" though — it is **start speaking early without
sounding chopped**. Those pull in opposite directions, so the rules are ordered:

1. A real sentence boundary is always safe. Emit immediately, whatever the
   length; short complete sentences ("Sure.", "Got it.") sound natural spoken
   alone, and waiting for a length quota just adds latency.
2. Failing that, once the buffer is long enough that the listener is waiting,
   cut at a **clause** boundary — comma, semicolon, colon, dash, or a
   coordinating conjunction. A clause carries its own prosody, so
   "Okay, I found the problem with your server config," speaks cleanly while
   the model is still writing the rest.
3. Failing that, hard-cut at a word boundary so a run-on sentence cannot hold
   the voice hostage indefinitely.

The first chunk of a turn gets a lower bar than the rest, because time-to-first-
audio is the metric a human actually feels (see docs/JARVIS_V2_BENCHMARKS.md).
Later chunks are already covered by the audio still playing, so they can afford
to be longer and more natural.
"""

import re

#: Words that end in a period without ending a sentence. Matched case-
#: insensitively against the token immediately before the period.
_ABBREVIATIONS = frozenset("""
mr mrs ms dr prof sr jr st rev hon gen col sgt lt capt cmdr
inc ltd co corp llc plc dept est fig vol no vs etc al
jan feb mar apr jun jul aug sept sep oct nov dec
mon tue tues wed thu thurs fri sat sun
approx est max min avg dept univ assn bros
am pm ie eg cf viz resp
""".split())

#: Terminator followed by optional closing quote/bracket, then whitespace.
_TERMINATOR = re.compile(r'([.!?]+)(["\'\)\]”’]*)(\s+)')

#: Spans that must never be split inside: URLs, emails, dotted filenames,
#: version numbers, and dotted identifiers like `memory.recall`.
_PROTECTED = re.compile(
    r'https?://\S+'
    r'|www\.\S+'
    r'|\b[\w.+-]+@[\w-]+\.[\w.]+'
    r'|\b\w[\w-]*\.(?:com|org|net|io|dev|ai|co|uk|gov|edu)\b'
    r'|\b[\w-]+\.(?:py|md|txt|json|js|jsx|ts|tsx|html|css|yml|yaml|toml|cfg|ini|log|csv|sh|ps1)\b'
    r'|\bv?\d+(?:\.\d+)+\b'
    r'|\b[a-z_]+(?:\.[a-z_]+)+\b',
    re.IGNORECASE,
)

#: Clause boundaries, in descending order of how natural a pause sounds there.
_CLAUSE = re.compile(
    r'(?:[,;:]|\s—\s|\s--?\s)\s+'
    r'|\s+(?=(?:and|but|so|because|which|while|though|although|however|then)\s)',
    re.IGNORECASE,
)

_WORD_BREAK = re.compile(r'\s+')


class SpeechChunker:
    """Incremental splitter. Feed tokens, take back speakable chunks.

    Stateful across a turn only in that it knows whether it has emitted yet;
    construct one per turn.
    """

    def __init__(
        self,
        *,
        first_min_chars: int = 12,
        clause_min_chars: int = 40,
        clause_after: int = 90,
        max_chars: int = 240,
    ) -> None:
        self.first_min_chars = first_min_chars
        self.clause_min_chars = clause_min_chars
        self.clause_after = clause_after
        self.max_chars = max_chars
        self._buffer = ""
        self._emitted = 0

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def emitted(self) -> int:
        return self._emitted

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any chunks that are now safe to speak."""
        if not text:
            return []
        self._buffer += text
        return self._drain()

    def flush(self) -> list[str]:
        """End of stream: emit whatever is left, boundaries or not."""
        out = self._drain()
        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            out.append(tail)
            self._emitted += 1
        return out

    # ── internals ────────────────────────────────────────────────────────────

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._next_cut()
            if cut is None:
                break
            head, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:].lstrip()
            if head:
                out.append(head)
                self._emitted += 1
        return out

    def _next_cut(self) -> int | None:
        buf = self._buffer
        if not buf.strip():
            return None

        # An open code fence is the one construct that legitimately spans
        # sentence boundaries. Cutting inside it would hand the voice half a
        # fence, which speech_text.to_spoken can no longer recognise as code —
        # so it would recite the source. Hold everything from the opening fence
        # until it closes (or until flush, which speaks what is left anyway).
        limit = len(buf)
        if buf.count("```") % 2 == 1:
            limit = buf.rfind("```")
            if limit <= 0:
                return None
            buf = buf[:limit]
            if not buf.strip():
                return None

        # 1. A genuine sentence end is always the best place to stop.
        pos = self._sentence_end(buf)
        if pos is not None:
            return pos

        # Below here we are cutting mid-sentence, which is only worth doing when
        # the listener would otherwise be waiting on silence.
        threshold = self.first_min_chars if self._emitted == 0 else self.clause_after
        if len(buf) < threshold:
            return None

        # 2. Clause boundary.
        pos = self._clause_end(buf)
        if pos is not None:
            return pos

        # 3. Hard cut so a run-on cannot stall the voice forever.
        if len(buf) >= self.max_chars:
            window = buf[: self.max_chars]
            match = None
            for match in _WORD_BREAK.finditer(window):
                pass
            if match is not None and match.start() >= self.clause_min_chars:
                return match.start()
        return None

    def _protected_spans(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in _PROTECTED.finditer(text)]

    def _sentence_end(self, text: str) -> int | None:
        spans = self._protected_spans(text)
        for m in _TERMINATOR.finditer(text):
            dot_start, dot_end = m.start(1), m.end(1)
            if any(s <= dot_start < e for s, e in spans):
                continue

            terminator = m.group(1)
            preceding = text[:dot_start]

            # Covers "." and "...": an ellipsis followed by lowercase is a
            # trailing-off continuation ("Wait... that is not right"), not two
            # sentences, and splitting it drops the intonation that makes it
            # sound like one thought.
            if set(terminator) == {"."}:
                word = re.search(r'([A-Za-z]+)$', preceding)
                token = word.group(1).lower() if word else ""
                if token in _ABBREVIATIONS:
                    continue
                # Single-letter initial: "J. R. R. Tolkien".
                if word and len(word.group(1)) == 1 and word.group(1).isupper():
                    continue
                # A lowercase word right after a lone period is far more often
                # a missed abbreviation than a new sentence.
                nxt = text[m.end():m.end() + 1]
                if nxt and nxt.isalpha() and nxt.islower():
                    continue

            end = m.end(2)  # keep the terminator and any closing quote
            # Require at least one character after the whitespace, so we never
            # split on a period that is simply the newest token in the stream.
            if m.end(3) >= len(text):
                continue
            return end
        return None

    def _clause_end(self, text: str) -> int | None:
        spans = self._protected_spans(text)
        best: int | None = None
        for m in _CLAUSE.finditer(text):
            cut = m.end(0) if m.group(0).strip() else m.start()
            # For conjunction matches the group is pure whitespace; cut before
            # the conjunction so "…config," / "and there are three…" splits
            # where a speaker would breathe.
            if not m.group(0).strip():
                cut = m.start()
            head_len = len(text[:cut].strip())
            if head_len < self.clause_min_chars:
                continue
            if any(s <= m.start() < e for s, e in spans):
                continue
            if len(text) - cut < 2:
                continue
            best = cut
            break
        return best


def split_sentences(text: str) -> list[str]:
    """One-shot split of complete text. Same rules, no streaming state."""
    chunker = SpeechChunker(first_min_chars=10_000, clause_after=10_000, max_chars=10_000)
    out = chunker.feed(text)
    out.extend(chunker.flush())
    return out
