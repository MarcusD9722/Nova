"""Speech chunker V2 and the display/spoken text split.

Everything here is a pure function, so these are exact assertions rather than
smoke tests. The V1 failures are kept as named cases so a regression is
obvious.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.voice.chunker import SpeechChunker, split_sentences
from core.voice.speech_text import has_speakable_content, to_spoken

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def stream(text, *, chunk=7, **kw):
    """Feed text through the chunker the way tokens actually arrive."""
    c = SpeechChunker(**kw)
    out = []
    for i in range(0, len(text), chunk):
        out.extend(c.feed(text[i:i + chunk]))
    out.extend(c.flush())
    return out


def test_v1_regressions():
    print("\nthings V1 got wrong")

    cases = [
        ("Dr. Chen reviewed the drive.", "abbreviation: Dr."),
        ("The Exos holds 3.5 TB per platter.", "decimal: 3.5"),
        ("Check e.g. the WD Gold first.", "abbreviation: e.g."),
        ("J. R. R. Tolkien wrote it.", "initials: J. R. R."),
        ("Open README.md and read it.", "filename: README.md"),
        ("Go to https://example.com/a.b.c now.", "URL with dots"),
        ("Call memory.recall for that.", "dotted identifier"),
        ("Mail me at a.b@example.com today.", "email address"),
        ("Nova v2.1.4 is the build.", "version number"),
    ]
    for text, label in cases:
        parts = split_sentences(text)
        check(parts == [text], f"{label} -> one utterance (got {parts})")


def test_real_sentences_still_split():
    print("\nreal boundaries still split")
    parts = split_sentences("I found the problem. There are three fixes. Want them?")
    check(parts == ["I found the problem.", "There are three fixes.", "Want them?"],
          f"three sentences (got {parts})")

    parts = split_sentences('She said "stop." Then she left.')
    check(len(parts) == 2, f"closing quote stays with its sentence (got {parts})")

    parts = split_sentences("Wait... that is not right. Try again.")
    check(len(parts) == 2 and parts[0].startswith("Wait..."),
          f"ellipsis is not three boundaries (got {parts})")


def test_first_chunk_is_early():
    print("\nlatency: first chunk")
    text = ("Okay, I found the problem with your server configuration, and there are "
            "three things we should change before testing it again.")
    parts = stream(text)
    check(len(parts) >= 2, f"a long run-on is broken up rather than held (got {len(parts)})")
    check(parts[0].endswith(","), f"first cut lands on a clause boundary (got {parts[0]!r})")
    check(len(parts[0]) < len(text), "first chunk is genuinely shorter than the whole reply")
    check(" ".join(parts).replace(" ", "") == text.replace(" ", ""),
          "no words are lost or duplicated across chunks")


def test_no_staccato_fragments():
    print("\nno staccato")
    text = ("The drive is quiet, fast, cheap, and, honestly, better than the one, "
            "which you had before, in every way that matters here.")
    parts = stream(text, clause_min_chars=40)
    tiny = [p for p in parts[:-1] if len(p) < 20]
    check(not tiny, f"no sub-20-char mid-stream fragments (offenders: {tiny})")


def test_short_complete_sentence_speaks_immediately():
    print("\nshort sentences are fine")
    parts = stream("Sure. Let me check that for you now.")
    check(parts[0] == "Sure.", f"a 5-char complete sentence is emitted as-is (got {parts[0]!r})")


def test_hard_cut_on_runaway():
    print("\nrunaway sentence")
    text = "word " * 120  # no punctuation at all
    parts = stream(text, max_chars=200)
    check(len(parts) >= 2, f"a punctuation-free run-on still gets spoken (got {len(parts)} chunks)")
    check(all(len(p) <= 260 for p in parts), "no chunk exceeds the hard limit")


def test_nothing_is_lost():
    print("\nlossless")
    text = ("Right. The RTX 5090 has 32 GB of GDDR7, which is 8 GB more than the 5080, "
            "so it holds bigger models. Want the full spec sheet?")
    parts = stream(text)
    rejoined = " ".join(parts)
    check(rejoined.replace(" ", "") == text.replace(" ", ""),
          "streaming reassembles to exactly the original text")


def test_open_code_fence_is_held():
    print("\ncode fences while streaming")
    text = "Here is the fix.\n```python\nx = 1\ny = 2\n```\nThat should do it."
    parts = stream(text)
    # Nothing may be cut inside the fence, or to_spoken can no longer recognise
    # the fragment as code and would recite the source aloud.
    for p in parts:
        check(p.count("```") % 2 == 0,
              f"chunk has balanced fences (got {p!r})")
    spoken = " ".join(to_spoken(p) for p in parts)
    check("x = 1" not in spoken and "y = 2" not in spoken,
          f"no source code reaches the voice (got {spoken!r})")

    # An unterminated fence must still be spoken at flush rather than swallowed.
    c = SpeechChunker()
    c.feed("Look at this.\n```python\nx = 1\n")
    out = c.flush()
    check(out and any("Look at this." in o for o in out),
          f"an unterminated fence still flushes (got {out})")


def test_spoken_text():
    print("\ndisplay -> spoken")

    # A terminator is added so the chunker can treat a bare fragment as
    # speakable; without it a heading or list item runs into the next one.
    check(to_spoken("**RTX 5090:** 32 GB GDDR7") == "RTX 5090: 32 gigabytes GDDR7.",
          f"the brief's example (got {to_spoken('**RTX 5090:** 32 GB GDDR7')!r})")

    check("gigabytes" in to_spoken("It has 24 GB."), "GB expands")
    check("terabytes" in to_spoken("A 28 TB drive."), "TB expands")
    check(to_spoken("1 GB free") == "1 gigabyte free.", "singular unit is singular")
    check("3.5 terabytes" in to_spoken("3.5 TB usable"), "decimals survive unit expansion")

    out = to_spoken("See [the docs](https://example.com/very/long/path) for more.")
    check("example.com" not in out and "the docs" in out,
          f"link label is spoken, href is not (got {out!r})")

    out = to_spoken("Visit https://example.com/x?y=1 today.")
    check("http" not in out and "a link" in out, f"bare URL is not read aloud (got {out!r})")

    out = to_spoken("Here:\n```python\nprint('hi')\n```\nThat is it.")
    check("print" not in out and "code" in out.lower(),
          f"code fences are described, not recited (got {out!r})")

    out = to_spoken("Use the `memory.recall` tool.")
    check(out == "Use the memory.recall tool.", f"inline code keeps its content (got {out!r})")

    out = to_spoken("## Findings\n- First item\n- Second item")
    check("#" not in out and "-" not in out, f"heading/bullet syntax is gone (got {out!r})")
    check("Findings." in out and "First item." in out,
          f"list items become spoken sentences (got {out!r})")

    out = to_spoken("| a | b |\n|---|---|\n| 1 | 2 |")
    check("|" not in out and "table" in out.lower(), f"tables are summarised (got {out!r})")

    check("!" not in to_spoken("Wow~~no~~ 🎉").replace("!", ""), "emoji removed")
    check(to_spoken("Really!!!") == "Really!", "repeated punctuation collapsed")

    check(has_speakable_content("Hello there"), "plain text is speakable")
    check(not has_speakable_content("```\ncode\n```"), "a code-only reply has nothing to say")
    check(not has_speakable_content("   "), "whitespace has nothing to say")


def test_spoken_text_preserves_facts():
    print("\nspoken text stays accurate")
    src = "The **Seagate Exos X28** is 28 TB, spins at 7200 rpm, and draws 9.4 W idle."
    out = to_spoken(src)
    check("Seagate Exos X28" in out, "product name intact")
    check("28 terabytes" in out, "capacity intact and expanded")
    check("7200" in out, "rpm figure intact")
    check("9.4 watts" in out, f"power figure intact (got {out!r})")


def test_chunker_over_spoken_text():
    print("\nchunker + spoken text together")
    display = ("## Options\n"
               "1. **Seagate Exos** — 28 TB, 7200 rpm.\n"
               "2. **WD Gold** — 26 TB, quieter.\n"
               "See [specs](https://example.com/specs).")
    spoken = to_spoken(display)
    parts = split_sentences(spoken)
    check(len(parts) >= 3, f"the list speaks as several utterances (got {len(parts)})")
    check(not any("*" in p or "#" in p or "http" in p for p in parts),
          f"no markdown reaches the voice (got {parts})")


def main():
    test_v1_regressions()
    test_real_sentences_still_split()
    test_first_chunk_is_early()
    test_no_staccato_fragments()
    test_short_complete_sentence_speaks_immediately()
    test_hard_cut_on_runaway()
    test_nothing_is_lost()
    test_open_code_fence_is_held()
    test_spoken_text()
    test_spoken_text_preserves_facts()
    test_chunker_over_spoken_text()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
