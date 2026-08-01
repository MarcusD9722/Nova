"""U2: the context firewall — what may leave the machine.

Safety-critical. Nova's promise is that her memory of Marcus stays local even
when a *role* is routed to a remote model. These tests prove the bulk,
structured leaks (grounding blocks, memory dumps, names) never survive.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.context_firewall import inspect_text, scrub_messages, verify_safe

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def main():
    # ── A real grounding JSON blob must be detected and dropped ──
    grounding = json.dumps({
        "known_user": {"name": "Marcus"},
        "known_family": {"spouse": "Leslie", "children": ["Liam", "Mateo"]},
        "recent_mood": "Marcus seemed tired the last 3 days.",
        "capabilities": {},
    })
    check(inspect_text(grounding), "grounding JSON blob is detected")
    res = scrub_messages([
        {"role": "system", "content": grounding},
        {"role": "user", "content": "Write a Python function that reverses a linked list."},
    ])
    check(res.dropped == 1, f"grounding block dropped (dropped={res.dropped})")
    check(len(res.messages) == 1, "the task message survives")
    check("linked list" in res.messages[0]["content"], "task content preserved intact")
    check(verify_safe(res.messages) == [], "post-scrub verification is clean")

    # ── The RENDERED (natural-language) grounding line must also be caught ──
    rendered = ("the user's name is Marcus; known family: spouse Leslie, children Liam, Mateo; "
                "you are currently working with the user on the 'nova' project")
    check(inspect_text(rendered), "rendered grounding line is detected")
    res2 = scrub_messages([{"role": "system", "content": rendered}])
    check(res2.dropped == 1 and res2.messages == [], "rendered grounding line dropped")

    # ── Raw memory records must be caught ──
    memdump = "FACT user spouse = Leslie\nPERSON Leslie {\"relation\": \"spouse\"}\nEVENT 2026-07-04: fireworks"
    check("memory-record" in inspect_text(memdump), "raw memory records detected")
    check(scrub_messages([{"role": "user", "content": memdump}]).messages == [], "memory dump dropped")

    # ── Identities inside an otherwise legitimate task are REDACTED, not dropped ──
    task = "Marcus wants a login page. Make sure Leslie can also sign in as an admin."
    res3 = scrub_messages([{"role": "user", "content": task}],
                          user_name="Marcus", known_names=["Leslie", "Liam"])
    check(res3.dropped == 0, "a legitimate task message is NOT dropped")
    check(res3.redactions == 2, f"both identities redacted (got {res3.redactions})")
    out = res3.messages[0]["content"]
    check("Marcus" not in out and "Leslie" not in out, "no personal names survive")
    check("[user]" in out and "[person]" in out, "names replaced with placeholders")
    check("login page" in out and "admin" in out, "the actual task survives redaction")

    # ── Redaction is case-insensitive and word-bounded ──
    res4 = scrub_messages([{"role": "user", "content": "marcus and MARCUS and Marcusville"}],
                          user_name="Marcus")
    o4 = res4.messages[0]["content"]
    check("marcus and" not in o4.lower().replace("[user] and", ""), "case-insensitive redaction")
    check("Marcusville" in o4, "word-boundary respected (Marcusville untouched)")

    # ── A clean coding message passes through untouched ──
    clean = [{"role": "user", "content": "Refactor this function to use asyncio.gather:\n\ndef f(): pass"}]
    res5 = scrub_messages(clean)
    check(res5.dropped == 0 and res5.redactions == 0, "clean coding task untouched")
    check(res5.messages[0]["content"] == clean[0]["content"], "content byte-identical")
    check(res5.summary() == "nothing personal detected", "summary is honest when clean")

    # ── Ordinary prose must NOT false-positive on key-like words ──
    prose = [{"role": "user", "content": "This module tracks known people in a graph structure."}]
    check(scrub_messages(prose).dropped == 0, "ordinary prose mentioning 'known people' not dropped")

    # ── verify_safe is the fail-closed gate ──
    check(verify_safe([{"role": "system", "content": grounding}]) != [], "verify_safe catches a leaked blob")

    # ── Input is never mutated ──
    original = [{"role": "user", "content": "Marcus asked for tests."}]
    snapshot = json.dumps(original)
    scrub_messages(original, user_name="Marcus")
    check(json.dumps(original) == snapshot, "caller's message list is never mutated")

    # ── Reporting is useful for the audit trail ──
    res6 = scrub_messages([{"role": "system", "content": grounding},
                           {"role": "user", "content": "Marcus wants a parser."}], user_name="Marcus")
    check(res6.markers and any("grounding-key" in m for m in res6.markers), "markers name what tripped")
    check("withheld" in res6.summary() and "redacted" in res6.summary(), "summary reports both actions")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
