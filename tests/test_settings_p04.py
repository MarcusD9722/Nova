"""Phase 0.4: central config catalog + boot validation.

The most important check here is self-verification: this test scans the
actual codebase for NOVA_* usages and fails if any variable is used without
being cataloged in core/settings.py — so the catalog can never rot.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.settings import CATALOG, get_bool, get_float, get_int, get_str, validate_environment

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def main():
    # ── Self-verification: every var used in code is cataloged ──
    used: set[str] = set()
    for folder in ("core", "backend", "memory", "plugins", "tools"):
        for f in (REPO / folder).rglob("*.py"):
            if "__pycache__" in f.parts or "venv" in f.parts:
                continue
            used |= set(re.findall(r'"(NOVA_[A-Z_]+)"', f.read_text(encoding="utf-8", errors="replace")))
    missing = sorted(used - set(CATALOG))
    check(not missing, f"every NOVA_* var used in code is cataloged (missing: {missing or 'none'})")
    unused = sorted(set(CATALOG) - used)
    check(not unused, f"no cataloged var is unused in code (stale: {unused or 'none'})")

    # ── Validation behaviors (pure, on a supplied environ) ──
    warnings = validate_environment({"NOVA_PROT": "9000"})
    check(any("NOVA_PROT" in w for w in warnings), "unknown var flagged")
    check(any("NOVA_PORT" in w for w in warnings), "typo hint suggests the closest real var")

    warnings = validate_environment({"NOVA_PORT": "banana"})
    check(any("NOVA_PORT" in w and "integer" in w for w in warnings), "non-integer int flagged")

    warnings = validate_environment({"NOVA_TTS_SPEED": "fast"})
    check(any("NOVA_TTS_SPEED" in w for w in warnings), "non-numeric float flagged")

    warnings = validate_environment({"NOVA_DEV_MODE": "maybe"})
    check(any("NOVA_DEV_MODE" in w for w in warnings), "unrecognized boolean flagged")

    warnings = validate_environment({"NOVA_BRIEFING_TIME": "8am"})
    check(any("NOVA_BRIEFING_TIME" in w for w in warnings), "bad HH:MM flagged")

    warnings = validate_environment({"NOVA_BRIEFING_TIME": "07:30", "NOVA_PORT": "8008", "NOVA_DEV_MODE": "1"})
    check(warnings == [], f"valid values produce zero warnings (got {warnings})")

    # Secrets never leak into messages
    warnings = validate_environment({"NOVA_API_TOKEN": "super-secret-value-123"})
    check(all("super-secret-value-123" not in w for w in warnings), "secret values never appear in warnings")

    # ── Typed accessors honor catalog defaults ──
    check(get_int("NOVA_PORT") in (8008, int(__import__("os").getenv("NOVA_PORT", "8008"))), "get_int reads default/env")
    check(isinstance(get_bool("NOVA_AUTONOMY"), bool), "get_bool returns bool")
    check(isinstance(get_float("NOVA_TTS_SPEED"), float), "get_float returns float")
    check(get_str("NOVA_STT_MODEL_SIZE") != "", "get_str returns catalog default when unset")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
