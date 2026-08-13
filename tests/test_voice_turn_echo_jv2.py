"""Turn identity, cancellation, and echo suppression.

The invariant under test throughout: nothing produced for a cancelled turn may
become visible or audible, and no genuine user speech may be discarded as echo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.voice.echo import ECHO, MIXED, USER, EchoFilter, classify
from core.voice.turn import TurnRegistry

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def test_turn_lifecycle():
    print("\nturn lifecycle")
    reg = TurnRegistry()
    t1 = reg.start("conv-1")
    check(t1.active, "a new turn is active")
    check(reg.active_turn("conv-1") is t1, "registry knows the live turn")
    check(not reg.is_cancelled(t1.turn_id), "a live turn is not cancelled")

    # The brief's scenario: 105 is cancelled, 106 starts.
    t2 = reg.start("conv-1")
    check(t1.cancelled, "starting a new turn supersedes the previous one")
    check(t1.cancel_reason == "superseded", f"reason recorded (got {t1.cancel_reason!r})")
    check(reg.is_cancelled(t1.turn_id), "the old turn reports cancelled forever after")
    check(not reg.is_cancelled(t2.turn_id), "the new turn is unaffected")
    check(reg.active_turn("conv-1") is t2, "the new turn is now the live one")

    reg.finish(t2.turn_id)
    check(reg.active_turn("conv-1") is None, "a finished turn is no longer active")
    check(not reg.is_cancelled(t2.turn_id), "finishing is not cancelling")


def test_conversations_are_independent():
    print("\nconversation isolation")
    reg = TurnRegistry()
    a = reg.start("conv-a")
    b = reg.start("conv-b")
    check(not a.cancelled, "starting a turn elsewhere does not cancel this one")
    reg.cancel_active("conv-b", reason="user_interrupt")
    check(reg.is_cancelled(b.turn_id) and not reg.is_cancelled(a.turn_id),
          "cancelling one conversation leaves the other alone")


def test_unknown_turn_is_treated_as_cancelled():
    print("\nunknown turns")
    reg = TurnRegistry(history=4)
    ids = [reg.start(f"c{i}").turn_id for i in range(8)]
    check(reg.is_cancelled(ids[0]),
          "a turn evicted from history is treated as cancelled, not as live")
    check(reg.is_cancelled("never-existed"),
          "a turn id we never issued is treated as cancelled")
    check(not reg.is_cancelled(""), "an empty id is not a turn at all")


def test_spoken_history():
    print("\nspoken history")
    reg = TurnRegistry()
    t = reg.start("c")
    reg.record_spoken(t.turn_id, "The RTX 5090 has thirty-two gigabytes.")
    reg.record_spoken(t.turn_id, "It is faster than the 5080.")
    check(len(t.spoken) == 2, "segments recorded")
    check("thirty-two" in t.spoken_text(), "spoken text is recoverable")

    recent = reg.recent_spoken(within_s=60.0, conversation_id="c")
    check(len(recent) == 2, f"recent window returns both (got {len(recent)})")
    check(reg.recent_spoken(within_s=60.0, conversation_id="other") == [],
          "another conversation's speech is not in the window")

    # Age the first segment past the window: echo suppression must not compare
    # against something Nova said a minute ago.
    t.spoken[0].queued_at -= 30.0
    recent = reg.recent_spoken(within_s=10.0, conversation_id="c")
    check(len(recent) == 1 and "5080" in recent[0].text,
          f"speech older than the window is excluded (got {[s.text for s in recent]})")


def test_echo_pure():
    print("\npure echo")
    spoken = "The RTX 5090 has thirty two gigabytes of memory."
    v = classify("the RTX 5090 has thirty two gigabytes of memory", spoken)
    check(v.kind == ECHO, f"Nova hearing herself is echo (got {v.kind}: {v.reason})")
    check(v.text == "", "echo yields no user text")
    check(not v.is_user_speech, "echo is not user speech")


def test_echo_partial_tail():
    print("\npartial echo (tail only)")
    spoken = "The RTX 5090 has thirty two gigabytes of memory."
    v = classify("has thirty two gigabytes of memory", spoken)
    check(v.kind == ECHO, f"a tail of Nova's sentence is still echo (got {v.kind})")


def test_mixed_salvages_the_interruption():
    print("\nmixed: the case that matters")
    spoken = "The RTX 5090 has thirty two gigabytes of memory and costs more."
    heard = "has thirty two gigabytes, but what about the 5080?"
    v = classify(heard, spoken)
    check(v.kind == MIXED, f"echo + interruption is MIXED (got {v.kind}: {v.reason})")
    check(v.text == "but what about the 5080?",
          f"the real question is recovered intact (got {v.text!r})")
    check(v.is_user_speech, "mixed counts as user speech")


def test_genuine_speech_is_never_eaten():
    print("\nfalse positives (the expensive kind)")
    spoken = "The RTX 5090 has thirty two gigabytes of memory."

    for heard, label in [
        ("actually only show the quietest two", "unrelated interruption"),
        ("wait compare the warranty first", "unrelated correction"),
        ("what about the 5080", "short follow-up sharing a couple of words"),
        ("stop", "one-word barge-in"),
        ("no", "minimal barge-in"),
        ("the memory is what I care about", "reuses Nova's vocabulary"),
    ]:
        v = classify(heard, spoken)
        check(v.is_user_speech and v.text == heard,
              f"{label}: passed through untouched (got {v.kind}: {v.text!r})")

    v = classify("anything at all", "")
    check(v.kind == USER, "with nothing spoken recently, everything is user speech")


def test_stt_errors_tolerated():
    print("\nSTT noise tolerance")
    spoken = "The Seagate Exos is the most reliable drive here."
    # Whisper mangles a couple of words but it is still plainly the same speech.
    v = classify("the seagate exos is the most reliabel drive here", spoken)
    check(v.kind == ECHO, f"small transcription errors still read as echo (got {v.kind})")


def test_filter_uses_registry():
    print("\nEchoFilter over the registry")
    reg = TurnRegistry()
    t = reg.start("c")
    reg.record_spoken(t.turn_id, "The three best options are the Exos, the Gold, and the IronWolf.")
    f = EchoFilter(reg)

    v = f.check("actually only show the quietest two", conversation_id="c")
    check(v.is_user_speech, f"real interruption survives the filter (got {v.kind})")

    v = f.check("the three best options are the exos the gold and the ironwolf",
                conversation_id="c")
    check(v.kind == ECHO, f"Nova's own line is caught (got {v.kind})")

    v = f.check("the three best options are the exos", conversation_id="other-conv")
    check(v.is_user_speech, "speech in a conversation Nova was silent in is never echo")


def main():
    test_turn_lifecycle()
    test_conversations_are_independent()
    test_unknown_turn_is_treated_as_cancelled()
    test_spoken_history()
    test_echo_pure()
    test_echo_partial_tail()
    test_mixed_salvages_the_interruption()
    test_genuine_speech_is_never_eaten()
    test_stt_errors_tolerated()
    test_filter_uses_registry()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
