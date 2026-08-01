"""Regenerate .env.example from the config catalog — one documented file.

`core/settings.py` already knows every NOVA_* setting, its type, default and a
one-line description. This turns that into a complete, self-documenting
.env.example so there is a single place to see everything Nova can be configured
with, with a comment above every line.

Run after adding a setting:

    .\\venv\\Scripts\\python.exe tools\\gen_env_example.py

Design choices that matter:

* Settings WITH a default are emitted COMMENTED OUT, showing that default. So
  copying this file to .env is safe — you inherit defaults by omission instead
  of pinning today's values forever. Uncomment only what you want to override.
* Secrets and third-party keys are emitted UNCOMMENTED and EMPTY, because those
  are the lines you actually have to fill in.
* Real secrets never live here. .env.example is committed; .env is git-ignored.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.settings import CATALOG  # noqa: E402

SETTINGS_SRC = REPO / "core" / "settings.py"
OUT = REPO / ".env.example"

# Third-party credentials (not NOVA_*, so not in the catalog). Each is optional:
# the matching feature reports "not configured" when absent.
THIRD_PARTY: list[tuple[str, str, str]] = [
    ("OPENWEATHER_API_KEY", "", "Weather lookups (weather.current). Free tier at openweathermap.org."),
    ("GOOGLE_MAPS_API_KEY", "", "Maps, geocoding, places and directions (maps.* tools + the map panel)."),
    ("DISCORD_BOT_TOKEN", "", "Discord bot token for discord.send / discord.read."),
    ("DISCORD_CHANNEL_ID", "", "The Discord channel id Nova posts to and reads from."),
    ("GOOGLE_OAUTH_CLIENT_ID", "", "Google OAuth client id — calendar (read-only) + Gmail (read + DRAFT only)."),
    ("GOOGLE_OAUTH_CLIENT_SECRET", "", "Google OAuth client secret. See tools/README_google_oauth.md."),
    ("COQUI_TOS_AGREED", "1", "Set 1 to accept the Coqui CPML licence for the one-time XTTS download (~1.9 GB)."),
]

# Vars that are credentials/tokens: emitted uncommented + empty so they stand out.
SECRETY = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)$")


def sections_in_order() -> list[tuple[str, list[str]]]:
    """Read settings.py to recover the section headers and which vars follow
    each, so the generated file keeps the same human grouping."""
    src = SETTINGS_SRC.read_text(encoding="utf-8")
    out: list[tuple[str, list[str]]] = []
    current = "General"
    seen: set[str] = set()
    for line in src.splitlines():
        header = re.search(r"#\s*──+\s*(.+?)\s*──+", line)
        if header:
            current = header.group(1).strip()
            continue
        m = re.search(r'_s\(\s*"(NOVA_[A-Z0-9_]+)"', line)
        if not m:
            continue
        name = m.group(1)
        if name in seen or name not in CATALOG:
            continue
        seen.add(name)
        if not out or out[-1][0] != current:
            out.append((current, []))
        out[-1][1].append(name)
    # Anything the parse missed still gets emitted, so the file is exhaustive.
    missing = [n for n in CATALOG if n not in seen]
    if missing:
        out.append(("Other", sorted(missing)))
    return out


def wrap(text: str, width: int = 76) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def render() -> str:
    L: list[str] = []
    L.append("# ============================================================================")
    L.append("# Nova configuration — EVERY setting, with what it does above each line.")
    L.append("#")
    L.append("# GENERATED FILE. Regenerate after adding a setting:")
    L.append("#     .\\venv\\Scripts\\python.exe tools\\gen_env_example.py")
    L.append("#")
    L.append("# To use it:  Copy-Item .env.example .env   then fill in the keys you want.")
    L.append("#")
    L.append("# Lines with a default are COMMENTED OUT and show that default — copying this")
    L.append("# file is therefore safe: you inherit defaults by omission rather than pinning")
    L.append("# today's values. Uncomment a line only to override it.")
    L.append("#")
    L.append("# NEVER commit your real .env — it is git-ignored on purpose. Secrets belong")
    L.append("# only in .env and credentials/, both of which Nova's self-editing refuses to")
    L.append("# read or modify.")
    L.append("# ============================================================================")
    L.append("")

    L.append("# ─────────────────────────────────────────────────────────────────────────")
    L.append("# Third-party API keys (all optional — features report 'not configured')")
    L.append("# ─────────────────────────────────────────────────────────────────────────")
    for name, default, desc in THIRD_PARTY:
        L.append("")
        for line in wrap(desc):
            L.append(f"# {line}")
        L.append(f"{name}={default}")
    L.append("")

    for section, names in sections_in_order():
        L.append("")
        L.append("# ─────────────────────────────────────────────────────────────────────────")
        L.append(f"# {section}")
        L.append("# ─────────────────────────────────────────────────────────────────────────")
        for name in names:
            spec = CATALOG[name]
            L.append("")
            for line in wrap(spec.description):
                L.append(f"# {line}")
            is_secret = bool(spec.secret or SECRETY.search(name))
            if is_secret:
                L.append(f"# (secret — keep this only in .env, never in .env.example)")
                L.append(f"{name}=")
            elif spec.default == "":
                L.append(f"# type: {spec.kind} · default: (empty)")
                L.append(f"# {name}=")
            else:
                L.append(f"# type: {spec.kind} · default: {spec.default}")
                L.append(f"# {name}={spec.default}")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    text = render()
    OUT.write_text(text, encoding="utf-8")
    documented = text.count("\n# NOVA_") + sum(1 for n in CATALOG if f"\n{n}=" in text)
    print(f"Wrote {OUT} — {len(CATALOG)} cataloged settings + {len(THIRD_PARTY)} third-party keys")
    print(f"{len(text.splitlines())} lines")
