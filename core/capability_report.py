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
is how an assistant ends up promising to send an email it cannot send.

That distinction was got wrong once already here, which is why it is spelled
out: computer-control tools ARE registered, and `RuntimeManager` constructs
`ComputerControl(..., adapter=None)` — no platform adapter ships, so
`.available` is false and every action is a dry run. The report called the
whole category "available" anyway, because it looked only at an environment
flag. Permission/configuration and an execution backend are SEPARATE axes and
both have to hold.

So availability is decided from runtime probes the caller actually measures,
not from environment variables alone:

    available       registered, permitted, and a working backend exists
    dry_run_only    registered and permitted, but nothing can execute it
    disabled        switched off by configuration
    needs_setup     registered, but required credentials are absent
    not_registered  no such tools in this build

Where the runtime genuinely cannot tell, the category keeps `available` rather
than acquiring an invented caveat — but anything with a KNOWN negative signal
must report it. Claiming an ability Nova does not have is the failure mode
being designed against.
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


#: Every state a capability can be in. `available` is the ONLY one that means
#: "I can do this right now".
STATES = ("available", "dry_run_only", "disabled", "needs_setup", "not_registered")


@dataclass
class Capability:
    """One category of things Nova can do, and whether she can do it now."""

    key: str
    label: str
    state: str
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
                if c.state in ("disabled", "needs_setup", "dry_run_only") and c.tools]

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

    def state_of(self, key: str) -> str:
        """The state of one category, for callers that need the raw answer."""
        for c in self.capabilities:
            if c.key == key:
                return c.state
        return "not_registered"

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


#: Credentials a category CANNOT work without. Checked by presence only — Nova
#: never reads the value, and an unlisted category is simply not credential-
#: checked rather than assumed broken.
_REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "communication": ("DISCORD_BOT_TOKEN",),
    "weather_maps": ("OPENWEATHER_API_KEY", "GOOGLE_MAPS_API_KEY"),
}


def _missing_credentials(key: str) -> list[str]:
    """Required credentials that are absent. Empty when nothing is required."""
    required = _REQUIRED_CREDENTIALS.get(key, ())
    return [name for name in required if not (os.getenv(name) or "").strip()]


def summarize_capabilities(tool_names: list[str] | None,
                           probes: dict[str, object] | None = None) -> CapabilityReport:
    """Group the REGISTERED tool names and judge each category's state.

    `tool_names` comes from `ToolRouter.list_tools()` — the same registry the
    agent loop calls — so the answer cannot drift from what is wired up.

    `probes` carries what the RUNTIME measured, because registration alone
    cannot answer "can you do this now":

        computer_can_execute   bool  — ComputerControl.available
                                       (false when no platform adapter exists)

    Anything absent from `probes` is simply not known, and an unknown never
    invents a caveat — but it never overrides a known negative either.
    """
    facts = dict(probes or {})
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
        # Runtime facts Nova can actually check. An unknown is not evidence of
        # being broken and gets no invented caveat; a KNOWN negative always wins.
        if key == "computer_control":
            if _env_off("NOVA_ALLOW_SHELL"):
                state, note = "disabled", "shell access is switched off"
            elif facts.get("computer_can_execute") is False:
                # The tools exist and are permitted, but ComputerControl has no
                # platform adapter, so every action is a dry run. Saying
                # "available" here would promise something that cannot happen.
                state, note = ("dry_run_only",
                               "no platform adapter is installed, so actions are "
                               "dry runs only")
        elif key == "research" and _env_off("NOVA_ALLOW_NETWORK_TOOLS"):
            state, note = "disabled", "network tools are switched off"
        elif key == "self_inspection" and _env_off("NOVA_DEV_MODE", "0"):
            state, note = "disabled", "developer mode is off"
        else:
            missing = _missing_credentials(key)
            if missing:
                state, note = ("needs_setup",
                               f"not connected yet ({', '.join(missing)} not set)")

        report.capabilities.append(
            Capability(key=key, label=label, state=state, tools=owned, note=note))

    return report
