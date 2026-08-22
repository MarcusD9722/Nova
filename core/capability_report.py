from __future__ import annotations

"""What Nova can ACTUALLY do right now, from the runtime rather than the README.

Live, asked "What are you capable of?", Nova dramatically underreported
herself — while `RuntimeManager` and `ToolRouter` already knew every registered
tool. The inventory was collected into the grounding context
(`context["available_tools"]`) and then never rendered: `_grounding_to_natural`
emitted only a smart-home availability flag, so the response model was left to
reconstruct the answer from whatever it happened to remember about itself.

The fix is a single source of runtime truth. This module groups the tool names
the router really has into categories a person would recognise, and reports
each one's state. It does not read the README: shipped documentation describes
intent, and the question "what can you do" is about what is wired up in THIS
process.

HONESTY ABOUT STATE
-------------------
"Registered" and "usable right now" are different claims, and conflating them
is how an assistant ends up promising to send an email it cannot send. A
capability is reported as `available` only when its tools are registered AND
nothing known-at-runtime disables it; otherwise it is reported with the reason
(`disabled`, `needs_setup`, `not_registered`) so the answer can say "built, but
switched off" rather than "I can do that".
"""

import os
from dataclasses import dataclass, field

__all__ = ["Capability", "CapabilityReport", "summarize_capabilities"]


#: category -> (human label, tool-name prefixes/substrings that belong to it)
#:
#: Matching is by prefix on the registered tool NAME, so adding a tool to an
#: existing family needs no change here — which is the point: the answer must
#: not be a hardcoded paragraph that drifts from the registry.
_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("memory", "remembering things you tell me",
     ("memory.", "recall.", "person.", "people.")),
    ("projects", "building and changing projects",
     ("project.", "plan.", "code.write", "code.edit")),
    ("code_understanding", "reading and analysing code",
     ("code.read", "code.index", "code.health", "code.security", "code.list")),
    ("self_inspection", "inspecting and proposing changes to my own source",
     ("self.",)),
    ("research", "searching and reading the web",
     ("web.", "research.", "browse.")),
    ("weather_maps", "weather, places and directions",
     ("weather.", "maps.", "location.")),
    ("reminders", "reminders, goals and planning",
     ("reminder.", "goal.", "task.", "calendar.")),
    ("communication", "email, calendar and messaging",
     ("email.", "gmail.", "discord.", "message.", "notify.")),
    ("computer_control", "controlling this computer",
     ("computer.", "shell.", "app.", "window.")),
    ("vision", "looking at the screen and images",
     ("vision.", "screen.", "image.", "camera.")),
    ("media", "generating images and audio",
     ("media.", "tts.", "speech.", "draw.")),
    ("skills", "skills I have learned",
     ("skill.",)),
    ("specialists", "my specialist advisors",
     ("agent.", "agents.", "executive.", "society.")),
    ("experiments", "running and analysing experiments",
     ("experiment.",)),
    ("smart_home", "smart-home control",
     ("smart", "home.", "light", "thermostat")),
)


@dataclass
class Capability:
    """One category of things Nova can do, and whether she can do it now."""

    key: str
    label: str
    state: str                      # available | disabled | needs_setup | not_registered
    tools: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "available"


@dataclass
class CapabilityReport:
    """The whole runtime picture, grouped."""

    capabilities: list[Capability] = field(default_factory=list)
    total_tools: int = 0

    def usable(self) -> list[Capability]:
        return [c for c in self.capabilities if c.usable]

    def unavailable(self) -> list[Capability]:
        return [c for c in self.capabilities
                if c.state in ("disabled", "needs_setup") and c.tools]

    def sentence(self) -> str:
        """A compact, honest summary — the deterministic answer's backbone."""
        can = self.usable()
        if not can:
            return "Right now I have no tools registered at all."
        parts = [c.label for c in can]
        out = f"Right now I can help with: {'; '.join(parts)}."
        held = self.unavailable()
        if held:
            bits = [f"{c.label} ({c.note or c.state.replace('_', ' ')})" for c in held]
            out += f" Built but not usable right now: {'; '.join(bits)}."
        return out

    def prompt_line(self) -> str:
        """One line for the grounding context when introspection was asked."""
        can = ", ".join(c.key for c in self.usable())
        held = ", ".join(f"{c.key}:{c.state}" for c in self.unavailable())
        line = f"tools actually registered right now ({self.total_tools}) cover: {can}"
        if held:
            line += f"; registered but not usable: {held}"
        return line


def _env_off(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default).strip().lower() in {"0", "false", "no", "off"})


def summarize_capabilities(tool_names: list[str] | None) -> CapabilityReport:
    """Group the REGISTERED tool names and judge each category's state.

    `tool_names` comes from `ToolRouter.list_tools()` — the same registry the
    agent loop calls — so the answer cannot drift from what is wired up.
    """
    names = sorted({str(t).strip() for t in (tool_names or []) if str(t).strip()})
    report = CapabilityReport(total_tools=len(names))

    for key, label, prefixes in _CATEGORIES:
        owned = [n for n in names
                 if any(n.lower().startswith(p) or p in n.lower() for p in prefixes)]
        if not owned:
            report.capabilities.append(
                Capability(key=key, label=label, state="not_registered"))
            continue

        state, note = "available", ""
        # Runtime switches Nova can actually read. Anything not checkable here
        # stays "available" rather than being guessed at — an unknown is not
        # evidence of being broken, and inventing a caveat is its own dishonesty.
        if key == "computer_control" and _env_off("NOVA_ALLOW_SHELL"):
            state, note = "disabled", "shell access is switched off"
        elif key == "research" and _env_off("NOVA_ALLOW_NETWORK_TOOLS"):
            state, note = "disabled", "network tools are switched off"
        elif key == "self_inspection" and _env_off("NOVA_DEV_MODE", "0"):
            state, note = "disabled", "developer mode is off"

        report.capabilities.append(
            Capability(key=key, label=label, state=state, tools=owned, note=note))

    return report
