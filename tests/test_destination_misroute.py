"""Regression: a declarative sentence must never be read as a routing request.

"We all just got home and we are about to go to sleep" made Nova reply
"Happy to route you to sleep — where are you starting from?" because the
directions pattern matched any mid-sentence "go to". Chatting about your
evening is not a navigation request.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.runtime import _extract_destination_from_here as extract

_fail = False


def check(cond, label):
    global _fail
    if not cond:
        _fail = True
    print(f"  {'OK  ' if cond else 'FAIL'} {label}")


# ── Declaratives: NEVER a destination ──
for text in [
    "We all just got home together and we are about to go to sleep.",
    "I have to go to work tomorrow",
    "we're gonna go to bed",
    "the kids need to go to school in the morning",
    "I might head to bed early",
]:
    check(extract(text) is None, f"not a routing request: {text[:44]!r}")

# ── Real navigation requests: still work ──
for text, want in [
    ("go to Chipotle", "Chipotle"),
    ("let's drive to Austin", "Austin"),
    ("can you navigate to the hardware store", "the hardware store"),
    ("take me to Chipotle", "Chipotle"),
    ("how do i get to Chipotle", "Chipotle"),
    ("directions to the airport", "the airport"),
]:
    got = extract(text)
    check(got == want, f"still routes: {text[:38]!r} -> {got!r}")

print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
sys.exit(1 if _fail else 0)
