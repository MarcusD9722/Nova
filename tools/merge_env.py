"""Merge your existing .env into the fully-documented template — one file.

Produces a .env that has EVERYTHING: your real values, every available setting,
and a comment above each line explaining what it does.

    .\\venv\\Scripts\\python.exe tools\\merge_env.py            # preview only
    .\\venv\\Scripts\\python.exe tools\\merge_env.py --write    # back up + write

What it does:
  * reads your current .env (locally — values are never printed or logged),
  * walks .env.example (the documented template),
  * fills in each setting you had a value for, uncommenting that line,
  * leaves everything else commented at its default, so nothing changes
    behavior just by being documented,
  * backs up your old .env to .env.backup-<timestamp> before writing.

Anything in your .env that the template doesn't know about is reported by NAME
only and preserved in an "Unrecognized" section at the bottom, so a stale or
custom variable is never silently dropped.

Your .env stays git-ignored. This never writes secrets into .env.example.
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV = REPO / ".env"
TEMPLATE = REPO / ".env.example"

ASSIGN = re.compile(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)\s*=(.*)$")


def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE from a .env. Only ACTIVE (uncommented) lines count."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = ASSIGN.match(raw)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def merge(template: str, values: dict[str, str]) -> tuple[str, set[str]]:
    out: list[str] = []
    used: set[str] = set()
    for line in template.splitlines():
        m = ASSIGN.match(line)
        if not m:
            out.append(line)
            continue
        name = m.group(1)
        if name in values and values[name] != "":
            out.append(f"{name}={values[name]}")   # your value, line uncommented
            used.add(name)
        else:
            out.append(line)                        # leave template default as-is
    return "\n".join(out) + "\n", used


def main() -> int:
    write = "--write" in sys.argv
    if not TEMPLATE.exists():
        print("!! .env.example not found — run tools/gen_env_example.py first")
        return 1

    values = read_env(ENV)
    if not ENV.exists():
        print("No existing .env found. Just copy the template:")
        print("    Copy-Item .env.example .env")
        return 0

    merged, used = merge(TEMPLATE.read_text(encoding="utf-8"), values)
    leftover = sorted(set(values) - used)

    if leftover:
        merged += "\n# ─────────────────────────────────────────────────────────────────────────\n"
        merged += "# Unrecognized — these were in your .env but are not settings Nova reads.\n"
        merged += "# Kept so nothing is lost; safe to delete once you've checked them.\n"
        merged += "# ─────────────────────────────────────────────────────────────────────────\n"
        for name in leftover:
            merged += f"{name}={values[name]}\n"

    # Report by NAME only — never print a value.
    print(f"Existing .env:        {len(values)} value(s) set")
    print(f"Merged into template: {len(used)} recognized -> {sorted(used)}")
    if leftover:
        print(f"Not recognized:       {leftover}  (preserved at the bottom)")
    print(f"Result:               {len(merged.splitlines())} lines, fully commented")

    if not write:
        print("\nPreview only. Re-run with --write to back up and apply:")
        print("    .\\venv\\Scripts\\python.exe tools\\merge_env.py --write")
        return 0

    backup = ENV.with_name(f".env.backup-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(ENV, backup)
    ENV.write_text(merged, encoding="utf-8")
    print(f"\nBacked up old .env -> {backup.name}")
    print(f"Wrote merged .env ({len(merged.splitlines())} lines).")
    print("Check it, then restart Nova.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
